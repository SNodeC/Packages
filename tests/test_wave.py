import datetime
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from common import callback, signature, version
from plan import prepare
from publish import release, retained


class WaveTests(unittest.TestCase):
    def test_versions(self):
        unit = {"version": "2.3.4", "revision": 17}
        for family, fields, expected in (
            ("deb", {"codename": "trixie"}, "2.3.4-17~trixie"),
            ("rpm", {"disttag": "fc44"}, "2.3.4-17.fc44"),
            ("ipk", {}, "2.3.4-r17"), ("apk", {}, "2.3.4-r17"),
        ):
            with self.subTest(family=family):
                self.assertEqual(version(unit, dict(family=family, **fields)), expected)
        return None

    def test_retention_is_per_package_and_target(self):
        entries = [dict(unit=dict(package=package, version=version, revision=revision),
                        target=dict(id=target)) for package, target, version, revision in (
            ("a", "arm", "1.0.0", 1), ("b", "arm", "1.0.0", 1),
            ("a", "x86", "1.0.0", 1), ("a", "arm", "1.0.0", 2),
            ("a", "arm", "2.0.0", 1), ("a", "arm", "2.0.0", 2),
        )]
        self.assertEqual(retained(entries), [entries[1], entries[2], entries[4], entries[5]])
        return None

    def test_release_golden(self):
        result = release("trixie", {"arm64", "amd64"},
                         {"main/binary-amd64/Packages": ("a" * 64, 42)},
                         datetime.datetime(2026, 11, 14, tzinfo=datetime.timezone.utc))
        self.assertEqual(result, "Origin: SNodeC/Packages\nLabel: SNode.C\nSuite: trixie\n"
                         "Codename: trixie\nComponents: main\nArchitectures: amd64 arm64\n"
                         "Date: Sat, 14 Nov 2026 00:00:00 GMT\nAcquire-By-Hash: yes\nSHA256:\n " +
                         "a" * 64 + "               42 main/binary-amd64/Packages\n")
        return None

    def test_coreci_verifier_vector(self):
        # Exact key/timestamp/body from core-ci main 649d0f8 tests/A_CallbackHmac.cpp.
        # Expected digest independently computed with OpenSSL HMAC-SHA256.
        body = b"phase=build&status=started&run_id=1&run_attempt=1"
        self.assertEqual(signature("key", "1000", body),
                         "sha256=d5fa3f263550df394611a7f93bae5672ce33dfd2673a78fc7cdbf4cc368d39fe")
        self.assertNotEqual(signature("key", "1000", body + b"&x=1"), signature("key", "1000", body))
        return None

    def test_empty_callback_disables_network(self):
        with patch.dict(os.environ, {"CALLBACK_URL": ""}), patch("urllib.request.urlopen") as request:
            callback("wave", "finished", conclusion="success")
        request.assert_not_called()
        return None

    def test_plan_uses_commit_declaration_and_dependency_order(self):
        declaration = dict(schema=1, package="b", depends_on=["a"], targets=["target"],
                           build_dependencies=dict(deb=[], rpm=[], openwrt=[]))
        unit = dict(package="b", repo="owner/repo", commit="a" * 40, action="build")
        plan = dict(feed_branch="feed-staging", targets=[dict(id="target", units=[unit])])
        with patch("plan.fetch", return_value=json.dumps(declaration).encode()) as request:
            with self.assertRaises(AssertionError):
                prepare(plan, {"target": dict(id="target")}, ["owner/repo"])
        request.assert_called_once_with("https://raw.githubusercontent.com/owner/repo/" + "a" * 40 + "/ci/packages.json")
        return None

    def test_reused_units_share_one_immutable_feed_snapshot(self):
        declarations = {name: dict(schema=1, package=name, depends_on=[], targets=["one", "two"],
                                  build_dependencies=dict(deb=[], rpm=[], openwrt=[]))
                        for name in ("a", "b")}
        units = [dict(package=name, repo="owner/repo", commit=name * 40, action=action)
                 for name, action in (("a", "reuse"), ("b", "build"))]
        plan = dict(feed_branch="feed-staging", targets=[dict(id=name, units=units) for name in ("one", "two")])
        responses = [json.dumps(declarations[name]).encode()
                     for name in ("a", "b", "a", "b")]
        with patch("plan.fetch", side_effect=responses), \
                patch("plan.run", return_value=Mock(stdout="f" * 40 + "\trefs/heads/feed-staging\n")) as branch:
            matrix = prepare(plan, {name: dict(id=name) for name in ("one", "two")}, ["owner/repo"])
        self.assertEqual([target["feed_commit"] for target in matrix], ["f" * 40, "f" * 40])
        branch.assert_called_once()
        self.assertEqual(branch.call_args.args[-1], "refs/heads/feed-staging")
        return None

    def test_native_declaration_still_requires_openwrt_dependency_array(self):
        declaration = dict(schema=1, package="a", depends_on=[], targets=["target"],
                           build_dependencies=dict(deb=[], rpm=[]))
        unit = dict(package="a", repo="owner/repo", commit="a" * 40, action="build")
        plan = dict(feed_branch="feed-staging", targets=[dict(id="target", units=[unit])])
        with patch("plan.fetch", return_value=json.dumps(declaration).encode()):
            with self.assertRaises(AssertionError):
                prepare(plan, {"target": dict(id="target", family="deb")}, ["owner/repo"])
        return None


if __name__ == "__main__":
    unittest.main()
