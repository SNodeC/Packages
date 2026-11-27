"""Execute one frozen build request in its catalog environment."""

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from common import (COMMIT, DIGEST, FAMILIES, NAME, NATIVE_NAME, digest, fetch, matches, positive, read_json,
                    release, require, resolved_packages, run, write_json)
from metadata import metadata

WORK = Path("work")
CONTAINER = "package-build"
SDK = "/work/sdk"
TOOLS = SDK + "/staging_dir/host/bin/"
OPENWRT = {"ipk", "apk"}


def docker(*args, **kwargs):
    result = run("docker", "exec", CONTAINER, *args, **kwargs)
    return result


def builder(*args):
    docker("runuser", "-u", "builder", "--", *args)
    return None


def identity(unit):
    result = {key: unit[key] for key in ("package", "version", "revision")}
    return result


def check_request(request):
    inputs = request["inputs"]
    target = inputs["target"]
    require(request["request"] == os.environ["REQUEST_ID"] and request["attempt"] == int(os.environ["ATTEMPT"]),
            "request envelope mismatch")
    require(inputs["contract"] == "Xv2" and matches(COMMIT, inputs["feed_commit"]), "invalid contract or feed snapshot")
    require(target["family"] in FAMILIES and matches(r"[^\s@]+@sha256:[0-9a-f]{64}", target["image"]),
            "invalid build environment")
    require(all(matches(NAME, target[key]) for key in ("id", "codename", "distribution", "release", "arch")),
            "invalid target")
    expected_arch = {"ubuntu-24.04": "X64", "ubuntu-24.04-arm": "ARM64"}
    require(os.environ["RUNNER_ARCH"] == expected_arch[target["runner"]], "runner does not match frozen target")
    seen = set()
    for unit in [*inputs["closure"], inputs]:
        package = unit["package"]
        require(matches(NAME, package) and package not in seen and positive(unit["revision"]), "invalid package or revision")
        require(matches(r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){2}", unit["version"]), "invalid release")
        repo = unit.get("repository", unit.get("repo"))
        require(matches(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", repo) and
                all(matches(COMMIT, unit[key]) for key in ("commit", "ci_commit")), "invalid repository or commit")
        require(all(binding["package"] in seen for binding in unit.get("bindings", [])), "closure is not topological")
        seen.add(package)
    return inputs


def install(family, packages):
    if packages:
        require(all(isinstance(p, str) and p and not p.startswith("-") for p in packages), "invalid package argument")
        if family == "rpm":
            docker("dnf", "install", "-y", "--allowerasing", *packages)
        else:
            docker("env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *packages)
    return None


def checkout(unit):
    package = unit["package"]
    repo = "https://github.com/" + unit.get("repository", unit.get("repo")) + ".git"
    source = WORK / "source" / package
    run("git", "init", "-q", str(source))
    git = ("git", "-C", str(source))
    run(*git, "fetch", "--depth=1", repo, unit["commit"])
    run(*git, "checkout", "--detach", "FETCH_HEAD")
    run(*git, "submodule", "update", "--init", "--recursive", "--depth=1")
    run(*git, "fetch", "--depth=1", repo, unit["ci_commit"])
    ci = WORK / "ci" / package
    ci.mkdir(parents=True)
    archive = run(*git, "archive", unit["ci_commit"] + ":ci", capture_output=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as files:
        files.extractall(ci, filter="data")
    return None


def base_setup(target):
    family = target["family"]
    if family == "rpm":
        install(family, ["ca-certificates", "git", "jq", "rpm-build", "gnupg2", "file", "shadow-utils", "util-linux"])
    else:
        docker("apt-get", "update")
        install(family, ["ca-certificates", "curl", "git", "jq", "gnupg", "dpkg-dev", "file", "passwd", "util-linux"])
    for command in target.get("repository_setup", []):
        docker(*command)
    install(family, target.get("host_packages", []))
    docker("useradd", "--uid", str(os.getuid()), "--create-home", "builder")
    return None


def unpack_sdk(target, directory):
    sdk = target["sdk"]
    archive = directory.with_suffix(".tar.zst")
    fetch(sdk["url"], archive)
    require(digest(archive) == sdk["sha256"], "SDK SHA256 mismatch")
    directory.mkdir()
    run("tar", "--zstd", "-xf", str(archive), "-C", str(directory), "--strip-components=1")
    archive.unlink()
    return None


def openwrt_setup(target):
    directory = WORK / "sdk"
    unpack_sdk(target, directory)
    (directory / "feeds.conf").write_text("\n".join(target["sdk"]["feeds"]) + "\n")
    (directory / ".config").write_text("CONFIG_ALL_NONSHARED=n\nCONFIG_ALL_KMODS=n\nCONFIG_SIGNED_PACKAGES=n\n")
    return None


def exact_dependencies(inputs):
    target = inputs["target"]
    family = target["family"]
    native = family not in OPENWRT
    base = "https://raw.githubusercontent.com/" + os.environ["GITHUB_REPOSITORY"] + "/" + inputs["feed_commit"]
    locked = WORK / "locked"
    locked.mkdir()
    packages = {}
    for binding in inputs["closure"]:
        require(bool(binding["inventory"]), "missing binding inventory")
        for entry in binding["inventory"]:
            name = entry["name"]
            path = entry["path"]
            checksum = entry["served_sha256"]
            require(matches(NATIVE_NAME, name) and matches(DIGEST, checksum) and
                    all(matches(NATIVE_NAME, part) for part in path.split("/")) and
                    path.endswith("." + family) and name not in packages, "invalid inventory")
            local = locked / Path(path).name
            require(not local.exists(), "duplicate inventory filename")
            fetch(base + "/" + path, local)
            require(digest(local) == checksum, "snapshot package digest mismatch")
            packages[name] = entry, "/work/locked/" + local.name
    paths = [path for _, path in packages.values()]
    if packages:
        if native:
            install(family, paths)
        else:
            openwrt_install(target, paths)
    for name, (entry, _) in packages.items():
        if family == "deb":
            command = ("dpkg-query", "-W", "-f=${Version}\t${Architecture}", name + ":" + entry["arch"])
        elif family == "rpm":
            command = ("rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}\t%{ARCH}", name + "." + entry["arch"])
        elif family == "ipk":
            command = (TOOLS + "opkg", "-f", "/work/opkg.conf", "--offline-root", "/work/root", "status", name)
        else:
            command = (TOOLS + "apk", "--root", "/work/root", "query", "--installed",
                       "--fields", "name,version,arch", "--format", "json", name)
        installed = docker(*command, capture_output=True, text=True).stdout
        if native:
            valid = installed.strip() == entry["version"] + "\t" + entry["arch"]
        elif family == "ipk":
            valid = (f'Version: {entry["version"]}\n' in installed and
                     f'Architecture: {entry["arch"]}\n' in installed and " installed" in installed)
        else:
            valid = dict(name=name, version=entry["version"], arch=entry["arch"]) in json.loads(installed)
        require(valid, f"installed identity mismatch: {name}")
    return None


def openwrt_install(target, paths):
    # The offline root records exact package-manager state. SDK development staging is done by the recipes.
    (WORK / "root").mkdir(exist_ok=True)
    repos = target["sdk"]["runtime_repositories"]
    if target["family"] == "ipk":
        conf = WORK / "opkg.conf"
        lines = [f'arch all 1\narch {target["arch"]} 10', "dest root /"]
        lines.extend(f"src/gz release{i} {url}" for i, url in enumerate(repos))
        conf.write_text("\n".join(lines) + "\n")
        opkg = (TOOLS + "opkg", "-f", "/work/opkg.conf", "--offline-root", "/work/root")
        docker(*opkg, "update")
        docker(*opkg, "install", *paths)
    else:
        (WORK / "apk-repositories").write_text("\n".join(repos) + "\n")
        docker(TOOLS + "apk", "--root", "/work/root", "--arch", target["arch"], "--initdb", "--allow-untrusted",
               "--repositories-file", "/work/apk-repositories", "add", "--force-non-repository", "--no-scripts", *paths)
    return None


def hook(unit, target, action, dependencies):
    package = unit["package"]
    args = ["env", "SOURCE_DIR=/work/source/" + package, "CI_DIR=/work/ci/" + package,
            "DEPENDENCIES_JSON=" + json.dumps(dependencies), "OUTPUT_DIR=/work/output/" + package,
            "SDK_DIR=" + SDK, "TARGET_JSON=" + json.dumps(target),
            "PACKAGE_VERSION=" + unit["version"], "PACKAGE_RELEASE=" + release(unit, target)]
    candidate = (WORK / "ci" / package / action.removeprefix("ci/")).resolve()
    require(action.startswith("ci/") and candidate.is_file() and
            candidate.is_relative_to((WORK / "ci" / package).resolve()), "invalid hook path")
    builder(*args, "sh", "/work/ci/" + package + "/" + action.removeprefix("ci/"))
    return None


def build(inputs):
    target = inputs["target"]
    family = target["family"]
    openwrt = family in OPENWRT
    root = inputs["package"]
    closure = inputs["closure"]
    declarations = {}
    packages = {unit["package"]: unit for unit in closure}
    dependencies = {}

    def declare(unit):
        package = unit["package"]
        repo = unit["repository"] if unit is not inputs else unit["repo"]
        path = WORK / (package + ".json")
        fetch(f'https://raw.githubusercontent.com/{repo}/{unit["ci_commit"]}/ci/packages.json', path)
        declared = read_json(path)
        require(declared["schema"] == 2 and declared["package"] == package, "frozen declaration mismatch")
        declarations[package] = declared
        dependencies[package] = [identity(packages[item["package"]]) for item in declared["depends_on"]]
        return None

    WORK.mkdir()
    (WORK / "output").mkdir()
    if target["emulated"]:
        run("sudo", "apt-get", "update")
        run("sudo", "apt-get", "install", "-y", "qemu-user-static", "binfmt-support")
        run("sudo", "update-binfmts", "--enable")
    run("docker", "run", "--rm", "-d", "--name", CONTAINER, "--platform", target["platform"],
        "-v", str(WORK.resolve()) + ":/work", target["image"], "sleep", "infinity")
    try:
        base_setup(target)
        declare(inputs)
        target_packages = resolved_packages(declarations[root], target)
        if openwrt:
            openwrt_setup(target)
            for unit in [*closure, inputs]:
                package = unit["package"]
                if package != root:
                    declare(unit)
                checkout(unit)
                hook(unit, target, "ci/package/openwrt-register.sh", dependencies[package])
            feeds = sorted({p for declared in declarations.values() for p in resolved_packages(declared, target)})
            builder("sh", "-ec", 'cd /work/sdk; ./scripts/feeds update -a; ./scripts/feeds install "$@"', "sh", *feeds)
            exact_dependencies(inputs)
            for unit in closure:
                package = unit["package"]
                hook(unit, target, declarations[package]["build"]["openwrt"], dependencies[package])
        else:
            install(family, target_packages)
            exact_dependencies(inputs)
            checkout(inputs)
        hook(inputs, target, declarations[root]["build"]["openwrt" if openwrt else family], dependencies[root])
        path = WORK / "output" / root
        outputs = sorted(path.glob("*." + family))
        require(outputs and all(output.is_file() and not output.is_symlink() for output in outputs),
                "missing or invalid hook output")
        if openwrt:
            openwrt_install(target, ["/" + str(output) for output in outputs])
        artifact = Path("artifact")
        artifact.mkdir()
        inventory = []
        for output in outputs:
            record = metadata(family, output, in_container=docker, apk=TOOLS + "apk")
            record.update(file=output.name, sha256=digest(output))
            inventory.append(record)
            shutil.copyfile(output, artifact / output.name)
        write_json(artifact / "inventory.json", inventory)
        write_json(artifact / "manifest.json", dict(unit=identity(inputs), target=target["id"],
                   request=os.environ["REQUEST_ID"], attempt=int(os.environ["ATTEMPT"]),
                   files={entry["file"]: entry["sha256"] for entry in inventory}))
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], check=False)
    return None


if __name__ == "__main__":
    build(check_request(read_json(".document")))
