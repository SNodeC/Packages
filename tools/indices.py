"""Index retained feed packages and preserve previous APT by-hash generations."""

import datetime
import email.utils
import gzip
import os
from pathlib import Path

from common import capture, digest, namespace, run
from metadata import ipk_control


def apt(feed, ledger, target, fingerprint, gpg):
    space = namespace(target)
    codename = target["codename"]
    suite = feed / space / "dists" / codename
    entries = [item for item in ledger["entries"] if namespace(item["target"]) == space
               and item["target"]["codename"] == codename]
    architectures = sorted({item["target"]["arch"] for item in entries} | {target["arch"]}
                           | {path.name.removeprefix("binary-") for path in (suite / "main").glob("binary-*")})
    checksums = {}
    for arch in architectures:
        binary = suite / "main" / ("binary-" + arch)
        by_hash = binary / "by-hash/SHA256"
        by_hash.mkdir(parents=True, exist_ok=True)
        paths = sorted({entry["path"] for item in entries if item["target"]["arch"] == arch
                        for entry in item["inventory"]})
        stanzas = []
        for path in paths:
            package = feed / path
            stanza = capture("dpkg-deb", "--field", str(package))
            stanza += f"\nFilename: {Path(path).relative_to(space)}\nSize: {package.stat().st_size}"
            stanzas.append(stanza + f"\nSHA256: {digest(package)}\n\n")
        data = "".join(stanzas).encode()
        generation = {}
        for name, content in (("Packages", data), ("Packages.gz", gzip.compress(data, mtime=0))):
            index = binary / name
            index.write_bytes(content)
            checksum = digest(index)
            (by_hash / checksum).write_bytes(content)
            generation[name] = checksum
            checksums[str(index.relative_to(suite))] = checksum, len(content)
        key = binary.relative_to(feed).as_posix()
        history = ledger["by_hash"].setdefault(key, [])
        if not history or history[-1]["indices"] != generation:
            history.append(dict(indices=generation, paths=paths))
        del history[:-3]
        hashes = {value for record in history for value in record["indices"].values()}
        for path in by_hash.iterdir():
            if path.name not in hashes:
                path.unlink()
    label = (Path("ci/feed-label").read_text()).strip()
    date = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc), usegmt=True)
    lines = ["Origin: " + os.environ["GITHUB_REPOSITORY"], "Label: " + label, "Suite: " + codename,
             "Codename: " + codename, "Components: main", "Architectures: " + " ".join(architectures),
             "Date: " + date, "Acquire-By-Hash: yes", "SHA256:"]
    lines.extend(f" {checksum} {size:16d} {name}" for name, (checksum, size) in sorted(checksums.items()))
    release = suite / "Release"
    release.write_text("\n".join(lines) + "\n")
    for name, mode in (("Release.gpg", "--detach-sign"), ("InRelease", "--clearsign")):
        gpg("--local-user", fingerprint, "--armor", mode, "--output", str(suite / name), str(release))
    return None


def index(feed, ledger, target, fingerprint, tools, gpg, key_files):
    family = target["family"]
    path = feed / namespace(target)
    if family == "deb":
        apt(feed, ledger, target, fingerprint, gpg)
    elif family == "rpm":
        run("createrepo_c", str(path))
        metadata = path / "repodata/repomd.xml"
        gpg("--local-user", fingerprint, "--armor", "--detach-sign", "--output", str(metadata) + ".asc", str(metadata))
    elif family == "ipk":
        stanzas = []
        for package in sorted(path.glob("*.ipk")):
            extra = f"\nFilename: {package.name}\nSize: {package.stat().st_size}\nSHA256sum: {digest(package)}\n\n"
            stanzas.append(ipk_control(package) + extra)
        packages = path / "Packages"
        packages.write_text("".join(stanzas))
        (path / "Packages.gz").write_bytes(gzip.compress(packages.read_bytes(), mtime=0))
        run(str(tools / "usign"), "-S", "-s", str(key_files["usign"]), "-m", str(packages),
            "-x", str(path / "Packages.sig"))
    else:
        run(str(tools / "apk"), "mkndx", "--allow-untrusted", "--sign-key", str(key_files["apk"]),
            "--output", str(path / "packages.adb"), *[str(package) for package in sorted(path.glob("*.apk"))])
    return None
