import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import publish as publisher
from common import capture, digest, read_json, write_json

CATALOG = {item["id"]: item for item in json.loads((ROOT / "ci/targets.json").read_text())["targets"]}
DEB = CATALOG["debian-trixie-amd64"]
RPM = CATALOG["fedora-44-x86_64"]
IPK = CATALOG["openwrt-24.10-x86_64"]


def command(*args):
    result = subprocess.run(args, text=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.stdout.strip()


def item(name, revision, target=DEB):
    result = dict(package=name, version="1.0.0", revision=revision, request=f"{name}-{revision}",
                  target=target, identity="a" * 64, artifact_id=5, artifact_digest="b" * 64,
                  attempt=1, run_id=42, repo="fixture/" + name, commit="a" * 40,
                  ci_commit="b" * 40, bindings=[], closure=[])
    return result


def make_deb(directory, name, revision, dependency=""):
    package = directory / "package"
    (package / "DEBIAN").mkdir(parents=True)
    control = (f"Package: {name}\nVersion: 1.0.0-{revision}~trixie\nArchitecture: amd64\n"
               "Maintainer: Fixture <fixture@example.invalid>\nDescription: Feed fixture\n")
    if dependency:
        control += "Depends: " + dependency + "\n"
    (package / "DEBIAN/control").write_text(control)
    (package / "payload").write_text(f"{name} {revision}\n")
    path = directory / f"{name}_1.0.0-{revision}~trixie_amd64.deb"
    command("dpkg-deb", "--build", "--root-owner-group", str(package), str(path))
    return path


def make_rpm(directory, revision):
    spec = directory / "example.spec"
    spec.write_text(f"Name: fixture\nVersion: 1.0.0\nRelease: {revision}.fc44\n"
                    "Summary: Feed fixture\nLicense: MIT\nBuildArch: noarch\n"
                    "%description\nFixture\n%install\nmkdir -p %{buildroot}/usr/share/fixture\n"
                    "%files\n%dir /usr/share/fixture\n")
    command("rpmbuild", "--define", "_topdir " + str(directory), "-bb", str(spec))
    result = next((directory / "RPMS").glob(f"noarch/*-{revision}.fc44.noarch.rpm"))
    return result


def make_ipk(path):
    control = b"Package: fixture\nVersion: 1.0.0-r1\nArchitecture: x86_64\nDepends: runtime\n"
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz") as archive:
        member = tarfile.TarInfo("./control")
        member.size = len(control)
        archive.addfile(member, io.BytesIO(control))
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("./control.tar.gz")
        member.size = len(inner.getvalue())
        archive.addfile(member, io.BytesIO(inner.getvalue()))
    return None


class PublisherTests(unittest.TestCase):
    def setUp(self):
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=root)))
        self.enterContext(chdir(self.root))
        self.enterContext(patch.dict(os.environ, GITHUB_REPOSITORY="Fixture/Packages",
                                    FEED_BRANCH="feed-staging", FAULT="none"))
        self.workflow = Mock()
        return None

    def initialize_feed(self, legacy):
        command("git", "init", "--bare", "remote.git")
        command("git", "init", "-q", "actor")
        self.enterContext(chdir("actor"))
        command("git", "config", "user.name", "Fixture")
        command("git", "config", "user.email", "fixture@example.invalid")
        command("git", "config", "commit.gpgsign", "false")
        command("git", "remote", "add", "origin", "../remote.git")
        old = Path("apt/dists/trixie")
        old.mkdir(parents=True)
        (old / "InRelease").write_bytes(b"legacy metadata\x00")
        old_pool = Path("apt/pool")
        old_pool.mkdir()
        (old_pool / "fixture").write_bytes(b"legacy payload\x00")
        write_json("publications.json", legacy)
        command("git", "add", ".")
        command("git", "commit", "-qm", "old feed")
        commit = command("git", "rev-parse", "HEAD")
        command("git", "push", "origin", "HEAD:refs/heads/feed-staging",
                "HEAD:refs/snapshots/feed-staging/pre-x", "HEAD:refs/snapshots/feed-staging/current")
        command("git", "commit", "--allow-empty", "-qm", "old unrelated snapshot")
        command("git", "push", "origin", "HEAD:refs/snapshots/feed-staging/stale")
        Path("keys").mkdir()
        shutil.copytree(ROOT / "ci", "ci")
        Path(".claim").write_text("a" * 64)
        return commit

    def signing_key(self):
        signing = self.root / "fixture-key"
        signing.mkdir(mode=0o700)
        home = str(signing)
        command("gpg", "--homedir", home, "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                "--quick-generate-key", "Fixture <fixture@example.invalid>", "rsa2048", "sign", "0")
        self.addCleanup(command, "gpgconf", "--homedir", home, "--kill", "gpg-agent")
        command("gpg", "--homedir", home, "--batch", "--output", str(self.root / "pub.gpg"), "--export")
        command("gpg", "--homedir", home, "--batch", "--armor", "--output", str(self.root / "pub.asc"), "--export")
        private = command("gpg", "--homedir", home, "--batch", "--armor", "--export-secret-keys")
        return private

    def plan(self, label, expected, items, **changes):
        result = dict(publication=label, attempt=1, expected_head=expected, deadline=time.time() + 60,
                      feed_branch="feed-staging", fault="none", items=items, retirements=[],
                      delete_snapshots=[], retire_legacy_apt=False, legacy_dates={}, acknowledged_receipts=[])
        result.update(changes)
        return result

    def execute_plan(self, plan, artifacts, private):
        workspace = self.root / plan["publication"]
        workspace.mkdir()

        def copy(item, directory):
            source = artifacts[item["request"]]
            if source is None:
                raise ValueError("malformed manifest")
            shutil.copyfile(source, directory / source.name)
            return {source.name: digest(source)}

        with patch.dict(os.environ, PACKAGES_GPG_PRIVATE_KEY=private,
                        OPENWRT_USIGN_PRIVATE_KEY="fixture", OPENWRT_APK_PRIVATE_KEY="fixture"), \
                patch("publish.artifact", side_effect=copy):
            publisher.publish(plan, self.workflow, workspace)
        command("gpgconf", "--homedir", str(workspace / "gnupg"), "--kill", "gpg-agent")
        head = publisher.remote_refs("refs/heads/feed-staging")["refs/heads/feed-staging"]
        return workspace / "feed", head

    def test_atomic_push_legacy_apt_receipts_by_hash_and_retired_payloads(self):
        expected = self.initialize_feed([])
        private = self.signing_key()
        paths = {}
        for revision in range(1, 6):
            directory = self.root / f"package-{revision}"
            directory.mkdir()
            paths[f"fixture-{revision}"] = make_deb(directory, "fixture", revision, "runtime (= 1.0.0-1~trixie)")
        directory = self.root / "runtime"
        directory.mkdir()
        paths["runtime-1"] = make_deb(directory, "runtime", 1)
        paths["malformed-1"] = None
        plan = self.plan("p1", expected, [item("fixture", 1), item("runtime", 1), item("malformed", 1)],
                         delete_snapshots=["refs/snapshots/feed-staging/stale"])
        feed, expected = self.execute_plan(plan, paths, private)
        refs = publisher.remote_refs("refs/snapshots/feed-staging/*")
        self.assertNotIn("refs/snapshots/feed-staging/stale", refs)
        self.assertIn("refs/snapshots/feed-staging/pre-x", refs)
        self.assertEqual(refs["refs/snapshots/feed-staging/p1"], expected)
        ledger = read_json(feed / "publications.json")
        self.assertEqual([r["outcome"] for r in ledger["receipts"]], ["published", "published", "failed"])
        index = "apt/debian/dists/trixie/main/binary-amd64"
        first_hash = digest(feed / index / "Packages")
        old_path = ledger["entries"][0]["inventory"][0]["path"]
        legacy_tree = command("git", "rev-parse", refs["refs/snapshots/feed-staging/pre-x"] + ":apt/dists")
        for revision in range(2, 6):
            plan = self.plan("p" + str(revision), expected, [item("fixture", revision)])
            if revision == 3:
                plan["retirements"] = [["fixture", "1.0.0", 1, DEB["id"]]]
                plan["acknowledged_receipts"] = [["p1", 1, "fixture-1"]]
            feed, expected = self.execute_plan(plan, paths, private)
            if revision == 3:
                ledger = read_json(feed / "publications.json")
                self.assertNotIn("fixture-1", [r["request"] for r in ledger["receipts"]])
                self.assertTrue((feed / index / "by-hash/SHA256" / first_hash).is_file())
                self.assertTrue((feed / old_path).is_file())
                current = (feed / index / "Packages").read_text()
                self.assertNotIn(Path(old_path).name, current)
            self.assertEqual(command("git", "rev-list", "--count", expected), "1")
            self.assertEqual(command("git", "rev-parse", expected + ":apt/dists"), legacy_tree)
            self.assertEqual((feed / "apt/pool/fixture").read_bytes(), b"legacy payload\x00")
        self.assertFalse((feed / old_path).exists())
        self.assertFalse((feed / index / "by-hash/SHA256" / first_hash).exists())
        self.assertIn("Package: runtime", (feed / index / "Packages").read_text())
        release = feed / "apt/debian/dists/trixie"
        text = (release / "Release").read_text()
        self.assertIn("Origin: Fixture/Packages\nLabel: SNode.C\nSuite: trixie\nCodename: trixie\n", text)
        self.assertNotIn("MD5Sum", text)
        command("gpgv", "--keyring", str(self.root / "pub.gpg"), str(release / "Release.gpg"), str(release / "Release"))
        command("gpgv", "--keyring", str(self.root / "pub.gpg"), str(release / "InRelease"))
        self.assertEqual(len(list((feed / index / "by-hash/SHA256").iterdir())), 6)
        retired, _ = self.execute_plan(self.plan("p6", expected, [], retire_legacy_apt=True), {}, private)
        self.assertFalse((retired / "apt/dists").exists())
        self.assertFalse((retired / "apt/pool").exists())
        self.assertTrue((retired / index / "Packages").exists())
        return None

    def test_namespace_collision_is_quarantined_and_rpm_digest_records_signing(self):
        expected = self.initialize_feed([])
        private = self.signing_key()
        directory = self.root / "rpm"
        directory.mkdir()
        first = make_rpm(directory, 1)
        second = make_rpm(directory, 2)
        packages = {"fixture-1": first, "fixture-2": second}
        feed, expected = self.execute_plan(self.plan("r1", expected, [item("fixture", 1, RPM)]), packages, private)
        record = read_json(feed / "publications.json")["entries"][0]["inventory"][0]
        self.assertEqual(record["sha256"], digest(first))
        self.assertNotEqual(record["sha256"], record["served_sha256"])
        self.assertEqual(record["served_sha256"], digest(feed / record["path"]))
        rpm_db = self.root / "rpmdb"
        command("rpm", "--define", "_keyringpath " + str(rpm_db), "--dbpath", str(rpm_db),
                "--import", str(self.root / "pub.asc"))
        self.assertIn("digests signatures OK", command("rpmkeys", "--define", "_keyringpath " + str(rpm_db),
                                                      "--dbpath", str(rpm_db), "--checksig", str(feed / record["path"])))
        duplicate = dict(item("fixture", 1, RPM), request="duplicate")
        packages["duplicate"] = first
        conflict = self.root / first.name
        shutil.copyfile(first, conflict)
        with conflict.open("ab") as output:
            output.write(b"modified original digest")
        packages["fixture-1"] = conflict
        feed, _ = self.execute_plan(self.plan("r2", expected, [item("fixture", 1, RPM), duplicate,
                                    item("fixture", 2, RPM)]), packages, private)
        receipts = read_json(feed / "publications.json")["receipts"][-3:]
        self.assertEqual(receipts[0]["failure"], "immutable-conflict")
        self.assertEqual(receipts[1]["reason"], "already published")
        self.assertEqual(receipts[2]["outcome"], "published")
        return None

    def test_legacy_import_records_old_database_or_branch_pre_x_timestamp(self):
        expected = self.initialize_feed([])
        date = int(command("git", "show", "-s", "--format=%ct", expected))
        feed = self.root / "input"
        rpm = feed / "rpm/fedora/44/x86_64"
        ipk = feed / "openwrt/24.10/x86_64"
        rpm.mkdir(parents=True)
        ipk.mkdir(parents=True)
        rpm_source = make_rpm(self.root, 1)
        rpm_path = rpm / rpm_source.name
        shutil.copyfile(rpm_source, rpm_path)
        ipk_path = ipk / "fixture_1.0.0-r1_x86_64.ipk"
        make_ipk(ipk_path)
        legacy = [dict(unit=dict(package="fixture", version="1.0.0", revision=1), target=target,
                       files={path.relative_to(feed).as_posix(): "old"}) for target, path in ((RPM, rpm_path), (IPK, ipk_path))]
        plan = self.plan("legacy", expected, [], legacy_dates={"fixture/1.0.0/1/" + RPM["id"]: date - 500})
        ledger = dict(entries=[], legacy=legacy)
        publisher.import_legacy(feed, ledger, plan, {publisher.namespace(RPM), publisher.namespace(IPK)}, None)
        self.assertEqual(ledger["legacy"], [])
        self.assertEqual([entry["published_at"] for entry in ledger["entries"]], [date - 500, date])
        rpm_entry, ipk_entry = [entry["inventory"][0] for entry in ledger["entries"]]
        self.assertEqual(rpm_entry["sha256"], "unknown")
        self.assertEqual(ipk_entry["sha256"], "unknown")
        self.assertEqual(rpm_entry["served_sha256"], digest(rpm_path))
        self.assertEqual(ipk_entry["served_sha256"], digest(ipk_path))
        self.assertEqual(ipk_entry["runtime_dependencies"], ["runtime"])
        return None

    def test_expected_head_and_pre_x_snapshots_are_protected(self):
        expected = self.initialize_feed([])
        workspace = self.root / "push"
        workspace.mkdir()
        branch = "refs/snapshots/feed-staging/"
        for forbidden in ("pre-x", "current"):
            plan = self.plan("p1", expected, [], delete_snapshots=[branch + forbidden])
            with self.assertRaisesRegex(ValueError, "protected"):
                publisher.push(workspace, plan, self.workflow, workspace)
            self.assertEqual(publisher.remote_refs("refs/heads/feed-staging")["refs/heads/feed-staging"], expected)
        return None

    def test_actual_lease_rejects_racing_head_without_creating_or_deleting_refs(self):
        expected = self.initialize_feed([])
        workspace = self.root / "push"
        workspace.mkdir()
        feed = workspace / "feed"
        feed.mkdir()
        (feed / "payload").write_text("new")
        new_head = capture("git", "rev-parse", "HEAD")
        before = publisher.remote_refs("refs/snapshots/feed-staging/*")

        def advance(*args):
            command("git", "push", "origin", "HEAD:refs/heads/feed-staging")
            return None

        self.workflow.document.side_effect = advance
        plan = self.plan("p1", expected, [], delete_snapshots=["refs/snapshots/feed-staging/stale"])
        with self.assertRaises(subprocess.CalledProcessError):
            publisher.push(feed, plan, self.workflow, workspace)
        self.assertEqual(publisher.remote_refs("refs/heads/feed-staging")["refs/heads/feed-staging"], new_head)
        self.assertEqual(publisher.remote_refs("refs/snapshots/feed-staging/*"), before)
        return None

    def test_zip_verification_checks_digest_names_and_manifest(self):
        package = b"content"
        request = item("fixture", 1)
        filename = "fixture_1.0.0-1~trixie_amd64.deb"
        members = {filename: package, "inventory.json": b"[]"}
        import hashlib

        manifest = dict(unit=dict(package="fixture", version="1.0.0", revision=1), request=request["request"],
                        attempt=1, target=DEB["id"], files={filename: hashlib.sha256(package).hexdigest()})
        members["manifest.json"] = json.dumps(manifest).encode()
        for number, extra in enumerate(({}, {"../escape.deb": b"bad"})):
            target = Path(f"artifact-{number}")
            target.mkdir()
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as output:
                for name, contents in (members | extra).items():
                    output.writestr(name, contents)
            request.update(inventory=[], artifact_digest=hashlib.sha256(archive.getvalue()).hexdigest())
            facts = dict(id=5, workflow_run=dict(id=42), name=f'coreci-{request["request"]}-1', expired=False)
            archive.seek(0)
            with patch("publish.api", side_effect=[io.BytesIO(json.dumps(facts).encode()), archive]):
                if extra:
                    with self.assertRaisesRegex(ValueError, "manifest contents"):
                        publisher.artifact(request, target)
                else:
                    self.assertEqual(publisher.artifact(request, target), manifest["files"])
                    self.assertEqual((target / filename).read_bytes(), package)
        return None
