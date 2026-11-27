"""Resolve a public plan and repository-owned declarations into the Actions matrix."""
import json
import os
import re

from common import callback, fetch, load_plan, owns_publication, read_json, run


def prepare(plan, catalog, repositories):
    assert plan["feed_branch"] in ("feed", "feed-staging")
    feed_commit = None
    if any(unit["action"] == "reuse" for target in plan["targets"] for unit in target["units"]):
        feed_commit = run("git", "ls-remote", "--exit-code", "--heads", "origin", "refs/heads/" + plan["feed_branch"],
                          capture_output=True, text=True).stdout.split()[0]
    matrix = []
    for planned in plan["targets"]:
        target = dict(catalog[planned["id"]])
        assert any(unit["action"] == "build" for unit in planned["units"])
        seen = set()
        selected = {}
        for unit in planned["units"]:
            assert unit["repo"] in repositories
            assert all(re.fullmatch(r"[0-9a-f]{40}", unit[key]) for key in ("commit", "ci_commit"))
            declaration = json.loads(fetch(
                f'https://raw.githubusercontent.com/{unit["repo"]}/{unit["ci_commit"]}/ci/packages.json'))
            assert type(declaration["schema"]) is int and declaration["schema"] == 2
            assert all(isinstance(declaration["build_dependencies"].get(family), list)
                       for family in ("deb", "rpm", "openwrt"))
            for selector, packages in declaration["build_dependencies"].items():
                assert re.fullmatch(r"(deb|rpm|openwrt)(@[A-Za-z0-9._-]+-[A-Za-z0-9._-]+)?", selector)
                assert isinstance(packages, list) and all(isinstance(name, str) and name for name in packages)
            for hook in declaration["build"].values():
                assert re.fullmatch(r"ci/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", hook)
                assert not {".", ".."} & set(hook.split("/"))
            requirements = declaration["depends_on"]
            assert isinstance(requirements, list)
            assert all(re.fullmatch(r"(>=|>|<=|<|==)(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){0,2}"
                                    r"(,(>=|>|<=|<|==)(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){0,2})*", item["version"])
                       for item in requirements)
            assert len({item["package"] for item in requirements}) == len(requirements)
            assert declaration["package"] == unit["package"]
            assert target["id"] in declaration["targets"]
            assert {item["package"] for item in requirements} <= seen
            assert unit["dependencies"] == [dict(package=item["package"], version=selected[item["package"]]["version"],
                                                 revision=selected[item["package"]]["revision"])
                                             for item in sorted(requirements, key=lambda item: item["package"])]
            assert unit["action"] in ("build", "reuse")
            unit["declaration"] = declaration
            seen.add(unit["package"])
            selected[unit["package"]] = unit
        target["units"] = planned["units"]
        target["feed_branch"] = plan["feed_branch"]
        target["feed_commit"] = feed_commit
        matrix.append(target)
    return matrix


def publication_items(plan, catalog):
    targets = set()
    builds = set()
    for item in plan["items"]:
        assert item["target"] in catalog and item["target"] not in targets
        targets.add(item["target"])
        assert isinstance(item["artifact"], str) and item["artifact"] and item["wave"]
        assert str(item["wave_run_id"]).isdigit() and int(item["wave_run_id"]) > 0
        assert item["units"]
        for unit in item["units"]:
            assert unit["build_id"] not in builds
            builds.add(unit["build_id"])
            assert all(key in unit for key in ("package", "version", "revision"))
    assert targets
    return plan["items"]


def main():
    scope = "publication" if "PUBLICATION_ID" in os.environ else "wave"
    callback(scope, "started", run_attempt=os.environ["GITHUB_RUN_ATTEMPT"])
    plan = load_plan()
    catalog = {entry["id"]: entry for entry in read_json("ci/targets.json")["targets"]}
    matrix = (publication_items(plan, catalog) if scope == "publication" else
              prepare(plan, catalog, read_json("ci/registry.json")["repositories"]))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("matrix=" + json.dumps({"include": matrix}, separators=(",", ":")) + "\n")
        if scope == "publication":
            output.write("owned=" + str(owns_publication()).lower() + "\n")
    return None


if __name__ == "__main__":
    main()
