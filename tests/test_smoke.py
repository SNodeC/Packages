import json
import sys
import tempfile
import unittest
from contextlib import chdir, nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))
import smoke


class SmokeTests(unittest.TestCase):
    def setUp(self):
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        self.enterContext(chdir(self.enterContext(tempfile.TemporaryDirectory(dir=root))))
        self.catalog = json.loads((ROOT / "ci/targets.json").read_text())
        return None

    def test_family_choices_and_exact_target(self):
        for family in smoke.FAMILIES:
            target = smoke.select_target(self.catalog, family, "")
            self.assertEqual(target["family"], family)
            self.assertIn(target["arch"], ("x86_64", "amd64"))
        selected = smoke.select_target(self.catalog, "", "raspberrypios-trixie-arm64")
        self.assertEqual(selected["arch"], "arm64")
        for family, target in (("", ""), ("deb", "debian-trixie-amd64"), ("", "unknown")):
            with self.assertRaises(ValueError):
                smoke.select_target(self.catalog, family, target)
        return None

    def test_resolves_numeric_stable_tags_and_pins_the_declaration(self):
        repos = "fixture/repo"
        source, old, newer, annotation, override = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
        refs = "\n".join(f"{sha}\t{ref}" for sha, ref in (
            (source, "HEAD"), (old, "refs/tags/v1.9.0"), (annotation, "refs/tags/v1.10.0"),
            (newer, "refs/tags/v1.10.0^{}"), (annotation, "refs/tags/v1.11.0-rc1"),
            (annotation, "refs/tags/v2.0.0"), (override, "refs/heads/fix/ci"),
        ))
        with patch("smoke.capture", return_value=refs), patch("smoke.fetch") as fetch, \
                patch("smoke.read_json", return_value=dict(schema=2, package="fixture", releases=">=1,<2")):
            unit, _ = smoke.resolve(repos, "", Path("."))
            self.assertEqual((unit["version"], unit["commit"], unit["ci_commit"]), ("1.10.0", newer, source))
            self.assertIn(source, fetch.call_args.args[0])
            unit, _ = smoke.resolve(repos, "fix/ci", Path("."))
            self.assertEqual((unit["commit"], unit["ci_commit"]), (override, override))
        return None

    def test_build_smoke_reuses_container_and_skips_failed_dependencies(self):
        target = smoke.select_target(self.catalog, "deb", "")
        successful = "fixtures/success"
        producer = "fixtures/producer"
        dependent = "fixtures/dependent"
        repos = [dependent, producer, successful]

        def resolve(repository, override, logs):
            package = repository.split("/")[1]
            dependencies = [dict(package="producer", version=">=1")] if repository == dependent else []
            return (dict(repo=repository, package=package, version="1.0.0", revision=1),
                    dict(targets=[target["id"]], build=dict(deb="ci/build.sh"), depends_on=dependencies,
                         build_dependencies={}))

        def run_hook(unit, target, action, dependencies):
            if unit["repo"] == producer:
                raise ValueError("fixture failure")
            directory = Path("work/output") / unit["package"]
            directory.mkdir(parents=True)
            (directory / "one.deb").write_bytes(b"fixture package")
            return None

        with patch("smoke.resolve", side_effect=resolve), patch("build.container", return_value=nullcontext()) as container, \
                patch("build.install"), patch("build.checkout"), patch("build.hook", side_effect=run_hook) as hook:
            self.assertFalse(smoke.smoke(target, repos, {}))
        container.assert_called_once_with(target)
        self.assertEqual([call.args[0]["repo"] for call in hook.call_args_list], [producer, successful])
        report = json.loads(Path("artifact/summary.json").read_text())
        self.assertEqual([(entry["repository"], entry["status"]) for entry in report["repositories"]],
                         [(dependent, "skipped"), (producer, "failed"), (successful, "ok")])
        self.assertEqual(Path("artifact/success/one.deb").read_bytes(), b"fixture package")
        return None

    def test_consumer_installs_outputs_from_current_job(self):
        target = smoke.select_target(self.catalog, "rpm", "")
        repos = ["fixtures/consumer", "fixtures/library"]

        def resolve(repository, override, logs):
            package = repository.split("/")[1]
            dependencies = [dict(package="library", version="==2")] if package == "consumer" else []
            return (dict(repo=repository, package=package, version="2.0.0", revision=1),
                    dict(targets=[target["id"]], build=dict(rpm="ci/build.sh"), depends_on=dependencies,
                         build_dependencies={}))

        def run_hook(unit, target, action, dependencies):
            directory = Path("work/output") / unit["package"]
            directory.mkdir(parents=True)
            (directory / "output.rpm").write_bytes(b"fixture package")
            return None

        with patch("smoke.resolve", side_effect=resolve), patch("build.container", return_value=nullcontext()), \
                patch("build.install") as install, patch("build.checkout"), patch("build.hook", side_effect=run_hook) as hook:
            self.assertTrue(smoke.smoke(target, repos, {}))
        self.assertEqual([call.args[0]["repo"] for call in hook.call_args_list], repos[::-1])
        self.assertEqual(install.call_args.args, ("rpm", ["/work/output/library/output.rpm"]))
        return None

    def test_excerpt_keeps_both_diagnostics_and_final_build_context(self):
        path = Path("build.log")
        diagnostic = "nlohmann_json 3.12.0 ≠ 3.11.2 expected by ci/"
        path.write_text(diagnostic + "\n" + "configure probe not found\n" * 40 + "make: Error 2\n")
        result = smoke.excerpt(path)
        self.assertEqual(result[0], diagnostic)
        self.assertEqual(result[-1], "make: Error 2")
        return None
