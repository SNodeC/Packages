"""Wire formats shared by the wave jobs."""
import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path


def run(*args, **kwargs):
    result = subprocess.run(args, check=True, **kwargs)
    return result


def read_json(path):
    result = json.loads(Path(path).read_text())
    return result


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")
    return None


def fetch(url):
    with urllib.request.urlopen(url) as response:
        result = response.read()
    return result


def signature(secret, timestamp, body):
    result = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body,
                                  hashlib.sha256).hexdigest()
    return result


def callback(scope, status, **fields):
    url = os.environ.get("CALLBACK_URL", "")
    if url:
        body = urllib.parse.urlencode(dict(scope=scope, status=status, **fields)).encode()
        timestamp = str(int(time.time()))
        request = urllib.request.Request(url, body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CoreCI-Timestamp": timestamp,
            "X-CoreCI-Signature-256": signature(os.environ["CORECI_CALLBACK_SECRET"], timestamp, body),
        })
        with urllib.request.urlopen(request) as response:
            response.read()
    return None


def version(unit, target):
    release = str(unit["revision"])
    if target["family"] == "deb":
        release += "~" + target["codename"]
    elif target["family"] == "rpm":
        release += "." + target["disttag"]
    else:
        release = "r" + release
    result = unit["version"] + "-" + release
    return result


def sha256(path):
    with Path(path).open("rb") as source:
        result = hashlib.file_digest(source, "sha256").hexdigest()
    return result


if __name__ == "__main__":
    callback("wave", "finished", run_id=os.environ["GITHUB_RUN_ID"], conclusion=os.environ["CONCLUSION"])
