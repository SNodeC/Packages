"""Run one target's ordered build chain in one container and one SDK."""
import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from common import callback, fetch, read_json, run, sha256, version, write_json


def execute(container, *args, **kwargs):
    result = run("docker", "exec", container, *args, **kwargs)
    return result


def install_feed(container, target):
    base = "https://raw.githubusercontent.com/SNodeC/Packages/" + target["feed_branch"]
    if target["family"] == "deb":
        execute(container, "sh", "-ec",
                "gpg --dearmor < /work/keys/packages-gpg.asc > /usr/share/keyrings/snodec.gpg; "
                "printf '%s\\n' \"$1\" > /etc/apt/sources.list.d/snodec.list; apt-get update",
                "sh", f'deb [signed-by=/usr/share/keyrings/snodec.gpg] {base}/apt {target["codename"]} main')
    elif target["family"] == "rpm":
        repo = ("[snodec]\nname=SNode.C\nbaseurl=" + base + "/rpm/" +
                "/".join(target[key] for key in ("distribution", "release", "arch")) +
                "\nenabled=1\ngpgcheck=1\nrepo_gpgcheck=1\ngpgkey=file:///work/keys/packages-gpg.asc\n")
        execute(container, "sh", "-ec", 'printf "%s" "$1" > /etc/yum.repos.d/snodec.repo', "sh", repo)
    return None


def setup(container, target):
    openwrt = target["family"] in ("ipk", "apk")
    family = "openwrt" if openwrt else target["family"]
    dependencies = sorted({dependency for unit in target["units"]
                           for dependency in unit["declaration"]["build_dependencies"][family]})
    if target["family"] == "rpm":
        execute(container, "dnf", "install", "-y", "dnf-plugins-core")
        if target["distribution"] == "rocky":
            execute(container, "dnf", "config-manager", "--set-enabled", "crb")
            execute(container, "dnf", "install", "-y", "epel-release")
            if target["release"] == "9":
                dependencies += ["gcc-toolset-14-gcc-c++"]
        execute(container, "dnf", "install", "-y", "git", "jq", "rpm-build", "gnupg2", *dependencies)
    else:
        execute(container, "apt-get", "update")
        base = ["ca-certificates", "git", "jq", "gnupg", "dpkg-dev"]
        if openwrt:
            base += ["build-essential", "clang", "flex", "bison", "gawk", "gettext", "libncurses-dev",
                     "libssl-dev", "python3", "python3-setuptools", "rsync", "swig", "unzip", "zstd", "wget", "file"]
        execute(container, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *base,
                *([] if openwrt else dependencies))
        if target["distribution"] == "raspberrypios":
            Path("work/raspberrypi-keyring.deb").write_bytes(fetch("https://archive.raspberrypi.com/debian/pool/main/r/raspberrypi-archive-keyring/raspberrypi-archive-keyring_2025.1+rpt1_all.deb"))
            execute(container, "sh", "-ec", "dpkg -i /work/raspberrypi-keyring.deb; "
                    'printf "%s\\n" "$1" > /etc/apt/sources.list.d/raspberrypi.list; apt-get update', "sh",
                    f'deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.pgp] https://archive.raspberrypi.com/debian/ {target["release"]} main')
    if any(unit["action"] == "reuse" for unit in target["units"]):
        install_feed(container, target)
    if openwrt:
        sdk = target["sdk"]
        archive = Path("work/sdk.tar.zst")
        archive.write_bytes(fetch(sdk["url"]))
        assert sha256(archive) == sdk["sha256"]
        checksums = fetch(sdk["sha256sums"]).decode()
        assert sdk["sha256"] + " *" + sdk["url"].rsplit("/", 1)[1] in checksums
        Path("work/sdk").mkdir()
        run("tar", "--zstd", "-xf", str(archive), "-C", "work/sdk", "--strip-components=1")
        execute(container, "sh", "-ec", "cd /work/sdk; ./scripts/feeds update -a; ./scripts/feeds install -a")
        Path("work/sdk/.config").write_text("CONFIG_ALL_NONSHARED=n\nCONFIG_ALL_KMODS=n\nCONFIG_SIGNED_PACKAGES=n\n")
    return None


def build(container, target, unit, output):
    source = Path("work/source") / unit["package"]
    run("git", "init", "-q", str(source))
    run("git", "-C", str(source), "fetch", "--depth=1", f'https://github.com/{unit["repo"]}.git', unit["commit"])
    run("git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD")
    run("git", "-C", str(source), "submodule", "update", "--init", "--recursive", "--depth=1")
    execute(container, "git", "config", "--global", "--add", "safe.directory", "/work/source/" + unit["package"])
    openwrt = target["family"] in ("ipk", "apk")
    full_version = version(unit, target)
    release = str(unit["revision"]) if openwrt else full_version[len(unit["version"]) + 1:]
    hook = unit["declaration"]["build"]["openwrt" if openwrt else target["family"]]
    args = ["env", "SOURCE_DIR=/work/source/" + unit["package"],
            "OUTPUT_DIR=/work/output/" + unit["package"], "SDK_DIR=/work/sdk",
            "TARGET_JSON=" + json.dumps(target), "PACKAGE_VERSION=" + unit["version"], "PACKAGE_RELEASE=" + release]
    command = 'exec sh "$SOURCE_DIR/$1"'
    if target["distribution"] == "rocky" and target["release"] == "9":
        command = '. /opt/rh/gcc-toolset-14/enable; ' + command
    execute(container, *args, "sh", "-ec", command, "sh", hook)
    suffix = "." + target["family"]
    packages = sorted((Path("work/output") / unit["package"]).glob("*" + suffix))
    assert packages, "hook produced no packages"
    destination = output / unit["package"]
    destination.mkdir()
    for package in packages:
        shutil.copy2(package, destination / package.name)
    if not openwrt:
        paths = ["/work/output/" + unit["package"] + "/" + package.name for package in packages]
        if target["family"] == "deb":
            execute(container, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *paths)
        else:
            execute(container, "dnf", "install", "-y", *paths)
    result = {str(path.relative_to(output)): sha256(path) for path in destination.iterdir()}
    return result


def reuse(container, target, unit):
    base = "https://raw.githubusercontent.com/SNodeC/Packages/" + target["feed_branch"]
    ledger = json.loads(fetch(base + "/publications.json"))
    publication = next(item for item in ledger if item["target"]["id"] == target["id"] and
                       item["unit"]["package"] == unit["package"] and
                       item["unit"]["version"] == unit["version"] and item["unit"]["revision"] == unit["revision"])
    if target["family"] == "deb":
        execute(container, "apt-get", "install", "-y", *[name + "=" + version(unit, target) for name in publication["packages"]])
    elif target["family"] == "rpm":
        execute(container, "dnf", "install", "-y", *[name + "-" + version(unit, target) for name in publication["packages"]])
    else:
        # The runtime feed has no development headers; the recipe stages them.
        staging_artifacts = Path("work/staging-artifacts")
        staging_artifacts.mkdir(exist_ok=True)
        build(container, target, unit, staging_artifacts)
        directory = Path("work/reuse") / unit["package"]
        directory.mkdir(parents=True)
        local_directory = "/work/reuse/" + unit["package"]
        remote = base + "/openwrt/" + target["release"] + "/" + target["arch"]
        tools = "/work/sdk/staging_dir/host/bin/"
        if target["family"] == "ipk":
            for name in ("Packages", "Packages.sig"):
                (directory / name).write_bytes(fetch(remote + "/" + name))
            execute(container, tools + "usign", "-V", "-p", "/work/keys/openwrt-usign.pub",
                    "-m", local_directory + "/Packages", "-x", local_directory + "/Packages.sig")
            stanzas = [dict(line.split(": ", 1) for line in block.splitlines()
                            if not line.startswith(" ") and ": " in line)
                       for block in (directory / "Packages").read_text().strip().split("\n\n")]
            trusted = {stanza["Filename"]: stanza["SHA256sum"] for stanza in stanzas}
        else:
            (directory / "keys").mkdir()
            shutil.copy2("keys/openwrt-apk.pub", directory / "keys/snodec.pub")
            (directory / "packages.adb").write_bytes(fetch(remote + "/packages.adb"))
            execute(container, tools + "apk", "verify", "--keys-dir", local_directory + "/keys",
                    local_directory + "/packages.adb")
            index = execute(container, tools + "apk", "adbdump", "--format", "json", local_directory + "/packages.adb",
                            capture_output=True, text=True)
            names = {Path(file).name for file in publication["files"]}
            packages = [package["name"] for package in json.loads(index.stdout)["packages"]
                        if package["name"] + "-" + package["version"] + ".apk" in names]
            assert len(packages) == len(names)
            execute(container, tools + "apk", "--no-cache", "--keys-dir", local_directory + "/keys", "--arch", target["arch"],
                    "--repository", remote + "/packages.adb", "fetch", "--all-matches", "--output", local_directory, *packages)
        staging = "/work/sdk/staging_dir/" + next(Path("work/sdk/staging_dir").glob("target-*")).name
        for file, digest in publication["files"].items():
            local = directory / Path(file).name
            if target["family"] == "ipk":
                assert trusted[local.name] == digest
                local.write_bytes(fetch(base + "/" + file))
            assert sha256(local) == digest
            if target["family"] == "ipk":
                with tarfile.open(local) as archive:
                    member = next(member for member in archive if member.name.lstrip("./") == "data.tar.gz")
                    (directory / "data.tar.gz").write_bytes(archive.extractfile(member).read())
                execute(container, "tar", "-xzf", local_directory + "/data.tar.gz", "-C", staging)
            else:
                execute(container, tools + "apk", "extract", "--allow-untrusted", "--destination", staging,
                        local_directory + "/" + local.name)
    return None


def main():
    target = json.loads(os.environ["TARGET"])
    output = Path("artifacts") / target["id"]
    output.mkdir(parents=True)
    manifest = {"target": target, "units": [], "files": {}}
    Path("work").mkdir()
    shutil.copytree("keys", "work/keys")
    container = "wave-chain"
    failed = set()
    try:
        run("docker", "run", "-d", "--name", container, "--platform", target["platform"],
            "-v", str(Path("work").resolve()) + ":/work", target["image"], "sleep", "infinity")
        setup(container, target)
        for unit in target["units"]:
            started = time.monotonic()
            state = "skipped"
            if not (set(unit["declaration"]["depends_on"]) & failed):
                try:
                    if unit["action"] == "build":
                        callback("unit", "started", build_id=unit["build_id"], phase="build")
                        files = build(container, target, unit, output)
                        manifest["files"].update(files)
                        callback("unit", "succeeded", build_id=unit["build_id"], phase="build", artifact=target["id"])
                    else:
                        reuse(container, target, unit)
                    state = "succeeded"
                except Exception as error:
                    state = "failed"
                    if unit["action"] == "build":
                        callback("unit", "failed", build_id=unit["build_id"], phase="build", reason=str(error))
            if state != "succeeded":
                failed.add(unit["package"])
            manifest["units"].append({"unit": unit, "state": state, "seconds": round(time.monotonic() - started, 1)})
            write_json(output / "manifest.json", manifest)
    finally:
        write_json(output / "manifest.json", manifest)
        subprocess.run(["docker", "rm", "-f", container], check=False)
    assert not failed, "failed or skipped units: " + ", ".join(sorted(failed))
    return None


if __name__ == "__main__":
    main()
