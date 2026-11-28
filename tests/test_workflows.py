import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from common import Workflow, signature
from publish import check_plan


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        build = Path("/tmp/codex/build/Packages/tests")
        build.mkdir(parents=True, exist_ok=True)
        temporary = self.enterContext(tempfile.TemporaryDirectory(dir=build))
        self.enterContext(chdir(temporary))
        self.enterContext(patch.dict(os.environ, CORECI_ORIGIN="https://core.example:8443",
                                    REQUEST_ID="req1", ATTEMPT="1", GITHUB_RUN_ID="42",
                                    GITHUB_RUN_ATTEMPT="1", CORECI_CALLBACK_SECRET="key"))
        return None

    def reply(self, workflow, document, **changes):
        stamp = str(int(time.time()) - changes.pop("age", 0))
        key = changes.pop("key", "key")
        signed = changes.pop("signed", True)
        grant = dict(workflow.owner, granted=True, document_sha256=hashlib.sha256(document).hexdigest())
        grant.update(changes)
        body = json.dumps(grant).encode()
        headers = {"X-CoreCI-Timestamp": stamp}
        if signed:
            headers["X-CoreCI-Signature-256"] = signature(key, stamp, body)
        return 200, headers, body

    def test_claim_binds_owner_and_exact_document_for_both_workflows(self):
        document = b'{ "attempt": 1 }\n'
        for kind in ("requests", "publications"):
            workflow = Workflow(kind)
            reply = self.reply(workflow, document)
            with patch.object(workflow, "request", side_effect=[reply, (200, {}, document)] * 2) as request:
                self.assertTrue(workflow.claim())
                self.assertTrue(workflow.claim())
            self.assertEqual(Path(".document").read_bytes(), document)
            self.assertEqual(Path(".claim").read_text(), hashlib.sha256(document).hexdigest())
            self.assertEqual(request.call_args_list[0].args, ("/claim", dict(attempt=1, run_id=42, run_attempt=1)))
            self.assertEqual(request.call_args_list[1].args, ("/attempts/1",))
        return None

    def test_unsigned_forged_expired_mismatched_claims_fail_closed(self):
        document = b"{}"
        for changes in (dict(signed=False), dict(key="wrong"), dict(age=301), dict(id="wrong"),
                        dict(attempt=2), dict(run_id=43), dict(run_attempt=2), dict(granted=False)):
            with self.subTest(changes=changes):
                workflow = Workflow("requests")
                reply = self.reply(workflow, document, **changes)
                with patch.object(workflow, "request", return_value=reply) as request, self.assertRaises(ValueError):
                    workflow.claim()
                self.assertEqual(request.call_count, 1)
                self.assertFalse(Path(".document").exists())
        workflow = Workflow("publications")
        with patch.object(workflow, "request", side_effect=[self.reply(workflow, document), (200, {}, b"{ }")]), \
                self.assertRaisesRegex(ValueError, "document digest mismatch"):
            workflow.claim()
        self.assertFalse(Path(".claim").exists())
        return None

    def test_duplicate_and_manual_rerun_end_successfully_without_work(self):
        for attempt in ("1", "2"):
            for kind in ("requests", "publications"):
                with patch.dict(os.environ, GITHUB_RUN_ATTEMPT=attempt):
                    workflow = Workflow(kind)
                    with patch.object(workflow, "request", return_value=(409, {}, b"")) as request:
                        self.assertFalse(workflow.claim())
                    self.assertEqual(request.call_count, 1)
        self.assertFalse(Path(".claim").exists())
        return None

    def test_trusted_origin_is_required(self):
        for origin in ("", "http://host", "https://name:secret@host", "https://host/path", "https://host?query"):
            with patch.dict(os.environ, CORECI_ORIGIN=origin), self.assertRaises(ValueError):
                Workflow("requests")
        workflow = Workflow("requests")
        self.assertEqual(workflow.endpoint, "https://core.example:8443/api/v1/requests/req1")
        for name in ("build", "publish"):
            text = (ROOT / ".github" / "workflows" / (name + ".yml")).read_text()
            self.assertIn("vars.CORECI_ORIGIN", text)
            self.assertNotRegex(text, r"(?im)^\s+\w*url:\s*$")
        return None

    def test_fault_valid_only_for_frozen_staging_plan(self):
        workflow = Mock(owner=dict(id="pub1", attempt=1))
        plan = dict(publication="pub1", attempt=1, feed_branch="feed", fault="after_push")
        with patch.dict(os.environ, FEED_BRANCH="feed", FAULT="after_push"), self.assertRaisesRegex(ValueError, "fault"):
            check_plan(plan, workflow)
        plan.update(feed_branch="feed-staging", expected_head="a" * 40, deadline=time.time() + 60,
                    items=[], retirements=[], retire_legacy_apt=True)
        with patch.dict(os.environ, FEED_BRANCH="feed-staging", FAULT="after_push"):
            check_plan(plan, workflow)
        return None

    def test_hmac_wire_vector(self):
        body = b"scope=unit&build_id=a%7Et&phase=build&status=started&run_id=42"
        self.assertEqual(signature("key", "1000", body),
                         "sha256=de6e84d9b3c954395b93ee316d046f31f59741249a9958e2a98c5f081fb4226f")
        return None

    def test_no_project_policy_or_legacy_workflows(self):
        registry = json.loads((ROOT / "ci/registry.json").read_text())
        names = {repo.split("/")[1].lower() for repo in registry["repositories"]}
        paths = list((ROOT / "tools").glob("*.py")) + list((ROOT / ".github").rglob("*.yml"))
        paths.append(ROOT / "ci/targets.json")
        for path in paths:
            text = path.read_text().lower()
            self.assertEqual([name for name in names if name in text], [], str(path))
        self.assertFalse((ROOT / ".github/workflows/wave.yml").exists())
        self.assertFalse((ROOT / ".github/workflows/chain.yml").exists())
        self.assertLessEqual(sum(len(path.read_text().splitlines()) for path in (ROOT / "tools").glob("*.py")), 1000)
        return None

    def test_build_workflow_routes_match_the_catalog(self):
        subprocess.run([sys.executable, str(ROOT / "ci/generate_workflow_runners.py"), "--check"], check=True)
        return None

    def test_archive_keyring_is_verified_before_installation(self):
        catalog = json.loads((ROOT / "ci/targets.json").read_bytes())
        setups = [target["repository_setup"] for target in catalog["targets"] if target["distribution"] == "raspberrypios"]
        self.assertTrue(setups)
        for setup in setups:
            before = setup.index(["dpkg", "-i", "/tmp/archive-keyring.deb"])
            verify = setup[before - 1]
            self.assertEqual(verify[:2], ["sh", "-ec"])
            self.assertIn("sha256sum --check", verify[2])
            self.assertRegex(verify[-2], r"^[0-9a-f]{64}$")
            self.assertEqual(verify[-1], "/tmp/archive-keyring.deb")
        return None

    def test_restore_updates_only_branch_and_requires_expected_head(self):
        git = lambda *args: subprocess.run(["git", *args], check=True, text=True, capture_output=True).stdout.strip()
        git("init", "--bare", "remote.git")
        git("init", "actor")
        with chdir("actor"):
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "commit.gpgsign", "false")
            git("remote", "add", "origin", "../remote.git")
            snapshots = []
            for label in ("legacy", "protected", "head"):
                git("commit", "--allow-empty", "-m", label)
                snapshots.append(git("rev-parse", "HEAD"))
            for label, commit in zip(("pre-x", "p1", "p2"), snapshots):
                git("push", "origin", commit + ":refs/snapshots/feed/" + label)
            git("push", "origin", "HEAD:refs/heads/feed-staging")
            lines = (ROOT / ".github/workflows/restore.yml").read_text().splitlines()
            self.assertIn("run-name: restore ${{ inputs.feed_branch }} ${{ inputs.snapshot_ref }}", lines)
            script = "\n".join(line[10:] for line in lines[lines.index("        run: |") + 1:])
            before = git("ls-remote", "origin", "refs/snapshots/*")
            env = dict(os.environ, FEED_BRANCH="feed-staging", SNAPSHOT="refs/snapshots/feed/pre-x",
                       EXPECTED_HEAD=snapshots[1])
            result = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            expected = snapshots[-1]
            for label, commit in zip(("pre-x", "p1", "p2"), snapshots):
                env.update(SNAPSHOT="refs/snapshots/feed/" + label, EXPECTED_HEAD=expected)
                subprocess.run(["bash", "-e", "-c", script], env=env, check=True, capture_output=True)
                self.assertEqual(git("ls-remote", "origin", "refs/heads/feed-staging").split()[0], commit)
                expected = commit
            self.assertEqual(git("ls-remote", "origin", "refs/snapshots/*"), before)
        return None
