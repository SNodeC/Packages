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
        if (not re.fullmatch(r"[A-Za-z0-9._-]+", label) or not re.fullmatch(r"[a-z0-9-]{1,24}", prefix)
                or prefix in prefixes or not re.fullmatch(r"[A-Z0-9]+", runner["runner_arch"])):
            raise ValueError(f"invalid catalog runner route {label!r}")
        prefixes.add(prefix)
        condition = f"startsWith(inputs.request_id, '{prefix}.')"
        checks.append(condition)
        choices.append(f"{condition} && '{label}'")
    return (f"    if: ${{{{ {' || '.join(checks)} }}}}\n"
            f"    runs-on: ${{{{ {' || '.join(choices)} }}}}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "ci/targets.json").read_text())
    workflow = root / ".github/workflows/build.yml"
    text = workflow.read_text()
    before, marker, rest = text.partition("    # BEGIN catalog runner routes\n")
    _, end, after = rest.partition("    # END catalog runner routes\n")
    if not marker or not end:
        raise ValueError("missing runner route markers in build.yml")
    generated = before + marker + routes(catalog) + end + after
    if options.check and generated != text:
        raise SystemExit("build.yml runner routes are stale; run python3 ci/generate_workflow_runners.py")
    if not options.check:
        workflow.write_text(generated)
    return None


if __name__ == "__main__":
    main()
