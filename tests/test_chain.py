import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import chain


class ChainTests(unittest.TestCase):
    def test_sdk_download_failure_reports_every_build_unit(self):
        units = [dict(package=name, build_id=name, action="build",
                      declaration=dict(depends_on=[], build_dependencies=dict(openwrt=[]))) for name in ("a", "b")]
        target = dict(id="target", family="ipk", distribution="openwrt", sdk=dict(url="fixture"),
                      platform="linux/amd64", image="fixture", emulated=False, units=units)
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            os.chdir(temporary)
            try:
                Path("keys").mkdir()
                with patch.dict(os.environ, TARGET=json.dumps(target), CHAIN_PHASE="first"), \
                        patch("chain.run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")), \
                        patch("chain.subprocess.run"), patch("chain.fetch", side_effect=RuntimeError("download unavailable")), \
                        patch("chain.callback") as report:
                    with self.assertRaisesRegex(RuntimeError, "setup: SDK: download unavailable"):
                        chain.main()
                self.assertEqual([(call.kwargs["build_id"], call.kwargs["reason"]) for call in report.call_args_list],
                                 [(name, "setup: SDK: download unavailable") for name in ("a", "b")])
            finally:
                os.chdir(original)
        return None

    def test_unknown_success_callback_does_not_send_a_contradictory_failure(self):
        units = [dict(package=name, build_id=name, action="build", declaration=dict(depends_on=[]))
                 for name in ("a", "b", "c")]
        target = dict(id="target", platform="linux/amd64", image="fixture", emulated=False, units=units)
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()

        def response(scope, status, **fields):
            if status == "succeeded" or fields["build_id"] == "b":
                raise RuntimeError("callback unavailable")
            return None

        with tempfile.TemporaryDirectory(dir=root) as temporary:
            os.chdir(temporary)
            try:
                Path("keys").mkdir()
                with patch.dict(os.environ, TARGET=json.dumps(target), CHAIN_PHASE="first"), \
                        patch("chain.run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")), \
                        patch("chain.subprocess.run"), patch("chain.setup"), patch("chain.build", return_value={}), \
                        patch("chain.callback", side_effect=response) as report:
                    with self.assertRaisesRegex(RuntimeError, "callback unavailable"):
                        chain.main()
                self.assertEqual([(call.args[1], call.kwargs["build_id"]) for call in report.call_args_list],
                                 [("started", "a"), ("succeeded", "a"), ("failed", "b"), ("failed", "c")])
                manifest = json.loads(Path("artifacts/target/manifest.json").read_text())
                self.assertEqual([item["state"] for item in manifest["units"]], ["succeeded", "failed", "failed"])
            finally:
                os.chdir(original)
        return None

    def test_setup_failure_reports_all_remaining_build_units_with_first_error(self):
        units = [dict(package=name, build_id=name, action=action,
                      declaration=dict(depends_on=[]))
                 for name, action in (("a", "build"), ("b", "reuse"), ("c", "build"))]
        target = dict(id="target", platform="linux/amd64", image="fixture", emulated=False, units=units)
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        for failure in ("container", "dependencies", "reuse"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=root) as temporary:
                os.chdir(temporary)
                try:
                    Path("keys").mkdir()
                    process = subprocess.CompletedProcess([], 0, stdout="", stderr="")
                    container_error = subprocess.CalledProcessError(125, ["docker"], stderr="manifest unknown\nsecond line")
                    with patch.dict(os.environ, TARGET=json.dumps(target), CHAIN_PHASE="first"), \
                            patch("chain.run", side_effect=container_error if failure == "container" else None,
                                  return_value=process), patch("chain.subprocess.run"), \
                            patch("chain.setup", side_effect=RuntimeError("repository unavailable")
                                  if failure == "dependencies" else None), \
                            patch("chain.build", return_value={}) as build, \
                            patch("chain.reuse", side_effect=RuntimeError("SDK unavailable")
                                  if failure == "reuse" else None), patch("chain.callback") as report:
                        with self.assertRaisesRegex((RuntimeError,), "setup:|unavailable"):
                            chain.main()
                    manifest = json.loads(Path("artifacts/target/manifest.json").read_text())
                    failed = [item for item in manifest["units"] if item["state"] == "failed" and item["unit"]["action"] == "build"]
                    names = ["c"] if failure == "reuse" else ["a", "c"]
                    reason = {"container": "setup: container: manifest unknown",
                              "dependencies": "setup: dependencies: repository unavailable",
                              "reuse": "setup: reuse b: SDK unavailable"}[failure]
                    self.assertEqual([item["unit"]["package"] for item in failed], names)
                    self.assertTrue(all(item["reason"] == reason for item in failed))
                    self.assertEqual([(call.kwargs["build_id"], call.kwargs["reason"])
                                      for call in report.call_args_list if call.args[1] == "failed"],
                                     [(name, reason) for name in names])
                    self.assertEqual(build.call_count, int(failure == "reuse"))
                finally:
                    os.chdir(original)
        return None

    def test_emulated_setup_failure_reports_all_units_in_one_job(self):
        units = [dict(package=name, build_id=name, action="build", declaration=dict(depends_on=[]))
                 for name in ("a", "b", "c")]
        target = dict(id="target", platform="linux/riscv64", image="fixture", emulated=True, units=units)
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            os.chdir(temporary)
            try:
                Path("keys").mkdir()
                with patch.dict(os.environ, TARGET=json.dumps(target), CHAIN_PHASE="first"), \
                        patch("chain.run", side_effect=subprocess.CalledProcessError(
                            1, ["sudo"], stderr="runner network unavailable\nmore detail")), \
                        patch("chain.subprocess.run"), patch("chain.callback") as report:
                    with self.assertRaisesRegex(RuntimeError, "setup: emulation: runner network unavailable"):
                        chain.main()
                    self.assertEqual([call.kwargs["build_id"] for call in report.call_args_list], ["a", "b", "c"])
                manifest = json.loads(Path("artifacts/target/manifest.json").read_text())
                self.assertEqual([item["state"] for item in manifest["units"]], ["failed"] * 3)
            finally:
                os.chdir(original)
        return None

    def test_failed_prerequisite_skips_dependents_but_independent_unit_builds(self):
        units = [dict(package=name, build_id=name, action="build", declaration=dict(depends_on=dependencies))
                 for name, dependencies in (("a", []), ("b", ["a"]), ("c", []), ("d", ["b", "c"]))]
        target = dict(id="target", platform="linux/amd64", image="fixture", emulated=False, units=units)
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            os.chdir(temporary)
            try:
                Path("keys").mkdir()
                with patch.dict(os.environ, TARGET=json.dumps(target), CHAIN_PHASE="first"), patch("chain.run"), \
                        patch("chain.subprocess.run"), patch("chain.setup"), patch("chain.callback") as callback, \
                        patch("chain.build", side_effect=[RuntimeError("fixture failure"), {}]) as build:
                    with self.assertRaisesRegex(AssertionError, "a, b, d"):
                        chain.main()
                self.assertEqual([call.args[2]["package"] for call in build.call_args_list], ["a", "c"])
                self.assertEqual([(call.args[1], call.kwargs["build_id"]) for call in callback.call_args_list],
                                 [("started", "a"), ("failed", "a"), ("started", "c"), ("succeeded", "c")])
                manifest = json.loads(Path("artifacts/target/manifest.json").read_text())
                self.assertEqual([item["state"] for item in manifest["units"]], ["failed", "skipped", "succeeded", "skipped"])
            finally:
                os.chdir(original)
        return None

    def test_emulated_chain_preserves_packages_callbacks_and_dependency_failures(self):
        units = [dict(package=name, build_id=name, action="reuse" if name == "b" else "build",
                      declaration=dict(depends_on=dependencies))
                 for name, dependencies in (("a", []), ("b", ["a"]), ("c", []), ("d", ["b", "c"]))]
        target = dict(id="target", family="deb", platform="linux/riscv64", image="fixture", emulated=True, units=units)
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        for failing in (False, True):
            with self.subTest(failing=failing), tempfile.TemporaryDirectory(dir=root) as temporary:
                work = Path(temporary)

                def produce(container, planned, unit, output):
                    if failing and unit["package"] == "a":
                        raise RuntimeError("fixture failure")
                    package = output / unit["package"] / (unit["package"] + ".deb")
                    package.parent.mkdir()
                    package.write_bytes(b"fixture package")
                    result = {str(package.relative_to(output)): chain.sha256(package)}
                    return result

                try:
                    with patch("chain.run"), patch("chain.subprocess.run"), patch("chain.setup"), \
                            patch("chain.callback") as callback, patch("chain.build", side_effect=produce) as build, \
                            patch("chain.reuse") as reuse, patch("chain.install_native") as install:
                        (work / "keys").mkdir()
                        os.chdir(work)
                        with patch.dict(os.environ, TARGET=json.dumps(target)):
                            if failing:
                                with self.assertRaises(AssertionError):
                                    chain.main()
                            else:
                                chain.main()
                    names = ["a", "c"] if failing else ["a", "c", "d"]
                    self.assertEqual([call.args[2]["package"] for call in build.call_args_list], names)
                    expected = ([("started", "a"), ("failed", "a"), ("started", "c"), ("succeeded", "c")]
                                if failing else [(status, name) for name in names for status in ("started", "succeeded")])
                    self.assertEqual([(call.args[1], call.kwargs["build_id"]) for call in callback.call_args_list], expected)
                    self.assertEqual(reuse.call_count, 0 if failing else 1)
                    install.assert_not_called()
                    manifest = json.loads(Path("artifacts/target/manifest.json").read_text())
                    self.assertEqual([item["state"] for item in manifest["units"]],
                                     ["failed", "skipped", "succeeded", "skipped"] if failing else ["succeeded"] * 4)
                    self.assertEqual(set(manifest["files"]), {name + "/" + name + ".deb" for name in names if not failing or name != "a"})
                    for name, digest in manifest["files"].items():
                        self.assertEqual(chain.sha256(Path("artifacts/target") / name), digest)
                finally:
                    os.chdir(original)
        return None

    def test_reuse_installs_all_binary_components_at_exact_version(self):
        target = dict(id="target", family="deb", feed_branch="feed-staging", feed_commit="f" * 40, codename="trixie")
        unit = dict(package="a", version="1.2.3", revision=4)
        ledger = [dict(target=target, unit=unit, packages=["a", "a-dev"])]
        with patch("chain.fetch", return_value=json.dumps(ledger).encode()) as fetch, patch("chain.execute") as execute:
            chain.reuse("container", target, unit)
        fetch.assert_called_once_with("https://raw.githubusercontent.com/SNodeC/Packages/" + "f" * 40 + "/publications.json")
        execute.assert_called_once_with("container", "apt-get", "install", "-y", "a=1.2.3-4~trixie", "a-dev=1.2.3-4~trixie")
        return None

    def test_no_packages_reports_empty_upload_output_and_skips_failed_dependents(self):
        units = [dict(package=name, build_id=name, action="build", declaration=dict(depends_on=depends))
                 for name, depends in (("a", []), ("b", ["a"]))]
        target = dict(id="target", emulated=False, units=units)
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            os.chdir(temporary)
            try:
                with patch.dict(os.environ, TARGET=json.dumps(target), GITHUB_OUTPUT="output"), \
                        patch("chain.build") as build, patch("chain.subprocess.run"), patch("chain.callback") as callback:
                    with self.assertRaises(FileNotFoundError):
                        chain.main()
                build.assert_not_called()
                self.assertEqual([call.kwargs["build_id"] for call in callback.call_args_list], ["a"])
                self.assertEqual(Path("output").read_text(), "packages=false\n")
            finally:
                os.chdir(original)
        return None

    def test_feed_install_uses_immutable_snapshot(self):
        target = dict(family="deb", feed_branch="feed-staging", feed_commit="f" * 40, codename="trixie")
        with patch("chain.run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as execute:
            chain.install_feed("container", target)
        self.assertIn("/" + "f" * 40 + "/apt trixie main", execute.call_args.args[-1])
        self.assertNotIn("feed-staging", execute.call_args.args[-1])
        return None


if __name__ == "__main__":
    unittest.main()
