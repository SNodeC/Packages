"""Contract X documents, workflow callbacks, and package identities."""

import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}"
NATIVE_NAME = r"[A-Za-z0-9_][A-Za-z0-9_.+~-]{0,239}"
COMMIT = r"[0-9a-f]{40}"
DIGEST = r"[0-9a-f]{64}"
BRANCHES = {"feed", "feed-staging"}
FAMILIES = {"deb", "rpm", "ipk", "apk"}


def require(condition, message):
    if not condition:
        raise ValueError(message)
    return None


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def positive(value):
    return type(value) is int and value > 0


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args, **kwargs):
    return run(*args, capture_output=True, text=True, **kwargs).stdout.strip()


def digest(path):
    with Path(path).open("rb") as stream:
        value = hashlib.file_digest(stream, "sha256").hexdigest()
    return value


def read_json(path):
    return json.loads(Path(path).read_bytes())


def write_json(path, data):
    Path(path).write_text(json.dumps(data, separators=(",", ":")) + "\n")
    return None


def fetch(url, destination):
    with urlopen(url, timeout=60) as response, Path(destination).open("wb") as stream:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            stream.write(block)
    return None


def resolved_packages(declaration, target):
    family = "openwrt" if target["family"] in ("ipk", "apk") else target["family"]
    lists = declaration["build_dependencies"]
    selector = f'{family}@{target["distribution"]}-{target["release"]}'
    return lists.get(family, []) + lists.get(selector, []) + lists.get(f'{selector}-{target["arch"]}', [])


def release(unit, target):
    value = str(unit["revision"])
    if target["family"] == "deb":
        value += "~" + target["codename"]
    elif target["family"] == "rpm":
        value += "." + target["disttag"]
    return value


def version(unit, target):
    marker = "-r" if target["family"] in ("ipk", "apk") else "-"
    value = unit["version"] + marker + release(unit, target)
    return value


def namespace(target):
    family = target["family"]
    distribution, release, arch = (target[key] for key in ("distribution", "release", "arch"))
    if family == "deb":
        result = Path("apt", distribution)
    elif family == "rpm":
        result = Path("rpm", distribution, release, arch)
    else:
        result = Path("openwrt", release, arch)
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def signature(secret, timestamp, body):
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body,
                                hashlib.sha256).hexdigest()


class Workflow:
    def __init__(self, kind):
        origin = os.environ.get("CORECI_ORIGIN", "")
        uri = urlsplit(origin)
        require(uri.scheme == "https" and uri.hostname and uri.netloc and
                not (uri.username or uri.password or uri.query or uri.fragment) and
                origin.rstrip("/") == f"https://{uri.netloc}", "invalid CORECI_ORIGIN")
        identity = os.environ["REQUEST_ID"]
        require(matches(NAME, identity), "invalid workflow id")
        self.owner = dict(id=identity, attempt=int(os.environ["ATTEMPT"]),
                          run_id=int(os.environ["GITHUB_RUN_ID"]),
                          run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]))
        require(all(value > 0 for key, value in self.owner.items() if key != "id"), "invalid attempt or run id")
        self.endpoint = origin.rstrip("/") + f"/api/v1/{kind}/{identity}"
        self.secret = os.environ["CORECI_CALLBACK_SECRET"]
        require(bool(self.secret), "missing callback secret")
        self.http = build_opener(NoRedirect())
        self.headers = None
        return None

    def request(self, path, fields=None):
        body, headers = None, {}
        if fields is not None:
            body = urlencode(fields).encode()
            stamp = str(int(time.time()))
            headers = {"Content-Type": "application/x-www-form-urlencoded", "X-CoreCI-Timestamp": stamp,
                       "X-CoreCI-Signature-256": signature(self.secret, stamp, body)}
        request = Request(self.endpoint + path, body, headers)
        result = None
        for delay in (1, 4, 16, None):
            if result is None:
                try:
                    with self.http.open(request, timeout=30) as response:
                        result = response.status, response.headers, response.read(32 * 1024 * 1024 + 1)
                except HTTPError as error:
                    if error.code == 409 and path == "/claim":
                        result = 409, {}, b""
                    elif error.code != 503 or delay is None:
                        raise
                except (URLError, TimeoutError):
                    if delay is None:
                        raise
                if result is None:
                    time.sleep(delay)
        require(len(result[2]) <= 32 * 1024 * 1024, "scheduler response exceeds size limit")
        return result

    def claim(self):
        status, headers, body = self.request("/claim", {k: v for k, v in self.owner.items() if k != "id"})
        accepted = status == 200
        if accepted:
            stamp = headers.get("X-CoreCI-Timestamp", "")
            require(matches(r"[0-9]{1,12}", stamp) and abs(time.time() - int(stamp)) <= 300,
                    "claim timestamp is missing or outside the allowed window")
            signed = headers.get("X-CoreCI-Signature-256", "")
            require(hmac.compare_digest(signed, signature(self.secret, stamp, body)), "invalid claim signature")
            grant = json.loads(body)
            require(all(type(grant.get(k)) is type(v) and grant[k] == v for k, v in self.owner.items()) and
                    grant.get("granted") is True and matches(DIGEST, grant.get("document_sha256")),
                    "invalid claim grant")
            checksum = grant["document_sha256"]
            Path(".document").write_bytes(self.document(checksum))
            Path(".claim").write_text(checksum)
        else:
            require(status == 409, "claim was rejected")
            print("claim 409; no work")
        return accepted

    def document(self, checksum):
        status, _, body = self.request(f'/attempts/{self.owner["attempt"]}')
        require(status == 200 and hashlib.sha256(body).hexdigest() == checksum, "document digest mismatch")
        return body

    def report(self, status, **fields):
        code, _, _ = self.request("/report", dict(self.owner, status=status, **fields))
        require(code == 200, "report rejected")
        return None


if __name__ == "__main__":
    import sys

    action, kind = sys.argv[1:]
    workflow = Workflow(kind)
    if action == "claim":
        accepted = workflow.claim()
        with open(os.environ["GITHUB_OUTPUT"], "a") as job:
            job.write("accepted=" + str(accepted).lower() + "\n")
    elif action == "started":
        workflow.report("started")
    else:
        status = os.environ["JOB_STATUS"]
        kind = "cancelled" if status == "cancelled" else "workflow failed"
        artifact = os.environ.get("ARTIFACT_ID", "")
        checksum = os.environ.get("ARTIFACT_DIGEST", "")
        if status == "success" and artifact:
            require(matches(DIGEST, checksum), "missing artifact digest")
            timings = dict(read_json("artifact/manifest.json")["timings"], upload=int(time.time()) - int(os.environ["UPLOAD_STARTED"]))
            workflow.report("finished", outcome="built", artifact_id=artifact,
                            artifact_digest=checksum, inventory=Path("artifact/inventory.json").read_text(), timings=json.dumps(timings))
        else:
            workflow.report("finished", outcome="failed", reason=kind)
