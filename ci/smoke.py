"""Run repository package hooks for one catalog target without the scheduler."""

import argparse
from collections import deque
from contextlib import contextmanager
from graphlib import CycleError, TopologicalSorter
import json
import operator
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build
from common import COMMIT, NAME, capture, fetch, matches, read_json, require, resolved_packages

FAMILIES = ("deb", "rpm", "ipk", "apk")
STABLE = r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
OPERATORS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq}
ERRORS = (OSError, ValueError, KeyError, subprocess.CalledProcessError)


def select_target(catalog, family, identifier):
    require(bool(family) != bool(identifier), "set exactly one of family or target")
    if family:
        require(family in FAMILIES, f"unknown family: {family}")
        identifier = catalog["smoke_targets"][family]
    target = next((entry for entry in catalog["targets"] if entry["id"] == identifier), None)
    require(target and target["runner"] in catalog["runners"], f"unknown smoke target: {identifier}")
    if family:
        require(target["family"] == family and target["arch"] in ("amd64", "x86_64"),
                f"invalid catalog smoke target for {family}")
    return target


def stable_tags(refs):
    tags = {}
    for ref, commit in refs.items():
        name = ref.removeprefix("refs/tags/")
        match = re.fullmatch(STABLE, name)
        if match and ref.startswith("refs/tags/"):
            tags[tuple(map(int, match.groups()))] = (name, refs.get(ref + "^{}", commit))
    return tags


def in_range(version, requirement):
    compatible = True
    for clause in requirement.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|>|<)?\s*([0-9]+(?:\.[0-9]+){0,2})\s*", clause)
        require(match, f"invalid release range: {requirement}")
        operation, bound = match.groups()
        parts = tuple(map(int, bound.split(".")))
        compatible = compatible and OPERATORS[operation or "=="](version, parts + (0,) * (3 - len(parts)))
    return compatible


def resolve(repository, override, directory):
    remote = f"https://github.com/{repository}.git"
    refs = dict((ref, sha) for sha, ref in
                (line.split("\t") for line in capture("git", "ls-remote", remote, "HEAD", "refs/heads/*", "refs/tags/*").splitlines()))
    ci_commit = refs["HEAD"]
    if override:
        candidates = [refs[key] for key in (override, "refs/tags/" + override, "refs/heads/" + override) if key in refs]
        require(len(candidates) == 1 or not candidates and matches(COMMIT, override),
                f"unknown or ambiguous ref: {override}")
        ci_commit = candidates[0] if candidates else override
        ci_commit = refs.get(override + "^{}", refs.get("refs/tags/" + override + "^{}", ci_commit))
    declaration_path = directory / (repository.replace("/", "--") + ".json")
    fetch(f"https://raw.githubusercontent.com/{repository}/{ci_commit}/ci/packages.json", declaration_path)
    declaration = read_json(declaration_path)
    require(declaration["schema"] == 2 and matches(NAME, declaration["package"]), "invalid declaration")
    versions = stable_tags(refs)
    compatible = [version for version in versions if in_range(version, declaration["releases"])]
    require(compatible, "no stable tag satisfies releases")
    explicit_tag = re.fullmatch(STABLE, override.removeprefix("refs/tags/"))
    version = tuple(map(int, explicit_tag.groups())) if explicit_tag else max(compatible)
    require(version in compatible, "requested tag is outside releases")
    _, source_commit = versions[version]
    unit = dict(repo=repository, package=declaration["package"], version=".".join(map(str, version)), revision=1,
                commit=ci_commit if override else source_commit, ci_commit=ci_commit)
    return unit, declaration


def dependencies(declaration, units, outcomes):
    upstream = []
    for dependency in declaration["depends_on"]:
        package = dependency["package"]
        require(package in units, f"dependency {package} was excluded or has no declaration")
        producer = units[package][0]
        require(outcomes.get(producer["repo"], {}).get("status") == "ok", f"dependency {package} did not build")
        version = tuple(map(int, producer["version"].split(".")))
        require(in_range(version, dependency["version"]), f"dependency {package} does not satisfy {dependency['version']}")
        upstream.append(producer)
    return upstream


@contextmanager
def capture_build(path):
    sys.stdout.flush()
    sys.stderr.flush()
    stdout, stderr = os.dup(1), os.dup(2)
    try:
        with path.open("ab") as log:
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(stdout, 1)
        os.dup2(stderr, 2)
        os.close(stdout)
        os.close(stderr)
    return None


def excerpt(path):
    important = deque(maxlen=16)
    tail = deque(maxlen=24)
    if path.exists():
        with path.open(errors="replace") as log:
            for line in log:
                text = line.rstrip()[:800]
                tail.append(text)
                if re.search(r"error:|AssertionError|FileNotFoundError|tests FAILED|Subprocess aborted|\(Failed\)|"
                             r"expected by ci/|No rule to make target|undefined reference|glob failed", text, re.I):
                    important.append(text)
    return list(dict.fromkeys([*important, *tail]))


def outcome(outcomes, repository, status, reason="", log=None):
    outcomes[repository] = dict(repository=repository, status=status, reason=reason, log=excerpt(log) if log else [])
    print(f"{repository}: {status} {reason}", flush=True)
    for line in outcomes[repository]["log"]:
        print(f"  | {line}", flush=True)
    return None


def collect_outputs(unit, family, artifact):
    package = unit["package"]
    outputs = build.package_outputs(package, family)
    destination = artifact / package
    destination.mkdir()
    for path in outputs:
        shutil.copyfile(path, destination / path.name)
    return ["/" + str(path) for path in outputs]


def build_selected(target, units, outcomes, artifact, logs):
    family = target["family"]
    graph = {package: [entry["package"] for entry in declaration["depends_on"] if entry["package"] in units]
             for package, (_, declaration) in units.items()}
    order = list(TopologicalSorter(graph).static_order())
    outputs = {}
    with build.container(target):
        if family in build.OPENWRT:
            build.openwrt_setup(target)
            feeds = set()
            for package in order:
                unit, declaration = units[package]
                log = logs / package
                if unit["repo"] not in outcomes:
                    try:
                        with capture_build(log):
                            build.checkout(unit)
                            build.hook(unit, target, declaration["build"]["openwrt_register"],
                                       [build.identity(units[key][0]) for key in graph[package]])
                        feeds.update(resolved_packages(declaration, target))
                    except ERRORS as error:
                        outcome(outcomes, unit["repo"], "failed", f"registration: {error}", log)
            build.prepare_feeds(feeds)
        for package in order:
            unit, declaration = units[package]
            repository = unit["repo"]
            log = logs / package
            if repository not in outcomes:
                try:
                    upstream = dependencies(declaration, units, outcomes)
                except ValueError as error:
                    outcome(outcomes, repository, "skipped", str(error))
                else:
                    try:
                        with capture_build(log):
                            paths = [path for producer in upstream for path in outputs[producer["package"]]]
                            if family in build.OPENWRT:
                                if paths:
                                    build.openwrt_install(target, paths)
                            else:
                                build.install(family, resolved_packages(declaration, target) + paths)
                                build.checkout(unit)
                            action = "openwrt" if family in build.OPENWRT else family
                            build.hook(unit, target, declaration["build"][action], [build.identity(producer) for producer in upstream])
                            outputs[package] = collect_outputs(unit, family, artifact)
                        outcome(outcomes, repository, "ok")
                    except ERRORS as error:
                        outcome(outcomes, repository, "failed", str(error), log)
    return None


def smoke(target, repositories, overrides):
    artifact = Path("artifact")
    artifact.mkdir()
    logs = Path("smoke-work")
    logs.mkdir()
    outcomes, units = {}, {}
    try:
        for repository in repositories:
            try:
                unit, declaration = resolve(repository, overrides.get(repository, ""), logs)
                package = unit["package"]
                require(package not in units, f"duplicate package {package}")
                units[package] = unit, declaration
                if target["id"] not in declaration["targets"]:
                    outcome(outcomes, repository, "skipped", "target is not declared")
                else:
                    required = ("openwrt", "openwrt_register") if target["family"] in build.OPENWRT else (target["family"],)
                    require(all(declaration["build"].get(key) for key in required), f"no {target['family']} hook declared")
            except ERRORS as error:
                outcome(outcomes, repository, "failed", str(error))
        if len(outcomes) < len(repositories):
            build_selected(target, units, outcomes, artifact, logs)
    except (*ERRORS, CycleError) as error:
        for repository in repositories:
            if repository not in outcomes:
                outcome(outcomes, repository, "failed", f"shared setup or graph: {error}")
    results = [outcomes[repository] for repository in repositories]
    (artifact / "summary.json").write_text(json.dumps(dict(target=target["id"], repositories=results), indent=2) + "\n")
    return not any(result["status"] == "failed" for result in results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", default="")
    parser.add_argument("--target", default="")
    options = parser.parse_args()
    catalog = read_json(ROOT / "ci/targets.json")
    target = select_target(catalog, options.family, options.target)
    require(catalog["runners"][target["runner"]]["runner_arch"] == os.environ["RUNNER_ARCH"], "runner architecture mismatch")
    allowed = read_json(ROOT / "ci/registry.json")["repositories"]
    repositories = [value.strip() for value in os.environ.get("SMOKE_REPOSITORIES", "").split(",") if value.strip()] or allowed
    overrides = json.loads(os.environ.get("SMOKE_REFS") or "{}")
    require(len(repositories) == len(set(repositories)) and set(repositories) <= set(allowed),
            "repository is outside the registry or duplicated")
    require(isinstance(overrides, dict) and overrides.keys() <= set(repositories) and
            all(isinstance(ref, str) and re.fullmatch(r"[A-Za-z0-9._/-]+", ref) for ref in overrides.values()),
            "invalid repository ref overrides")
    return int(not smoke(target, repositories, overrides))


if __name__ == "__main__":
    sys.exit(main())
