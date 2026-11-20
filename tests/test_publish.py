import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from common import sha256
from publish import PublicationError, main, publish, sign

CATALOG = json.loads((Path(__file__).resolve().parents[1] / "ci/targets.json").read_text())


def batch(target, unit):
    result = dict(publication="p1", feed_branch="feed-staging", owner_run_id=42, items=[dict(
        wave="w1", wave_run_id="123", target=target["id"], artifact="coreci-w1-" + target["id"],
        units=[{key: unit[key] for key in ("build_id", "package", "version", "revision")}])])
    return result


def catalog():
    Path("ci").mkdir()
    Path("ci/targets.json").write_text(json.dumps(CATALOG))
    return None


class PublicationTests(unittest.TestCase):
    def test_signed_apt_retention_and_single_root_commit(self):
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            work = Path(temporary)
            original = Path.cwd()
            os.chdir(work)
            try:
                self.qualify(work)
            finally:
                os.chdir(original)
        return None

    def qualify(self, work):
        def command(*args):
            process = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(process.returncode, 0, process.stdout)
            return process.stdout.strip()

        command("git", "init", "--bare", "remote.git")
        command("git", "init", "repo")
        os.chdir("repo")
        command("git", "remote", "add", "origin", str(work / "remote.git"))
        command("git", "config", "user.name", "Fixture")
        command("git", "config", "user.email", "fixture@example.invalid")
        command("git", "config", "commit.gpgsign", "false")
        command("git", "commit", "--allow-empty", "-m", "Fixture source")
        catalog()
        Path("keys").mkdir()
        Path("signing/gnupg").mkdir(parents=True, mode=0o700)
        home = str(Path("signing/gnupg").resolve())
        command("gpg", "--homedir", home, "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                "--quick-generate-key", "Fixture <fixture@example.invalid>", "rsa2048", "sign", "0")
        command("gpg", "--homedir", home, "--batch", "--output", "keys/fixture.gpg", "--export")
        command("gpg", "--homedir", home, "--batch", "--armor", "--output", "keys/fixture.asc", "--export")
        target = dict(id="debian-trixie-amd64", family="deb", codename="trixie", arch="amd64", feed_commit="f" * 40)
        package = Path("package/DEBIAN")
        package.mkdir(parents=True)
        artifact = Path("artifacts/debian-trixie-amd64")
        (artifact / "fixture").mkdir(parents=True)
        for publication, revision in enumerate((1, 2, 3, 3), 1):
            version = f"1.0.0-{revision}~trixie"
            (package / "control").write_text(f"Package: fixture\nVersion: {version}\nArchitecture: amd64\n"
                                             "Maintainer: Fixture <fixture@example.invalid>\nDescription: Disposable feed fixture\n")
            output = artifact / "fixture" / ("fixture_" + version + "_amd64.deb")
            if publication != 4:
                (package.parent / "payload").write_text(str(publication))
                command("dpkg-deb", "--build", "--root-owner-group", "package", str(output))
            unit = dict(package="fixture", version="1.0.0", revision=revision, action="build", build_id=str(publication))
            manifest = dict(target=target, units=[dict(unit=unit, state="succeeded")],
                            files={str(output.relative_to(artifact)): sha256(output)})
            (artifact / "manifest.json").write_text(json.dumps(manifest))
            environment = dict(FEED_BRANCH="feed-staging", CALLBACK_URL="", PUBLICATION_ID="p1",
                               PLAN_URL="https://fixture.invalid/plan", PLAN_JSON="", GITHUB_RUN_ID="42")
            plan = batch(target, unit)
            with patch.dict(os.environ, environment), patch("common.fetch", return_value=json.dumps(plan).encode()):
                published, commit = publish(plan)
            head = command("git", "--git-dir", str(work / "remote.git"), "rev-parse", "feed-staging")
            self.assertEqual(commit, head)
            self.assertEqual(published, [dict(unit=unit, reason="already published" if publication == 4 else "")])
            snapshot = {str(path.relative_to("feed-work")): path.read_bytes()
                        for path in Path("feed-work").rglob("*") if path.is_file()}
            if publication == 4:
                self.assertEqual(head, previous_head)
                self.assertEqual(snapshot, previous_snapshot)
                self.assertFalse(Path("feed.index").exists())
            previous_head, previous_snapshot = head, snapshot
            self.assertEqual(command("git", "--git-dir", str(work / "remote.git"), "rev-list", "--count", "feed-staging"), "1")
            command("gpgv", "--keyring", str(Path("keys/fixture.gpg").resolve()),
                    "feed-work/apt/dists/trixie/Release.gpg", "feed-work/apt/dists/trixie/Release")
            command("gpgv", "--keyring", str(Path("keys/fixture.gpg").resolve()), "feed-work/apt/dists/trixie/InRelease")
            ledger = json.loads(Path("feed-work/publications.json").read_text())
            self.assertNotIn("feed_commit", ledger[-1]["target"])
            self.assertEqual([entry["unit"]["revision"] for entry in ledger], list(range(max(1, revision - 1), revision + 1)))
            self.assertEqual(ledger[-1]["unit"]["build_id"], str(min(publication, 3)))
            for entry in ledger:
                for file, digest in entry["files"].items():
                    self.assertEqual(sha256(Path("feed-work") / file), digest)
            index = Path("feed-work/apt/dists/trixie/main/binary-amd64/Packages")
            text = index.read_text()
            self.assertIn("Version: " + version, text)
            self.assertNotIn("MD5sum:", text)
            self.assertTrue((index.parent / "by-hash/SHA256" / sha256(index)).exists())
            if revision == 3:
                self.assertNotIn("Version: 1.0.0-1~trixie", text)
                self.assertEqual(len(list(Path("feed-work/apt/pool").rglob("*.deb"))), 2)
            shutil.rmtree("feed-work")
            Path("feed.index").unlink(missing_ok=True)
            if publication == 4:
                before = command("git", "--git-dir", str(work / "remote.git"), "rev-parse", "feed-staging")
                environment["PLAN_URL"] = "https://fixture.invalid/plan"
                foreign = json.dumps(dict(plan, owner_run_id=43)).encode()
                with patch.dict(os.environ, environment), patch("common.fetch", return_value=foreign) as latest, \
                        patch("publish.callback") as report:
                    added, commit = publish(plan)
                self.assertEqual(added, [])
                self.assertIsNone(commit)
                latest.assert_called_once_with(environment["PLAN_URL"])
                report.assert_not_called()
                self.assertEqual(command("git", "--git-dir", str(work / "remote.git"), "rev-parse", "feed-staging"), before)
                shutil.rmtree("feed-work")
                Path("feed.index").unlink(missing_ok=True)
                output.write_bytes(b"different immutable bytes")
                conflict = dict(manifest, files={str(output.relative_to(artifact)): sha256(output)})
                (artifact / "manifest.json").write_text(json.dumps(conflict))
                with patch.dict(os.environ, environment), self.assertRaises(PublicationError) as caught:
                    publish(plan)
                self.assertEqual(caught.exception.failure, "immutable-conflict")
                self.assertEqual(command("git", "--git-dir", str(work / "remote.git"), "rev-parse", "feed-staging"), before)
                output.write_bytes(previous_snapshot[next(file for file in ledger[-1]["files"])])
                (artifact / "manifest.json").write_text(json.dumps(manifest))
                shutil.rmtree("feed-work")
            if publication < 3:
                output.unlink()
        repeated_unit = unit
        previous_plan = plan
        artifact = Path("artifacts/fedora-44-x86_64")
        (artifact / "fixture").mkdir(parents=True)
        Path("fixture.spec").write_text("Name: fixture\nVersion: 1.0.0\nRelease: 1.fc44\n"
                                       "Summary: Disposable feed fixture\nLicense: MIT\nBuildArch: noarch\n"
                                       "%description\nDisposable fixture\n%install\n"
                                       "mkdir -p %{buildroot}/usr/share/wave-fixture\n"
                                       "%files\n%dir /usr/share/wave-fixture\n")
        command("rpmbuild", "--define", "_topdir " + str(Path("rpm-build").resolve()), "-bb", "fixture.spec")
        output = artifact / "fixture/fixture-1.0.0-1.fc44.noarch.rpm"
        shutil.copy2(next(Path("rpm-build/RPMS").rglob("*.rpm")), output)
        unit = dict(package="fixture", version="1.0.0", revision=1, action="build", build_id="rpm")
        target = dict(id="fedora-44-x86_64", family="rpm", distribution="fedora", release="44",
                      arch="x86_64", disttag="fc44")
        manifest = dict(target=target, units=[dict(unit=unit, state="succeeded")],
                        files={str(output.relative_to(artifact)): sha256(output)})
        (artifact / "manifest.json").write_text(json.dumps(manifest))
        fingerprint = next(line.split(":")[9] for line in command("gpg", "--homedir", home,
                           "--with-colons", "--list-keys").splitlines() if line.startswith("fpr:"))
        plan = batch(target, unit)
        plan["items"] = previous_plan["items"] + plan["items"]
        environment["GPG_FINGERPRINT"] = fingerprint
        with patch.dict(os.environ, environment), patch("common.fetch", return_value=json.dumps(plan).encode()):
            published, commit = publish(plan)
        self.assertEqual(published, [dict(unit=repeated_unit, reason="already published"), dict(unit=unit, reason="")])
        self.assertNotEqual(commit, previous_head)
        mixed_ledger = json.loads(Path("feed-work/publications.json").read_text())
        for entry in ledger:
            self.assertIn(entry, mixed_ledger)
            for file, digest in entry["files"].items():
                self.assertEqual(sha256(Path("feed-work") / file), digest)
        directory = Path("feed-work/rpm/fedora/44/x86_64")
        command("gpgv", "--keyring", str(Path("keys/fixture.gpg").resolve()),
                str(directory / "repodata/repomd.xml.asc"), str(directory / "repodata/repomd.xml"))
        database = str(Path("rpmdb").resolve())
        command("rpm", "--define", "_keyringpath " + database, "--dbpath", database, "--import", "keys/fixture.asc")
        self.assertIn("digests signatures OK", command("rpmkeys", "--define", "_keyringpath " + database, "--dbpath", database, "--checksig",
                      str(directory / output.name)))
        self.assertEqual(command("git", "--git-dir", str(work / "remote.git"), "rev-list", "--count", "feed-staging"), "1")
        shutil.rmtree("feed-work")
        Path("feed.index").unlink()
        with patch.dict(os.environ, environment), patch("common.fetch", return_value=json.dumps(plan).encode()):
            repeated, same_commit = publish(plan)
        self.assertEqual(same_commit, commit)
        self.assertEqual([item["reason"] for item in repeated], ["already published"] * 2)
        shutil.rmtree("feed-work")
        missing = dict(plan, items=[dict(plan["items"][0], target="debian-trixie-arm64", artifact="non-existent")])
        with patch.dict(os.environ, environment), self.assertRaises(PublicationError) as caught:
            publish(missing)
        self.assertEqual(caught.exception.failure, "artifact-unavailable")
        self.assertEqual(command("git", "--git-dir", str(work / "remote.git"), "rev-parse", "feed-staging"), commit)
        shutil.rmtree("feed-work")
        unit = dict(unit, revision=2, build_id="lease")
        manifest["units"][0]["unit"] = unit
        (artifact / "manifest.json").write_text(json.dumps(manifest))
        plan = batch(target, unit)
        import publish as publisher
        real_run = publisher.run

        def reject_push(*args, **kwargs):
            if args[:2] == ("git", "push"):
                raise subprocess.CalledProcessError(1, args)
            result = real_run(*args, **kwargs)
            return result

        with patch.dict(os.environ, environment), patch("common.fetch", return_value=json.dumps(plan).encode()), \
                patch("publish.run", side_effect=reject_push) as calls, self.assertRaises(PublicationError) as caught:
            publish(plan)
        self.assertEqual(caught.exception.failure, "feed-push")
        pushed = next(call.args for call in calls.call_args_list if call.args[:2] == ("git", "push"))
        self.assertIn("--force-with-lease=refs/heads/feed-staging:" + commit, pushed)
        self.assertEqual(command("git", "--git-dir", str(work / "remote.git"), "rev-parse", "feed-staging"), commit)
        command("gpgconf", "--homedir", home, "--kill", "gpg-agent")
        return None

    def test_already_published_success_callback_uses_current_build_and_commit(self):
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        environment = dict(PLAN_URL="", PACKAGES_GPG_PRIVATE_KEY="fixture", OPENWRT_USIGN_PRIVATE_KEY="fixture",
                           OPENWRT_APK_PRIVATE_KEY="fixture")
        plan = batch(dict(id="debian-trixie-amd64"), dict(build_id="current", package="fixture", version="1", revision=1))
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            original = Path.cwd()
            os.chdir(temporary)
            try:
                catalog()
                with patch.dict(os.environ, environment), patch("publish.gpg"), patch("publish.os.umask"), \
                        patch("publish.load_plan", return_value=plan), patch("publish.owns_publication", return_value=True), \
                        patch("publish.publish", return_value=([dict(unit=dict(build_id="current"),
                              reason="already published")], "a" * 40)), patch("publish.callback") as report:
                    main()
                report.assert_called_once_with("unit", "succeeded", build_id="current", phase="publish",
                                               feed_commit="a" * 40, reason="already published")
            finally:
                os.chdir(original)
        return None

    def test_non_owner_main_skips_signing_and_callbacks(self):
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        environment = dict(PLAN_URL="https://fixture.invalid/plan", PUBLICATION_ID="p1", FEED_BRANCH="feed-staging", GITHUB_RUN_ID="42")
        plan = batch(dict(id="debian-trixie-amd64"), dict(build_id="b1", package="fixture", version="1", revision=1))
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            original = Path.cwd()
            os.chdir(temporary)
            try:
                catalog()
                with patch.dict(os.environ, environment), \
                        patch("common.fetch", return_value=json.dumps(dict(plan, owner_run_id=43)).encode()), \
                        patch("publish.callback") as report:
                    main()
                report.assert_not_called()
                self.assertEqual([path.name for path in Path.cwd().iterdir()], ["ci"])
            finally:
                os.chdir(original)
        return None

    def test_failure_callbacks_include_machine_code_for_every_planned_unit(self):
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        plan = batch(dict(id="debian-trixie-amd64"), dict(build_id="b1", package="fixture", version="1", revision=1))
        environment = dict(PACKAGES_GPG_PRIVATE_KEY="fixture", OPENWRT_USIGN_PRIVATE_KEY="fixture", OPENWRT_APK_PRIVATE_KEY="fixture")
        original = Path.cwd()
        for failure in ("artifact-unavailable", "immutable-conflict", "feed-push", "signing", "other"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=root) as temporary:
                os.chdir(temporary)
                try:
                    catalog()
                    error = RuntimeError("fixture") if failure == "other" else PublicationError(failure, "fixture")
                    with patch.dict(os.environ, environment), patch("publish.load_plan", return_value=plan), \
                            patch("publish.owns_publication", return_value=True), patch("publish.gpg"), patch("publish.os.umask"), \
                            patch("publish.publish", side_effect=error), patch("publish.callback") as report:
                        with self.assertRaises(RuntimeError):
                            main()
                    report.assert_called_once_with("unit", "failed", build_id="b1", phase="publish", reason="fixture", failure=failure)
                finally:
                    os.chdir(original)
        with patch("publish.run", side_effect=OSError("fixture")), self.assertRaises(PublicationError) as caught:
            sign("fixture")
        self.assertEqual(caught.exception.failure, "signing")
        return None


if __name__ == "__main__":
    unittest.main()
