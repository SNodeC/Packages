import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import chain


class ChainTests(unittest.TestCase):
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

    def test_split_handoff_preserves_packages_callbacks_and_dependency_failures(self):
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
                        for phase in ("first", "second"):
                            workspace = work / phase
                            (workspace / "keys").mkdir(parents=True)
                            if phase == "second":
                                shutil.copytree(work / "first/artifacts", workspace / "artifacts")
                            os.chdir(workspace)
                            with patch.dict(os.environ, TARGET=json.dumps(target), CHAIN_PHASE=phase):
                                if failing:
                                    with self.assertRaises(AssertionError):
                                        chain.main()
                                else:
                                    chain.main()
                            if phase == "first":
                                self.assertEqual(len(json.loads(Path("artifacts/target/manifest.json").read_text())["units"]), 1)
                    names = ["a", "c"] if failing else ["a", "c", "d"]
                    self.assertEqual([call.args[2]["package"] for call in build.call_args_list], names)
                    expected = ([("started", "a"), ("failed", "a"), ("started", "c"), ("succeeded", "c")]
                                if failing else [(status, name) for name in names for status in ("started", "succeeded")])
                    self.assertEqual([(call.args[1], call.kwargs["build_id"]) for call in callback.call_args_list], expected)
                    self.assertEqual(reuse.call_count, 0 if failing else 1)
                    self.assertEqual([call.args[2] for call in install.call_args_list],
                                     [] if failing else [["/previous/a/a.deb"]])
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

    def test_incomplete_handoff_does_not_repeat_unknown_builds(self):
        target = dict(id="target", emulated=True, units=[{}, {}])
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            os.chdir(temporary)
            try:
                output = Path("artifacts/target")
                output.mkdir(parents=True)
                chain.write_json(output / "manifest.json", dict(target=target, units=[], files={}))
                with patch.dict(os.environ, TARGET=json.dumps(target), CHAIN_PHASE="second"), \
                        patch("chain.build") as build, patch("chain.callback") as callback:
                    with self.assertRaisesRegex(AssertionError, "incomplete first chain job"):
                        chain.main()
                build.assert_not_called()
                callback.assert_not_called()
            finally:
                os.chdir(original)
        return None

    def test_feed_install_uses_immutable_snapshot(self):
        target = dict(family="deb", feed_branch="feed-staging", feed_commit="f" * 40, codename="trixie")
        with patch("chain.execute") as execute:
            chain.install_feed("container", target)
        self.assertIn("/" + "f" * 40 + "/apt trixie main", execute.call_args.args[-1])
        self.assertNotIn("feed-staging", execute.call_args.args[-1])
        return None


if __name__ == "__main__":
    unittest.main()
