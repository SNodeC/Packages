"""Resolve a public plan and tag-bound declarations into the Actions matrix."""
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
        for unit in planned["units"]:
            assert unit["repo"] in repositories
            assert re.fullmatch(r"[0-9a-f]{40}", unit["commit"])
            declaration = json.loads(fetch(
                f'https://raw.githubusercontent.com/{unit["repo"]}/{unit["commit"]}/ci/packages.json'))
            assert declaration["schema"] == 1
            assert all(isinstance(declaration["build_dependencies"].get(family), list)
                       for family in ("deb", "rpm", "openwrt"))
            assert declaration["package"] == unit["package"]
            assert target["id"] in declaration["targets"]
            assert set(declaration["depends_on"]) <= seen
            assert unit["action"] in ("build", "reuse")
            unit["declaration"] = declaration
            seen.add(unit["package"])
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
