import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build
from common import digest, matches, resolved_packages, write_json


def target(id):
    targets = json.loads((ROOT / "ci/targets.json").read_text())["targets"]
    result = next(target for target in targets if target["id"] == id)
    return result


def unit(package, **fields):
    result = dict(package=package, version="1.2.3", revision=4, commit="a" * 40, ci_commit="b" * 40, **fields)
    return result


class BuildTests(unittest.TestCase):
    def setUp(self):
        root = Path("/tmp/codex/build/Packages/tests")
        root.mkdir(parents=True, exist_ok=True)
        temporary = self.enterContext(tempfile.TemporaryDirectory(dir=root))
        self.enterContext(chdir(temporary))
        self.enterContext(patch.dict(os.environ, GITHUB_REPOSITORY="fixture/packages",
                                    REQUEST_ID="b.1", ATTEMPT="1", RUNNER_ARCH="X64"))
        return None

    def test_selectors_match_distribution_release_and_optional_architecture(self):
        declaration = dict(build_dependencies={"rpm": ["native"], "rpm@rocky-9": ["compiler"],
                                               "rpm@rocky-9-x86_64": ["x86"], "deb@debian-trixie-amd64": ["amd64"],
                                               "openwrt@openwrt-24.10-aarch64_cortex-a53": ["extra"]})
        for id, expected in (("rocky-9-x86_64", ["native", "compiler", "x86"]), ("rocky-9-aarch64", ["native", "compiler"]),
                             ("rocky-10-x86_64", ["native"]), ("debian-trixie-amd64", ["amd64"]),
                             ("debian-trixie-arm64", []), ("openwrt-24.10-aarch64_cortex-a53", ["extra"]),
                             ("openwrt-24.10-x86_64", []), ("openwrt-25.12-x86_64", [])):
            self.assertEqual(resolved_packages(declaration, target(id)), expected)
        return None

    def test_request_validation_rejects_mismatching_owner_environment_and_closure(self):
        Path("ci").mkdir()
        (Path("ci") / "targets.json").write_bytes((ROOT / "ci/targets.json").read_bytes())
        work = unit("fixture", repo="owner/fixture", target=target("debian-trixie-amd64"), contract="Xv2",
                    feed_commit="f" * 40, closure=[unit("lib", repository="owner/lib", bindings=[])])
        request = dict(request="b.1", attempt=1, inputs=work)
        self.assertEqual(build.check_request(request), work)
        for key, value in (("request", "b2"), ("attempt", 2)):
            with self.assertRaises(ValueError):
                build.check_request(dict(request, **{key: value}))
        with patch.dict(os.environ, RUNNER_ARCH="ARM64"), self.assertRaisesRegex(ValueError, "runner"):
            build.check_request(request)
        with patch.dict(os.environ, REQUEST_ID="arm.1"), self.assertRaisesRegex(ValueError, "runner"):
            build.check_request(dict(request, request="arm.1"))
        for key, value in (("feed_commit", "feed"), ("commit", "main"), ("repo", "owner/../invalid")):
            with self.assertRaises(ValueError):
                build.check_request(dict(request, inputs=dict(work, **{key: value})))
        work["closure"][0]["bindings"] = [dict(package="later")]
        with self.assertRaisesRegex(ValueError, "topological"):
            build.check_request(request)
        return None

    def test_runner_label_and_architecture_come_from_the_catalog(self):
        Path("ci").mkdir()
        write_json(Path("ci/targets.json"), dict(runners={"fixture-new-runner": dict(request_prefix="new", runner_arch="RISCV64")}))
        frozen = dict(target("debian-trixie-amd64"), runner="fixture-new-runner")
        inputs = unit("fixture", repo="owner/fixture", target=frozen, contract="Xv2", feed_commit="f" * 40, closure=[])
        request = dict(request="new.1", attempt=1, inputs=inputs)
        with patch.dict(os.environ, REQUEST_ID="new.1", RUNNER_ARCH="RISCV64"):
            self.assertEqual(build.check_request(request), inputs)
        return None

    def test_catalog_environment_is_content_addressed(self):
        targets = json.loads((ROOT / "ci/targets.json").read_text())["targets"]
        for item in targets:
            self.assertTrue(matches(r"[^@]+@sha256:[0-9a-f]{64}", item["image"]))
            if item["family"] in ("ipk", "apk"):
                sdk = item["sdk"]
                self.assertTrue(matches(r"[0-9a-f]{64}", sdk["sha256"]))
                self.assertEqual(sum(" base " in feed for feed in sdk["feeds"]), 1, item["id"])
                for feed in sdk["feeds"]:
                    self.assertRegex(feed, r"\^[a-f0-9]{40}$")
                self.assertEqual(sum("/kmods/" in repo for repo in sdk["runtime_repositories"]), 1, item["id"])
                if item["family"] == "ipk":
                    bootstrap = sdk["runtime_bootstrap"]
                    self.assertEqual({Path(entry["url"]).name.split("_")[0] for entry in bootstrap}, {"kernel", "libc"})
                    self.assertTrue(all(matches(r"[0-9a-f]{64}", entry["sha256"]) for entry in bootstrap))
        return None

    def test_exact_inventory_uses_pinned_commit_and_verifies_each_split(self):
        Path("work").mkdir()
        content = b"package"
        Path("payload").write_bytes(content)
        checksum = digest("payload")
        paths = [f"apt/debian/pool/lib/{name}_1.2.3-4~trixie_all.deb" for name in ("lib", "lib-dev")]
        inventory = [dict(name=name, path=path, served_sha256=checksum, version="1.2.3-4~trixie", arch="all")
                     for name, path in zip(("lib", "lib-dev"), paths)]
        inputs = dict(target=target("debian-trixie-amd64"), feed_commit="d" * 40, closure=[dict(inventory=inventory)])

        def download(url, path):
            path.write_bytes(content)
            return None

        with patch("build.fetch", side_effect=download) as fetch, patch("build.install") as install, \
                patch("build.docker", return_value=Mock(stdout="1.2.3-4~trixie\tall")) as docker:
            build.exact_dependencies(inputs)
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         ["https://raw.githubusercontent.com/fixture/packages/" + "d" * 40 + "/" + path for path in paths])
        install.assert_called_once_with("deb", ["/work/locked/" + Path(path).name for path in paths])
        self.assertEqual([call.args[-1] for call in docker.call_args_list], ["lib:all", "lib-dev:all"])
        for path in Path("work/locked").iterdir():
            path.unlink()
        Path("work/locked").rmdir()
        with patch("build.fetch", side_effect=download), patch("build.install"), \
                patch("build.docker", return_value=Mock(stdout="1.2.3-3~trixie\tall")), \
                self.assertRaisesRegex(ValueError, "installed identity mismatch"):
            build.exact_dependencies(inputs)
        return None

    def test_checkout_at_source_and_ci_commit_independently(self):
        work = Path("fixture")
        (work / "ci").mkdir(parents=True)
        git = lambda *args: subprocess.run(["git", "-C", str(work), *args], check=True, text=True,
                                            capture_output=True).stdout.strip()
        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "commit.gpgsign", "false")
        (work / "payload").write_text("tag source")
        (work / "ci/build.sh").write_text("old build Hook")
        git("add", ".")
        git("commit", "-qm", "source")
        source = git("rev-parse", "HEAD")
        (work / "payload").write_text("new source")
        (work / "ci/build.sh").write_text("new build hook")
        git("commit", "-qam", "ci change")
        ci = git("rev-parse", "HEAD")
        real_run = build.run

        def local_run(*args, **kwargs):
            arguments = [str(work.resolve()) if str(arg).startswith("https://github.com/") else arg for arg in args]
            result = real_run(*arguments, **kwargs)
            return result

        with patch("build.run", side_effect=local_run):
            build.checkout(dict(package="fixture", repo="owner/fixture", commit=source, ci_commit=ci))
        self.assertEqual(Path("work/source/fixture/payload").read_text(), "tag source")
        self.assertEqual(Path("work/ci/fixture/build.sh").read_text(), "new build hook")
        return None

    def test_ipk_install_creates_its_offline_root_lock_directory(self):
        paths = ["/work/locked/lib_1.0.0-r1_x86_64.ipk"]
        release = target("openwrt-24.10-x86_64")
        bootstrap = ["/work/" + Path(entry["url"]).name for entry in release["sdk"]["runtime_bootstrap"]]
        operations = []

        def opkg(*arguments):
            self.assertTrue(Path("work/root/tmp").is_dir())
            self.assertEqual(arguments[3:5], ("--offline-root", "/work/root"))
            operations.append(arguments[5:])
            return None

        with patch("build.docker", side_effect=opkg):
            build.openwrt_install(release, paths)
        self.assertEqual(operations, [("update",), ("install", *bootstrap, *paths)])
        return None

    def test_runtime_bootstrap_rejects_an_unverified_package(self):
        release = target("openwrt-24.10-x86_64")

        def unpack(_, directory):
            directory.mkdir(parents=True)
            return None

        def download(_, path):
            path.write_bytes(b"wrong bootstrap package")
            return None

        with patch("build.unpack_sdk", side_effect=unpack), patch("build.fetch", side_effect=download), \
                self.assertRaisesRegex(ValueError, "runtime bootstrap SHA256 mismatch"):
            build.openwrt_setup(release)
        self.assertFalse(Path("work/sdk/feeds.conf").exists())
        return None

    def test_openwrt_registers_complete_closure_before_resolving_feeds_in_one_sdk(self):
        library = unit("lib", repository="owner/lib", bindings=[])
        middleware = unit("mid", repository="owner/mid", bindings=[dict(package="lib")])
        app = unit("app", repo="owner/app", closure=[library, middleware], target=target("openwrt-25.12-x86_64"))
        graph = {"lib": [], "mid": ["lib"], "app": ["mid"]}

        def download(url, path):
            name = path.stem
            write_json(path, dict(schema=2, package=name, depends_on=[dict(package=p) for p in graph[name]],
                                  build_dependencies=dict(openwrt=["dependency"]),
                                  build=dict(openwrt="ci/package/build.sh", openwrt_register=f"ci/hooks/{name}-register.sh")))
            return None

        def invoke(unit, target, action, dependencies):
            trace.append((unit["package"], action, [item["package"] for item in dependencies]))
            if action.endswith("/build.sh"):
                output = Path("work/output") / unit["package"]
                output.mkdir()
                (output / (unit["package"] + ".apk")).write_bytes(unit["package"].encode())
            return None

        trace = []
        with patch("build.fetch", side_effect=download), patch("build.hook", side_effect=invoke), \
                patch("build.run"), patch("build.base_setup"), patch("build.openwrt_setup") as sdk, \
                patch("build.builder", side_effect=lambda *args: trace.append("resolve feeds")), \
                patch("build.checkout"), patch("build.exact_dependencies", side_effect=lambda _: trace.append("install")), \
                patch("build.openwrt_install", side_effect=lambda *_: trace.append("validate output")), \
                patch("build.subprocess.run"), patch("build.metadata", return_value={"name": "app"}):
            build.build(app)
        sdk.assert_called_once_with(app["target"])
        self.assertEqual(trace, [("lib", "ci/hooks/lib-register.sh", []), ("mid", "ci/hooks/mid-register.sh", ["lib"]),
                                ("app", "ci/hooks/app-register.sh", ["mid"]),
                                "resolve feeds", "install", ("lib", "ci/package/build.sh", []),
                                ("mid", "ci/package/build.sh", ["lib"]), ("app", "ci/package/build.sh", ["mid"]),
                                "validate output"])
        self.assertEqual(list(Path("artifact").glob("*.apk")), [Path("artifact/app.apk")])
        return None
