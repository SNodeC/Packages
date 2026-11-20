import datetime
import json
import os
import runpy
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from common import callback, owns_publication, signature, version
from plan import prepare, publication_items
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
        # Exact key/timestamp/body from core-ci 4ee51e7 tests/A_CallbackHmac.cpp.
        # Expected digest independently computed with OpenSSL HMAC-SHA256.
        body = b"scope=unit&build_id=a%7Et&phase=build&status=started&run_id=42"
        self.assertEqual(signature("key", "1000", body),
                         "sha256=de6e84d9b3c954395b93ee316d046f31f59741249a9958e2a98c5f081fb4226f")
        self.assertNotEqual(signature("key", "1000", body + b"&x=1"), signature("key", "1000", body))
        return None

    def test_empty_callback_disables_network(self):
        with patch.dict(os.environ, {"CALLBACK_URL": ""}), patch("urllib.request.urlopen") as request:
            callback("wave", "finished", conclusion="success")
        request.assert_not_called()
        return None

    def test_callbacks_take_run_identity_from_actions(self):
        environment = dict(CALLBACK_URL="https://fixture.invalid/report", GITHUB_RUN_ID="42", CORECI_CALLBACK_SECRET="fixture")
        with patch.dict(os.environ, environment), patch("urllib.request.urlopen") as request:
            callback("unit", "started", build_id="unit", phase="build", run_id="foreign")
        values = urllib.parse.parse_qs(request.call_args.args[0].data.decode())
        self.assertEqual(values["run_id"], ["42"])
        self.assertEqual(values["build_id"], ["unit"])
        return None

    def test_publication_finished_callback_obeys_plan_owner_and_fails_closed(self):
        environment = dict(PLAN_URL="https://fixture.invalid/plan", CALLBACK_URL="https://fixture.invalid/report",
                           PUBLICATION_ID="p1", FEED_BRANCH="feed-staging", GITHUB_RUN_ID="42", CONCLUSION="success",
                           CORECI_CALLBACK_SECRET="fixture")
        script = Path(__file__).resolve().parents[1] / "tools/common.py"
        for owner in (None, 43, "42", True, 42):
            with self.subTest(owner=owner), patch.dict(os.environ, environment), patch("urllib.request.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = json.dumps(
                    dict(publication="p1", feed_branch="feed-staging", owner_run_id=owner)).encode()
                runpy.run_path(str(script), run_name="__main__")
                self.assertEqual(request.call_count, 2 if type(owner) is int and owner == 42 else 1)
                if type(owner) is int and owner == 42:
                    values = urllib.parse.parse_qs(request.call_args.args[0].data.decode())
                    self.assertEqual(values, dict(scope=["publication"], status=["finished"], run_id=["42"], conclusion=["success"]))
        for body in (b'{"publication":"p1","feed_branch":"feed-staging"}',
                     b'{"publication":"other","feed_branch":"feed-staging","owner_run_id":42}', b'not-json'):
            with self.subTest(body=body), patch.dict(os.environ, environment), patch("urllib.request.urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = body
                with self.assertRaises((AssertionError, KeyError, ValueError)):
                    runpy.run_path(str(script), run_name="__main__")
                self.assertEqual(request.call_count, 1)
        with patch.dict(os.environ, environment), patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")) as request:
            with self.assertRaises(urllib.error.URLError):
                runpy.run_path(str(script), run_name="__main__")
            self.assertEqual(request.call_count, 1)
        return None

    def test_wave_finished_and_target_finished_wire_fields(self):
        script = Path(__file__).resolve().parents[1] / "tools/common.py"
        environment = dict(CALLBACK_URL="https://fixture.invalid/report", GITHUB_RUN_ID="42",
                           CORECI_CALLBACK_SECRET="fixture", CONCLUSION="failure")
        for artifact in (None, "", "coreci-w1-target"):
            with self.subTest(artifact=artifact), patch.dict(os.environ, environment), patch("urllib.request.urlopen") as request:
                if artifact is not None:
                    os.environ.update(TARGET='{"id":"target"}', ARTIFACT=artifact)
                runpy.run_path(str(script), run_name="__main__")
                values = urllib.parse.parse_qs(request.call_args.args[0].data.decode(), keep_blank_values=True)
                expected = (dict(scope=["wave"], conclusion=["failure"]) if artifact is None else
                            dict(scope=["target"], target=["target"], artifact=[artifact]))
                self.assertEqual(values, dict(expected, status=["finished"], run_id=["42"]))
        return None

    def test_publication_plan_batches_and_rejects_ambiguous_targets_or_builds(self):
        unit = dict(build_id="b1", package="a", version="1.0", revision=1)
        item = dict(wave="w1", wave_run_id="123", target="one", artifact="coreci-w1-one", units=[unit])
        plan = dict(items=[item])
        catalog = {"one": {}, "two": {}}
        self.assertEqual(publication_items(plan, catalog), [item])
        for items in ([], [item, item], [dict(item, target="unknown")], [dict(item, wave_run_id="bad")],
                      [dict(item, units=[])], [dict(item, units=[unit, unit])]):
            with self.subTest(items=items), self.assertRaises(AssertionError):
                publication_items(dict(items=items), catalog)
        self.assertEqual(publication_items(dict(items=[item, dict(item, target="two", artifact="other",
                         units=[dict(unit, build_id="b2")])]), catalog)[1]["target"], "two")
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
