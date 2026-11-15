"""Run one target's ordered build chain in one container and one SDK."""
import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from common import callback, fetch, read_json, run, sha256, version, write_json


class SetupError(RuntimeError):
    pass


def setup_run(step, *args, **kwargs):
    try:
        result = run(*args, capture_output=True, text=True, **kwargs)
    except (subprocess.CalledProcessError, OSError) as error:
        stdout, stderr = getattr(error, "stdout", ""), getattr(error, "stderr", "")
        print(stdout or "", end="", flush=True)
        print(stderr or "", end="", flush=True)
        lines = ((stderr or stdout or "").strip() or str(error)).splitlines()
        raise SetupError(f"setup: {step}: {lines[0]}") from error
    print(result.stdout or "", end="", flush=True)
    print(result.stderr or "", end="", flush=True)
    return result


def execute(container, *args, **kwargs):
    result = run("docker", "exec", container, *args, **kwargs)
    return result


def as_builder(container, *args, **kwargs):
    result = execute(container, "runuser", "-u", "builder", "--", *args, **kwargs)
    return result


def install_native(container, target, paths):
    if target["family"] == "deb":
        setup_run("install", "docker", "exec", container, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *paths)
    else:
        setup_run("install", "docker", "exec", container, "dnf", "install", "-y", *paths)
    return None


def install_feed(container, target):
    base = "https://raw.githubusercontent.com/SNodeC/Packages/" + target["feed_commit"]
    if target["family"] == "deb":
        setup_run("repositories", "docker", "exec", container, "sh", "-ec",
                "gpg --dearmor < /work/keys/packages-gpg.asc > /usr/share/keyrings/snodec.gpg; "
                "printf '%s\\n' \"$1\" > /etc/apt/sources.list.d/snodec.list; apt-get update",
                "sh", f'deb [signed-by=/usr/share/keyrings/snodec.gpg] {base}/apt {target["codename"]} main')
    elif target["family"] == "rpm":
        repo = ("[snodec]\nname=SNode.C\nbaseurl=" + base + "/rpm/" +
                "/".join(target[key] for key in ("distribution", "release", "arch")) +
                "\nenabled=1\ngpgcheck=1\nrepo_gpgcheck=1\ngpgkey=file:///work/keys/packages-gpg.asc\n")
        setup_run("repositories", "docker", "exec", container, "sh", "-ec", 'printf "%s" "$1" > /etc/yum.repos.d/snodec.repo', "sh", repo)
    return None


def setup(container, target):
    openwrt = target["family"] in ("ipk", "apk")
    family = "openwrt" if openwrt else target["family"]
    dependencies = sorted({dependency for unit in target["units"]
                           for dependency in unit["declaration"]["build_dependencies"][family]})
    if target["family"] == "rpm":
        setup_run("repositories", "docker", "exec", container, "dnf", "install", "-y", "dnf-plugins-core")
        if target["distribution"] == "rocky":
            setup_run("repositories", "docker", "exec", container, "dnf", "config-manager", "--set-enabled", "crb")
            setup_run("repositories", "docker", "exec", container, "dnf", "install", "-y", "epel-release")
            if target["release"] == "9":
                dependencies += ["gcc-toolset-14-gcc-c++"]
        setup_run("dependencies", "docker", "exec", container, "dnf", "install", "-y", "git", "jq", "rpm-build", "gnupg2", "file",
                "shadow-utils", "util-linux", *dependencies)
    else:
        setup_run("repositories", "docker", "exec", container, "apt-get", "update")
        base = ["ca-certificates", "git", "jq", "gnupg", "dpkg-dev", "file", "passwd", "util-linux"]
        if openwrt:
            base += ["build-essential", "clang", "flex", "bison", "gawk", "gettext", "libncurses-dev",
                     "libssl-dev", "python3", "python3-setuptools", "rsync", "swig", "unzip", "zstd", "wget"]
        setup_run("dependencies", "docker", "exec", container, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *base,
                *([] if openwrt else dependencies))
        if target["distribution"] == "raspberrypios":
            Path("work/raspberrypi-keyring.deb").write_bytes(fetch("https://archive.raspberrypi.com/debian/pool/main/r/raspberrypi-archive-keyring/raspberrypi-archive-keyring_2025.1+rpt1_all.deb"))
            setup_run("repositories", "docker", "exec", container, "sh", "-ec", "dpkg -i /work/raspberrypi-keyring.deb; "
                    'printf "%s\\n" "$1" > /etc/apt/sources.list.d/raspberrypi.list; apt-get update', "sh",
                    f'deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.pgp] https://archive.raspberrypi.com/debian/ {target["release"]} main')
    setup_run("builder", "docker", "exec", container, "useradd", "--uid", str(os.getuid()), "--create-home", "builder")
    if any(unit["action"] == "reuse" for unit in target["units"]):
        install_feed(container, target)
    if openwrt:
        try:
            sdk = target["sdk"]
            archive = Path("work/sdk.tar.zst")
            archive.write_bytes(fetch(sdk["url"]))
            assert sha256(archive) == sdk["sha256"], "SDK SHA256 mismatch"
            checksums = fetch(sdk["sha256sums"]).decode()
            assert sdk["sha256"] + " *" + sdk["url"].rsplit("/", 1)[1] in checksums, "SDK checksum list mismatch"
            Path("work/sdk").mkdir()
            setup_run("SDK", "tar", "--zstd", "-xf", str(archive), "-C", "work/sdk", "--strip-components=1")
            setup_run("SDK", "docker", "exec", container, "runuser", "-u", "builder", "--", "sh", "-ec",
                      "cd /work/sdk; ./scripts/feeds update -a; ./scripts/feeds install -a")
            Path("work/sdk/.config").write_text("CONFIG_ALL_NONSHARED=n\nCONFIG_ALL_KMODS=n\nCONFIG_SIGNED_PACKAGES=n\n")
        except Exception as error:
            if isinstance(error, SetupError):
                raise
            raise SetupError(f"setup: SDK: {str(error).splitlines()[0]}") from error
    return None


def build(container, target, unit, output):
    source = Path("work/source") / unit["package"]
    run("git", "init", "-q", str(source))
    run("git", "-C", str(source), "fetch", "--depth=1", f'https://github.com/{unit["repo"]}.git', unit["commit"])
    run("git", "-C", str(source), "checkout", "--detach", "FETCH_HEAD")
    run("git", "-C", str(source), "submodule", "update", "--init", "--recursive", "--depth=1")
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
    as_builder(container, *args, "sh", "-ec", command, "sh", hook)
    suffix = "." + target["family"]
    packages = sorted((Path("work/output") / unit["package"]).glob("*" + suffix))
    assert packages, "hook produced no packages"
    destination = output / unit["package"]
    destination.mkdir()
    for package in packages:
        shutil.copy2(package, destination / package.name)
    if not openwrt:
        paths = ["/work/output/" + unit["package"] + "/" + package.name for package in packages]
        install_native(container, target, paths)
    result = {str(path.relative_to(output)): sha256(path) for path in destination.iterdir()}
    return result


def reuse(container, target, unit):
    base = "https://raw.githubusercontent.com/SNodeC/Packages/" + target["feed_commit"]
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
            as_builder(container, tools + "usign", "-V", "-p", "/work/keys/openwrt-usign.pub",
                    "-m", local_directory + "/Packages", "-x", local_directory + "/Packages.sig")
            stanzas = [dict(line.split(": ", 1) for line in block.splitlines()
                            if not line.startswith(" ") and ": " in line)
                       for block in (directory / "Packages").read_text().strip().split("\n\n")]
            trusted = {stanza["Filename"]: stanza["SHA256sum"] for stanza in stanzas}
        else:
            (directory / "keys").mkdir()
            shutil.copy2("keys/openwrt-apk.pub", directory / "keys/snodec.pub")
            (directory / "packages.adb").write_bytes(fetch(remote + "/packages.adb"))
            as_builder(container, tools + "apk", "verify", "--keys-dir", local_directory + "/keys",
                    local_directory + "/packages.adb")
            index = as_builder(container, tools + "apk", "adbdump", "--format", "json", local_directory + "/packages.adb",
                            capture_output=True, text=True)
            names = {Path(file).name for file in publication["files"]}
            packages = [package["name"] for package in json.loads(index.stdout)["packages"]
                        if package["name"] + "-" + package["version"] + ".apk" in names]
            assert len(packages) == len(names)
            as_builder(container, tools + "apk", "--no-cache", "--keys-dir", local_directory + "/keys", "--arch", target["arch"],
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
                as_builder(container, "tar", "-xzf", local_directory + "/data.tar.gz", "-C", staging)
            else:
                as_builder(container, tools + "apk", "extract", "--allow-untrusted", "--destination", staging,
                        local_directory + "/" + local.name)
    return None


def main():
    target = json.loads(os.environ["TARGET"])
    phase = os.environ["CHAIN_PHASE"]
    output = Path("artifacts") / target["id"]
    output.mkdir(parents=True, exist_ok=phase == "second")
    manifest = (read_json(output / "manifest.json") if phase == "second"
                else {"target": target, "units": [], "files": {}})
    units = target["units"]
    if phase == "second":
        recorded = {item["unit"]["package"] for item in manifest["units"]}
        assert manifest["units"] and (manifest["units"][0]["unit"] == units[0] or
                                      all(unit["package"] in recorded for unit in units if unit["action"] == "build")), \
            "incomplete first chain job"
        units = [unit for unit in units[1:] if unit["package"] not in recorded]
        if not any(unit["action"] == "build" for unit in units):
            units = []
    elif target["emulated"]:
        units = units[:1]
    container = "wave-chain"
    failed = {item["unit"]["package"] for item in manifest["units"] if item["state"] != "succeeded"}
    step = "workspace"
    try:
        if units:
            Path("work").mkdir()
            shutil.copytree("keys", "work/keys")
            if target["emulated"]:
                setup_run("emulation", "sudo", "apt-get", "update")
                setup_run("emulation", "sudo", "apt-get", "install", "-y", "qemu-user-static", "binfmt-support")
                setup_run("emulation", "sudo", "update-binfmts", "--enable")
            setup_run("container", "docker", "run", "-d", "--name", container, "--platform", target["platform"],
                      "-v", str(Path("work").resolve()) + ":/work", "-v", str(output.resolve()) + ":/previous:ro",
                      target["image"], "sleep", "infinity")
            step = "dependencies"
            setup(container, target)
            for item in manifest["units"]:
                if item["state"] == "succeeded":
                    unit = item["unit"]
                    step = "restore " + unit["package"]
                    if unit["action"] == "reuse":
                        reuse(container, target, unit)
                    else:
                        paths = ["/previous/" + file for file in manifest["files"]
                                 if Path(file).parts[0] == unit["package"]]
                        install_native(container, target, paths)
        for unit in units:
            started = time.monotonic()
            state = "skipped"
            reason = ""
            if not (set(unit["declaration"]["depends_on"]) & failed):
                if unit["action"] == "build":
                    step = "callback " + unit["package"]
                    callback("unit", "started", build_id=unit["build_id"], phase="build")
                try:
                    if unit["action"] == "build":
                        files = build(container, target, unit, output)
                        manifest["files"].update(files)
                    else:
                        step = "reuse " + unit["package"]
                        reuse(container, target, unit)
                    state = "succeeded"
                except Exception as error:
                    if unit["action"] == "reuse" or isinstance(error, SetupError):
                        raise
                    state = "failed"
                    reason = str(error)
                    print(f'{unit["package"]}: {reason}', flush=True)
            if state != "succeeded":
                failed.add(unit["package"])
            manifest["units"].append({"unit": unit, "state": state, "reason": reason, "seconds": round(time.monotonic() - started, 1)})
            write_json(output / "manifest.json", manifest)
            if unit["action"] == "build" and state != "skipped":
                step = "callback " + unit["package"]
                callback("unit", state, build_id=unit["build_id"], phase="build",
                         **({"artifact": target["id"]} if state == "succeeded" else {"reason": reason}))
    except Exception as error:
        reason = str(error) if isinstance(error, SetupError) else f"setup: {step}: {(str(error) or type(error).__name__).splitlines()[0]}"
        print(reason, flush=True)
        recorded = {item["unit"]["package"] for item in manifest["units"]}
        for unit in target["units"]:
            if unit["action"] == "build" and unit["package"] not in recorded:
                manifest["units"].append({"unit": unit, "state": "failed", "reason": reason, "seconds": 0})
                write_json(output / "manifest.json", manifest)
                try:
                    callback("unit", "failed", build_id=unit["build_id"], phase="build", reason=reason)
                except Exception as report_error:
                    print(f'Report failed for {unit["build_id"]}: {type(report_error).__name__}', flush=True)
        raise
    finally:
        write_json(output / "manifest.json", manifest)
        subprocess.run(["docker", "rm", "-f", container], check=False)
    assert not failed, "failed or skipped units: " + ", ".join(sorted(failed))
    return None


if __name__ == "__main__":
    main()
