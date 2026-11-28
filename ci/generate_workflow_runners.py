"""Render the runner allowlist used before GitHub Actions assigns a runner."""

import argparse
import json
from pathlib import Path
import re


def routes(catalog):
    configured = catalog["runners"]
    if {target["runner"] for target in catalog["targets"]} != configured.keys() or not configured:
        raise ValueError("catalog runners must match the labels used by its targets")
    prefixes = set()
    checks = []
    choices = []
    for label, runner in sorted(configured.items()):
        prefix = runner["request_prefix"]
        if (not re.fullmatch(r"[A-Za-z0-9._-]+", label) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", prefix)
                or prefix in prefixes or not re.fullmatch(r"[A-Z0-9]+", runner["runner_arch"])):
            raise ValueError(f"invalid catalog runner route {label!r}")
        prefixes.add(prefix)
        condition = f"startsWith(inputs.request_id, '{prefix}.')"
        checks.append(condition)
        choices.append(f"{condition} && '{label}'")
    return (f"    if: ${{{{ {' || '.join(checks)} }}}}\n"
            f"    runs-on: ${{{{ {' || '.join(choices)} }}}}\n")


def smoke_route(catalog):
    runners = json.dumps({target["id"]: target["runner"] for target in catalog["targets"]}, separators=(",", ":"))
    defaults = json.dumps(catalog["smoke_targets"], separators=(",", ":"))
    fallback = next(target["runner"] for target in catalog["targets"] if target["id"] == catalog["smoke_targets"]["deb"])
    return (f"    runs-on: ${{{{ fromJSON('{runners}')[inputs.target || "
            f"fromJSON('{defaults}')[inputs.family]] || '{fallback}' }}}}\n")


def update(workflow, route, check):
    text = workflow.read_text()
    before, marker, rest = text.partition("    # BEGIN catalog runner routes\n")
    _, end, after = rest.partition("    # END catalog runner routes\n")
    if not marker or not end:
        raise ValueError(f"missing runner route markers in {workflow.name}")
    generated = before + marker + route + end + after
    if check and generated != text:
        raise SystemExit(f"{workflow.name} runner routes are stale; run python3 ci/generate_workflow_runners.py")
    if not check:
        workflow.write_text(generated)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "ci/targets.json").read_text())
    for name, route in (("build", routes(catalog)), ("smoke", smoke_route(catalog))):
        update(root / f".github/workflows/{name}.yml", route, options.check)
    return None


if __name__ == "__main__":
    main()
