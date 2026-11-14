import json
import os
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
        target = dict(id="target", platform="linux/amd64", image="fixture", units=units)
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            os.chdir(temporary)
            try:
                Path("keys").mkdir()
                with patch.dict(os.environ, TARGET=json.dumps(target)), patch("chain.run"), \
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

    def test_reuse_installs_all_binary_components_at_exact_version(self):
        target = dict(id="target", family="deb", feed_branch="feed-staging", codename="trixie")
        unit = dict(package="a", version="1.2.3", revision=4)
        ledger = [dict(target=target, unit=unit, packages=["a", "a-dev"])]
        with patch("chain.fetch", return_value=json.dumps(ledger).encode()), patch("chain.execute") as execute:
            chain.reuse("container", target, unit)
        execute.assert_called_once_with("container", "apt-get", "install", "-y", "a=1.2.3-4~trixie", "a-dev=1.2.3-4~trixie")
        return None


if __name__ == "__main__":
    unittest.main()
