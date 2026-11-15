"""Resolve a public plan and tag-bound declarations into the Actions matrix."""
import base64
import json
import os
import re

from common import callback, fetch, read_json, write_json


def prepare(plan, catalog, repositories):
    assert plan["feed_branch"] in ("feed", "feed-staging")
    feed_commit = None
    if any(unit["action"] == "reuse" for target in plan["targets"] for unit in target["units"]):
        feed_commit = json.loads(fetch(
            "https://api.github.com/repos/SNodeC/Packages/commits/" + plan["feed_branch"]))["sha"]
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


def main():
    inline = os.environ.get("PLAN_JSON", "")
    raw = base64.b64decode(inline, validate=True) if inline else fetch(os.environ["PLAN_URL"])
    plan = json.loads(raw)
    assert str(plan["wave"]) == os.environ["WAVE_ID"]
    assert plan["feed_branch"] == os.environ["FEED_BRANCH"]
    catalog = {entry["id"]: entry for entry in read_json("ci/targets.json")["targets"]}
    matrix = prepare(plan, catalog, read_json("ci/registry.json")["repositories"])
    write_json("plan.json", plan)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("matrix=" + json.dumps({"include": matrix}, separators=(",", ":")) + "\n")
    callback("wave", "started", run_id=os.environ["GITHUB_RUN_ID"],
             run_attempt=os.environ["GITHUB_RUN_ATTEMPT"])
    return None


if __name__ == "__main__":
    main()
