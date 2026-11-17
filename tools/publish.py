"""Retain, index, sign and publish the successful units in one root commit."""
import datetime
import email.utils
import gzip
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from common import callback, fetch, owns_wave, read_json, run, sha256, version, write_json


def retained(publications):
    versions = {}
    selected = []
    for publication in reversed(publications):
        key = (publication["unit"]["package"], publication["target"]["id"])
        versions.setdefault(key, set())
        identity = (publication["unit"]["version"], publication["unit"]["revision"])
        if len(versions[key]) < 2 and identity not in versions[key]:
            versions[key].add(identity)
            selected.append(publication)
    result = list(reversed(selected))
    return result


def release(codename, architectures, files, date):
    lines = ["Origin: SNodeC/Packages", "Label: SNode.C", "Suite: " + codename,
             "Codename: " + codename, "Components: main", "Architectures: " + " ".join(sorted(architectures)),
             "Date: " + email.utils.format_datetime(date, usegmt=True), "Acquire-By-Hash: yes", "SHA256:"]
    for name, (digest, size) in sorted(files.items()):
        lines.append(f" {digest} {size:16d} {name}")
    result = "\n".join(lines) + "\n"
    return result


def capture(*args, **kwargs):
    result = subprocess.check_output(args, text=True, **kwargs).strip()
    return result


def gpg(*args):
    run("gpg", "--batch", "--yes", "--homedir", str(Path("signing/gnupg").resolve()), *args)
    return None


def openwrt_tools(catalog):
    directory = Path("/tmp/codex/build/Packages/publish/sdk")
    if not directory.exists():
        sdk = next(target["sdk"] for target in catalog if target["family"] == "apk" and target["arch"] == "x86_64")
        directory.mkdir(parents=True)
        archive = directory.parent / "sdk.tar.zst"
        archive.write_bytes(fetch(sdk["url"]))
        assert sha256(archive) == sdk["sha256"]
        run("tar", "--zstd", "-xf", str(archive), "-C", str(directory), "--strip-components=1")
    result = directory / "staging_dir/host/bin"
    return result


def apt_indices(feed, publications, codename):
    suite = feed / "apt/dists" / codename
    shutil.rmtree(suite, ignore_errors=True)
    entries = [item for item in publications if item["target"]["family"] == "deb" and
               item["target"]["codename"] == codename]
    architectures = {item["target"]["arch"] for item in entries}
    hashes = {}
    for arch in sorted(architectures):
        binary = suite / "main" / ("binary-" + arch)
        binary.mkdir(parents=True)
        stanzas = []
        files = sorted({file for item in entries if item["target"]["arch"] == arch for file in item["files"]})
        for file in files:
            package = feed / file
            stanza = capture("dpkg-deb", "--field", str(package))
            stanza += "\nFilename: " + str(Path(file).relative_to("apt"))
            stanza += f"\nSize: {package.stat().st_size}\nSHA256: {sha256(package)}\n\n"
            stanzas.append(stanza)
        packages = "".join(stanzas).encode()
        (binary / "Packages").write_bytes(packages)
        (binary / "Packages.gz").write_bytes(gzip.compress(packages, mtime=0))
        for name in ("Packages", "Packages.gz"):
            index = binary / name
            digest = sha256(index)
            hashed = binary / "by-hash/SHA256" / digest
            hashed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(index, hashed)
            hashes[str(index.relative_to(suite))] = (digest, index.stat().st_size)
    (suite / "Release").write_text(release(codename, architectures, hashes, datetime.datetime.now(datetime.timezone.utc)))
    gpg("--armor", "--detach-sign", "--output", str(suite / "Release.gpg"), str(suite / "Release"))
    gpg("--clearsign", "--output", str(suite / "InRelease"), str(suite / "Release"))
    return None


def ipk_index(directory):
    stanzas = []
    for package in sorted(directory.glob("*.ipk")):
        with tarfile.open(package) as outer:
            control = next(member for member in outer if member.name.lstrip("./") == "control.tar.gz")
            with tarfile.open(fileobj=io.BytesIO(outer.extractfile(control).read())) as inner:
                member = next(member for member in inner if member.name.lstrip("./") == "control")
                stanza = inner.extractfile(member).read().decode().rstrip()
        stanzas.append(stanza + f"\nFilename: {package.name}\nSize: {package.stat().st_size}\nSHA256sum: {sha256(package)}\n\n")
    (directory / "Packages").write_text("".join(stanzas))
    return None


def place(feed, target, unit, source, verified):
    family = target["family"]
    if family == "deb":
        destination = Path("apt/pool/main") / unit["package"] / target["id"] / version(unit, target)
    elif family == "rpm":
        destination = Path("rpm") / target["distribution"] / target["release"] / target["arch"]
    else:
        destination = Path("openwrt") / target["release"] / target["arch"]
    (feed / destination).mkdir(parents=True, exist_ok=True)
    files = {}
    packages = []
    for package in sorted(source.glob("*." + family)):
        assert str(package.relative_to(source.parent)) in verified
        relative = destination / package.name
        output = feed / relative
        shutil.copy2(package, output)
        if family == "rpm":
            run("rpmsign", "--define", "_gpg_name " + os.environ["GPG_FINGERPRINT"],
                "--define", "_gpg_path " + str(Path("signing/gnupg").resolve()), "--addsign", str(output))
            packages.append(capture("rpm", "-qp", "--qf", "%{NAME}", str(output)))
        elif family == "deb":
            packages.append(capture("dpkg-deb", "--field", str(output), "Package"))
        files[str(relative)] = sha256(output)
    assert files, "no packages in successful unit artifact"
    result = {"unit": {key: value for key, value in unit.items() if key != "declaration"},
              "target": {key: value for key, value in target.items()
                         if key not in ("units", "feed_branch", "feed_commit")},
              "files": files, "packages": packages}
    return result


def publication_identity(unit, target):
    result = (unit["package"], target["id"], unit["version"], unit["revision"])
    return result


def publish():
    feed = Path("feed-work")
    feed.mkdir()
    branch = os.environ["FEED_BRANCH"]
    assert branch in ("feed", "feed-staging")
    exists = subprocess.run(["git", "ls-remote", "--exit-code", "--heads", "origin", branch], capture_output=True)
    assert exists.returncode in (0, 2), exists.stderr.decode()
    commit = None
    if exists.returncode == 0:
        run("git", "fetch", "--depth=1", "origin", branch)
        commit = capture("git", "rev-parse", "FETCH_HEAD")
        archive = subprocess.check_output(["git", "archive", "FETCH_HEAD"])
        with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
            contents.extractall(feed, filter="data")
    ledger = read_json(feed / "publications.json") if (feed / "publications.json").exists() else []
    added = []
    published = []
    identities = {publication_identity(item["unit"], item["target"]) for item in ledger}
    for manifest in sorted(Path("artifacts").glob("*/manifest.json")):
        data = read_json(manifest)
        for file, digest in data["files"].items():
            path = manifest.parent / file
            assert path.resolve().is_relative_to(manifest.parent.resolve())
            assert sha256(path) == digest, "artifact SHA256 mismatch: " + file
        for item in data["units"]:
            if item["state"] == "succeeded" and item["unit"]["action"] == "build":
                unit = item["unit"]
                identity = publication_identity(unit, data["target"])
                reason = "already published" if identity in identities else ""
                if not reason:
                    added.append(place(feed, data["target"], unit, manifest.parent / unit["package"], data["files"]))
                    identities.add(identity)
                published.append(dict(unit=unit, reason=reason))
    if added:
        combined = ledger + added
        selected = retained(combined)
        live = {file for item in selected for file in item["files"]}
        for file in {file for item in combined for file in item["files"]} - live:
            (feed / file).unlink()
        shutil.copytree("keys", feed / "keys", dirs_exist_ok=True)
        write_json(feed / "publications.json", selected)
        touched = {item["target"]["id"]: item["target"] for item in added}
        suites = set()
        tools = None
        for target in touched.values():
            family = target["family"]
            if family == "deb":
                suites.add(target["codename"])
            elif family == "rpm":
                directory = feed / "rpm" / target["distribution"] / target["release"] / target["arch"]
                run("createrepo_c", str(directory))
                gpg("--armor", "--detach-sign", "--output", str(directory / "repodata/repomd.xml.asc"),
                    str(directory / "repodata/repomd.xml"))
            else:
                if tools is None:
                    tools = openwrt_tools(read_json("ci/targets.json")["targets"])
                directory = feed / "openwrt" / target["release"] / target["arch"]
                if family == "ipk":
                    ipk_index(directory)
                    run(str(tools / "usign"), "-S", "-s", "signing/usign.key", "-m", str(directory / "Packages"),
                        "-x", str(directory / "Packages.sig"))
                else:
                    run(str(tools / "apk"), "mkndx", "--allow-untrusted", "--sign-key", str(Path("signing/apk.pem").resolve()),
                        "--output", "packages.adb", *[p.name for p in sorted(directory.glob("*.apk"))], cwd=directory)
        for codename in sorted(suites):
            apt_indices(feed, selected, codename)
        environment = dict(os.environ, GIT_DIR=str(Path(".git").resolve()), GIT_WORK_TREE=str(feed.resolve()),
                           GIT_INDEX_FILE=str(Path("feed.index").resolve()), GIT_AUTHOR_NAME="github-actions[bot]",
                           GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
                           GIT_COMMITTER_NAME="github-actions[bot]", GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com")
        run("git", "read-tree", "--empty", env=environment)
        paths = [path.name for path in feed.iterdir()]
        run("git", "add", "--", *paths, cwd=feed, env=environment)
        tree = capture("git", "write-tree", env=environment)
        commit = capture("git", "commit-tree", tree, "-m", "Publish wave " + os.environ["WAVE_ID"], env=environment)
    if owns_wave():
        if added:
            run("git", "push", "--force", "origin", commit + ":refs/heads/" + branch)
    else:
        published = []
        commit = None
    return published, commit


def main():
    if owns_wave():
        os.umask(0o077)
        Path("signing/gnupg").mkdir(parents=True, mode=0o700)
        for environment, name in (("PACKAGES_GPG_PRIVATE_KEY", "gpg.asc"),
                                  ("OPENWRT_USIGN_PRIVATE_KEY", "usign.key"),
                                  ("OPENWRT_APK_PRIVATE_KEY", "apk.pem")):
            Path("signing", name).write_text(os.environ[environment])
        gpg("--import", "signing/gpg.asc")
        try:
            published, commit = publish()
        except Exception as error:
            for manifest in Path("artifacts").glob("*/manifest.json"):
                for item in read_json(manifest)["units"]:
                    if item["state"] == "succeeded" and item["unit"]["action"] == "build":
                        callback("unit", "failed", build_id=item["unit"]["build_id"], phase="publish", reason=str(error))
            raise
        for item in published:
            print(item["unit"]["build_id"] + ": " + (item["reason"] or "published"), flush=True)
            callback("unit", "succeeded", build_id=item["unit"]["build_id"], phase="publish", feed_commit=commit,
                     reason=item["reason"])
    return None


if __name__ == "__main__":
    main()
