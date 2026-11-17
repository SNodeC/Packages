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
from publish import main, publish


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
        command("git", "commit", "--allow-empty", "-m", "Fixture source")
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
            (package.parent / "payload").write_text(str(publication))
            command("dpkg-deb", "--build", "--root-owner-group", "package", str(output))
            unit = dict(package="fixture", version="1.0.0", revision=revision, action="build", build_id=str(publication))
            manifest = dict(target=target, units=[dict(unit=unit, state="succeeded")],
                            files={str(output.relative_to(artifact)): sha256(output)})
            (artifact / "manifest.json").write_text(json.dumps(manifest))
            environment = dict(FEED_BRANCH="feed-staging", CALLBACK_URL="", WAVE_ID=str(revision))
            environment.update(PLAN_URL="https://fixture.invalid/plan" if publication == 2 else "", GITHUB_RUN_ID="42")
            plan = json.dumps(dict(wave=str(revision), owner_run_id=42)).encode()
            with patch.dict(os.environ, environment), patch("common.fetch", return_value=plan):
                published, commit = publish()
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
                foreign = json.dumps(dict(wave=str(revision), owner_run_id=43)).encode()
                with patch.dict(os.environ, environment), patch("common.fetch", return_value=foreign) as latest, \
                        patch("publish.callback") as report:
                    added, commit = publish()
                self.assertEqual(added, [])
                self.assertIsNone(commit)
                latest.assert_called_once_with(environment["PLAN_URL"])
                report.assert_not_called()
                self.assertEqual(command("git", "--git-dir", str(work / "remote.git"), "rev-parse", "feed-staging"), before)
                shutil.rmtree("feed-work")
                Path("feed.index").unlink(missing_ok=True)
            if publication != 4:
                output.unlink()
        repeated_unit = unit
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
        with patch.dict(os.environ, dict(FEED_BRANCH="feed-staging", CALLBACK_URL="", WAVE_ID="rpm", GPG_FINGERPRINT=fingerprint)):
            published, commit = publish()
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
        command("gpgconf", "--homedir", home, "--kill", "gpg-agent")
        return None

    def test_already_published_success_callback_uses_current_build_and_commit(self):
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        environment = dict(PLAN_URL="", PACKAGES_GPG_PRIVATE_KEY="fixture", OPENWRT_USIGN_PRIVATE_KEY="fixture",
                           OPENWRT_APK_PRIVATE_KEY="fixture")
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            original = Path.cwd()
            os.chdir(temporary)
            try:
                with patch.dict(os.environ, environment), patch("publish.gpg"), patch("publish.os.umask"), \
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
        environment = dict(PLAN_URL="https://fixture.invalid/plan", WAVE_ID="w1", GITHUB_RUN_ID="42")
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            original = Path.cwd()
            os.chdir(temporary)
            try:
                with patch.dict(os.environ, environment), \
                        patch("common.fetch", return_value=b'{"wave":"w1","owner_run_id":43}'), \
                        patch("publish.callback") as report:
                    main()
                report.assert_not_called()
                self.assertEqual(list(Path.cwd().iterdir()), [])
            finally:
                os.chdir(original)
        return None


if __name__ == "__main__":
    unittest.main()
