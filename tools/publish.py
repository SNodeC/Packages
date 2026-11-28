"""Publish the valid items of a claimed, frozen plan using one leased atomic push."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from build import unpack_sdk
from common import (BRANCHES, COMMIT, DIGEST, FAMILIES, NAME, NATIVE_NAME, NoRedirect, Workflow, capture, digest, fetch,
                    matches, namespace, positive, read_json, require, run, version, write_json)
from indices import index
from metadata import metadata


class ItemError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        return None


def group(item):
    return [item[key] for key in ("package", "version", "revision")] + [item["target"]["id"]]


def index_group(target):
    return namespace(target), target["codename"] if target["family"] == "deb" else ""


def native(entry, target):
    return namespace(target), entry["name"], entry["version"], entry["arch"]


def api(path):
    origin = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    url = origin + "/repos/" + os.environ["GITHUB_REPOSITORY"] + "/actions/" + path
    request = Request(url, headers={"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    return build_opener(NoRedirect()).open(request, timeout=30)


def artifact(item, destination):
    artifact_id = item["artifact_id"]
    zip_path = destination / "download.zip"
    try:
        with api(f"artifacts/{artifact_id}") as response:
            info = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            with api(f'runs/{item["run_id"]}') as response:
                require(json.load(response)["id"] == item["run_id"], "artifact run identity mismatch")
            raise ItemError("artifact-expired", "artifact missing from its origin run") from error
        raise
    require(info["id"] == artifact_id and info["workflow_run"]["id"] == item["run_id"] and
            info["name"] == f'coreci-{item["request"]}-{item["attempt"]}', "artifact identity mismatch")
    if info["expired"]:
        raise ItemError("artifact-expired", "artifact expired")
    try:
        with api(f"artifacts/{artifact_id}/zip") as stream, zip_path.open("wb") as output:
            shutil.copyfileobj(stream, output)
    except HTTPError as error:
        if error.code == 302:
            location = error.headers["Location"]
            require(location.startswith("https://"), "invalid artifact download location")
            fetch(location, zip_path)
        elif error.code == 410:
            raise ItemError("artifact-expired", "artifact expired during download") from error
        else:
            raise
    require(digest(zip_path) == item["artifact_digest"], "artifact digest mismatch")
    control = {"manifest.json", "inventory.json"}
    with zipfile.ZipFile(zip_path) as archive:
        files = {entry.filename: entry for entry in archive.infolist()}
        require(len(files) == len(archive.infolist()) and control <= files.keys(), "invalid artifact contents")
        manifest = json.loads(archive.read("manifest.json"))
        expected = {key: item[key] for key in ("package", "version", "revision")}
        require(manifest["unit"] == expected and manifest["target"] == item["target"]["id"] and
                all(manifest[key] == item[key] for key in ("request", "attempt")), "manifest identity mismatch")
        checksums = manifest["files"]
        require(bool(checksums) and files.keys() == control | checksums.keys(), "artifact manifest contents mismatch")
        declared = json.loads(archive.read("inventory.json"))
        require(declared == item["inventory"], "artifact inventory does not match the build report")
        for filename, checksum in checksums.items():
            mode = stat.S_IFMT(files[filename].external_attr >> 16)
            require(matches(NATIVE_NAME, filename) and filename.endswith("." + item["target"]["family"]) and
                    mode in (0, stat.S_IFREG) and matches(DIGEST, checksum), "unsafe artifact member")
            package = destination / filename
            with archive.open(filename) as source, package.open("wb") as output:
                shutil.copyfileobj(source, output)
            require(digest(package) == checksum, "package digest mismatch")
    return checksums


def check_plan(plan, workflow):
    branch = os.environ["FEED_BRANCH"]
    fault = os.environ["FAULT"]
    require(branch in BRANCHES and plan["feed_branch"] == branch and fault == plan["fault"] and
            (fault == "none" or (fault == "after_push" and branch == "feed-staging")), "invalid feed or fault")
    require(plan["publication"] == workflow.owner["id"] and plan["attempt"] == workflow.owner["attempt"] and
            matches(COMMIT, plan["expected_head"]) and time.time() < plan["deadline"], "invalid or expired publication")
    require(plan["items"] or plan["retirements"] or plan["retire_legacy_apt"], "empty publication")
    seen = set()
    for item in plan["items"]:
        target = item["target"]
        require(matches(NAME, item["request"]) and item["request"] not in seen and
                all(positive(item[key]) for key in ("attempt", "revision", "run_id", "artifact_id")) and
                all(matches(DIGEST, item[key]) for key in ("identity", "artifact_digest")), "invalid publication item")
        require(target["family"] in FAMILIES and all(matches(NAME, target[key]) for key in
                ("distribution", "release", "codename", "arch", "id")) and matches(NAME, item["package"]),
                "invalid publication target")
        seen.add(item["request"])
    return None


def remote_refs(*patterns):
    text = capture("git", "ls-remote", "--refs", "origin", *patterns)
    return {ref: commit for commit, ref in (line.split("\t") for line in text.splitlines())}


def stream_tree(feed, branch, expected):
    run("git", "fetch", "--depth=1", "origin", "refs/heads/" + branch)
    require(capture("git", "rev-parse", "FETCH_HEAD") == expected, "expected feed head changed")
    feed.mkdir()
    with subprocess.Popen(["git", "archive", expected], stdout=subprocess.PIPE) as process:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            archive.extractall(feed, filter="data")
        require(process.wait() == 0, "cannot stream feed tree")
    return None


def push(feed, plan, workflow, workspace):
    branch = plan["feed_branch"]
    head = "refs/heads/" + branch
    prefix = "refs/snapshots/" + branch + "/"
    fresh = prefix + plan["publication"]
    delete = plan["delete_snapshots"]
    refs = remote_refs(prefix + "*")
    require(fresh not in refs and len(delete) == len(set(delete)), "snapshot already exists or duplicate deletion")
    for ref in delete:
        require(ref.startswith(prefix) and matches(NAME, ref[len(prefix):]) and ref in refs and
                ref != prefix + "pre-x" and refs[ref] != plan["expected_head"], "protected or unknown snapshot")
    environment = dict(os.environ, GIT_DIR=str(Path(".git").resolve()), GIT_WORK_TREE=str(feed),
                       GIT_INDEX_FILE=str(workspace / "index"), GIT_AUTHOR_NAME="github-actions[bot]",
                       GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
                       GIT_COMMITTER_NAME="github-actions[bot]",
                       GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com")
    run("git", "add", "--", ".", cwd=feed, env=environment)
    tree = capture("git", "write-tree", env=environment)
    commit = capture("git", "commit-tree", tree, "-m", "Publish " + plan["publication"])
    workflow.document(Path(".claim").read_text())
    require(time.time() < plan["deadline"], "publication deadline elapsed before push")
    leases = [f"--force-with-lease={head}:{plan['expected_head']}", f"--force-with-lease={fresh}:"]
    leases.extend(f"--force-with-lease={ref}:{refs[ref]}" for ref in delete)
    run("git", "push", "--atomic", *leases, "origin", f"{commit}:{head}", f"{commit}:{fresh}",
        *[":" + ref for ref in delete])
    return commit


def load_ledger(feed):
    path = feed / "publications.json"
    data = read_json(path)
    if isinstance(data, list):
        data = dict(entries=[], receipts=[], by_hash={},
                    legacy=[item for item in data if item["target"]["family"] != "deb"])
    return data


def import_legacy(feed, ledger, plan, touched, apk):
    pending = []
    snapshot = "refs/snapshots/" + plan["feed_branch"] + "/pre-x"
    date = None
    for entry in ledger["legacy"]:
        target = entry["target"]
        if namespace(target) in touched:
            if date is None:
                run("git", "fetch", "--depth=1", "origin", snapshot)
                date = int(capture("git", "show", "-s", "--format=%ct", "FETCH_HEAD"))
            record = {**entry["unit"], "target": target, "legacy": True}
            record["published_at"] = plan["legacy_dates"].get("/".join(map(str, group(record))), date)
            known = []
            for path in sorted(entry["files"]):
                package = feed / path
                data = metadata(target["family"], package, apk=apk)
                known.append(dict(data, path=path, sha256="unknown", served_sha256=digest(package)))
            ledger["entries"].append(dict(record, inventory=known))
        else:
            pending.append(entry)
    ledger["legacy"] = pending
    return None


def signers(workspace):
    files = {}
    for name, variable in (("gpg", "PACKAGES_GPG_PRIVATE_KEY"), ("usign", "OPENWRT_USIGN_PRIVATE_KEY"),
                           ("apk", "OPENWRT_APK_PRIVATE_KEY")):
        path = workspace / (name + ".key")
        path.touch(mode=0o600)
        path.write_text(os.environ.pop(variable))
        files[name] = path
    home = workspace / "gnupg"
    home.mkdir(mode=0o700)

    def gpg(*args):
        result = capture("gpg", "--batch", "--yes", "--homedir", str(home), *args)
        return result

    gpg("--import", str(files["gpg"]))
    pairs = gpg("--with-colons", "--list-secret-keys").splitlines()
    fingerprint = next(line.split(":")[9] for line in pairs if line.startswith("fpr:"))
    return files, gpg, home, fingerprint


def place(feed, item, source, checksums, known, apk, home, fingerprint):
    target = item["target"]
    family = target["family"]
    space = namespace(target)
    inventory, pending, identities = [], [], set()
    for filename, checksum in sorted(checksums.items()):
        try:
            record = metadata(family, source / filename, apk=apk)
        except subprocess.CalledProcessError as error:
            raise ItemError("malformed-artifact", "cannot read native package metadata") from error
        require(record["version"] == version(item, target), "native artifact version mismatch")
        require(record["arch"] in {target["arch"], "all", "noarch"}, "native artifact architecture mismatch")
        token = native(record, target)
        require(token not in identities, "duplicate split identity")
        identities.add(token)
        previous = known.get(token)
        if previous:
            if previous["sha256"] != checksum:
                raise ItemError("immutable-conflict", "different original digest for native identity")
            inventory.append(previous)
        else:
            path = space / filename
            if family == "deb":
                path = space / "pool/main" / item["package"] / target["id"] / filename
            if (feed / path).exists():
                raise ItemError("immutable-conflict", "native filename already present in namespace")
            pending.append((filename, dict(record, path=path.as_posix(), sha256=checksum)))
    for filename, record in pending:
        path = feed / record["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / filename, path)
        if family == "rpm":
            run("rpmsign", "--define", "_gpg_name " + fingerprint, "--define", "_gpg_path " + str(home),
                "--addsign", str(path))
        record["served_sha256"] = digest(path)
        known[native(record, target)] = record
        inventory.append(record)
    return inventory, bool(pending)


def publish(plan, workflow, workspace):
    feed = workspace / "feed"
    stream_tree(feed, plan["feed_branch"], plan["expected_head"])
    ledger = load_ledger(feed)
    files, gpg, home, fingerprint = signers(workspace)
    touched = {index_group(item["target"]): item["target"] for item in plan["items"]}
    expired = {(tuple(entry)) for entry in plan["retirements"]}
    touched.update((index_group(entry["target"]), entry["target"])
                   for entry in ledger["entries"] if tuple(group(entry)) in expired)
    tools = workspace / "openwrt"
    if any(target["family"] in {"ipk", "apk"} for target in touched.values()):
        catalog = read_json("ci/targets.json")
        sdk = next((target for target in catalog["targets"] if target["id"] == catalog.get("apk_signing_sdk")), None)
        require(sdk and sdk["family"] == "apk" and "sdk" in sdk, "catalog apk_signing_sdk must name an apk target with an SDK")
        unpack_sdk(sdk, tools)
    tools /= "staging_dir/host/bin"
    apk = str(tools / "apk")
    import_legacy(feed, ledger, plan, {space for space, _ in touched}, apk)
    known = {native(record, entry["target"]): record for entry in ledger["entries"] for record in entry["inventory"]}
    results = []
    for number, item in enumerate(plan["items"]):
        receipt = {key: item[key] for key in ("request", "revision", "identity", "artifact_digest")}
        receipt.update(publication=plan["publication"], attempt=plan["attempt"], inventory=[])
        source = workspace / f"artifact-{number}"
        source.mkdir()
        try:
            checksums = artifact(item, source)
            inventory, added = place(feed, item, source, checksums, known, apk, home, fingerprint)
            entry = {key: item[key] for key in ("package", "version", "revision", "target", "identity",
                                               "repo", "commit", "ci_commit", "bindings", "closure")}
            if added or not any(group(existing) == group(entry) for existing in ledger["entries"]):
                ledger["entries"].append(dict(entry, inventory=inventory, published_at=int(time.time()), openwrt_register=item.get("openwrt_register", "")))
            receipt.update(outcome="published", reason="" if added else "already published", inventory=inventory)
        except (ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile, tarfile.TarError, StopIteration) as error:
            receipt.update(outcome="failed", reason=str(error), failure=getattr(error, "code", "malformed-artifact"))
        results.append(receipt)
    old_paths = {record["path"] for entry in ledger["entries"] for record in entry["inventory"]}
    old_paths.update(path for history in ledger["by_hash"].values() for gen in history for path in gen["paths"])
    ledger["entries"] = [entry for entry in ledger["entries"] if tuple(group(entry)) not in expired]
    for target in touched.values():
        if target["family"] == "deb":
            index(feed, ledger, target, fingerprint, tools, gpg, files)
    live = {record["path"] for entry in ledger["entries"] for record in entry["inventory"]}
    live.update(path for history in ledger["by_hash"].values() for gen in history for path in gen["paths"])
    for path in old_paths - live:
        (feed / path).unlink()
    for target in touched.values():
        if target["family"] != "deb":
            index(feed, ledger, target, fingerprint, tools, gpg, files)
    if plan["retire_legacy_apt"]:
        for directory in ("apt/dists", "apt/pool"):
            shutil.rmtree(feed / directory, ignore_errors=True)
        (feed / "apt/SOURCES.md").unlink(missing_ok=True)
    elif (feed / "apt/dists").is_dir():
        shutil.copyfile("ci/legacy-apt.md", feed / "apt/SOURCES.md")
    acknowledged = {tuple(key) for key in plan["acknowledged_receipts"]}
    ledger["receipts"] = [r for r in ledger["receipts"]
                          if (r["publication"], r["attempt"], r["request"]) not in acknowledged] + results
    shutil.copytree("keys", feed / "keys", dirs_exist_ok=True)
    write_json(feed / "publications.json", dict(ledger, publication=plan["publication"]))
    commit = push(feed, plan, workflow, workspace)
    if plan["fault"] == "after_push":
        sys.exit("qualification fault after successful feed push")
    for receipt in results:
        workflow.report("item", receipt=json.dumps(receipt), feed_commit=commit)
    return None


if __name__ == "__main__":
    workflow = Workflow("publications")
    plan = read_json(".document")
    check_plan(plan, workflow)
    workflow.report("started")
    with tempfile.TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as directory:
        try:
            publish(plan, workflow, Path(directory))
        except Exception as error:
            workflow.report("finished", outcome="failed", reason=str(error))
            raise
    workflow.report("finished", outcome="published")
