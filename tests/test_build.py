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
                                    REQUEST_ID="b1", ATTEMPT="1", RUNNER_ARCH="X64"))
        return None

    def test_selectors_match_only_the_distribution_and_release(self):
        declaration = dict(build_dependencies={"deb": ["base"], "rpm": ["native"], "openwrt": ["feed"],
                                               "rpm@rocky-9": ["compiler"], "openwrt@openwrt-24.10": ["extra"]})
        for id, expected in (("rocky-9-x86_64", ["native", "compiler"]), ("rocky-10-x86_64", ["native"]),
                             ("debian-trixie-amd64", ["base"]), ("openwrt-24.10-x86_64", ["feed", "extra"]),
                             ("openwrt-25.12-x86_64", ["feed"])):
            self.assertEqual(resolved_packages(declaration, target(id)), expected)
        return None

    def test_request_validation_rejects_mismatching_owner_environment_and_closure(self):
        work = unit("fixture", repo="owner/fixture", target=target("debian-trixie-amd64"), contract="Xv2",
                    feed_commit="f" * 40, closure=[unit("lib", repository="owner/lib", bindings=[])])
        request = dict(request="b1", attempt=1, inputs=work)
        self.assertEqual(build.check_request(request), work)
        for key, value in (("request", "b2"), ("attempt", 2)):
            with self.assertRaises(ValueError):
                build.check_request(dict(request, **{key: value}))
        with patch.dict(os.environ, RUNNER_ARCH="ARM64"), self.assertRaisesRegex(ValueError, "runner"):
            build.check_request(request)
        for key, value in (("feed_commit", "feed"), ("commit", "main"), ("repo", "owner/../invalid")):
            with self.assertRaises(ValueError):
                build.check_request(dict(request, inputs=dict(work, **{key: value})))
        work["closure"][0]["bindings"] = [dict(package="later")]
        with self.assertRaisesRegex(ValueError, "topological"):
            build.check_request(request)
        return None

    def test_catalog_environment_is_content_addressed(self):
        targets = json.loads((ROOT / "ci/targets.json").read_text())["targets"]
        for item in targets:
            self.assertTrue(matches(r"[^@]+@sha256:[0-9a-f]{64}", item["image"]))
            if item["family"] in ("ipk", "apk"):
                sdk = item["sdk"]
                self.assertTrue(matches(r"[0-9a-f]{64}", sdk["sha256"]))
                for feed in sdk["feeds"]:
                    self.assertRegex(feed, r"\^[a-f0-9]{40}$")
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

    def test_openwrt_registers_complete_closure_before_resolving_feeds_in_one_sdk(self):
        library = unit("lib", repository="owner/lib", bindings=[])
        middleware = unit("mid", repository="owner/mid", bindings=[dict(package="lib")])
        app = unit("app", repo="owner/app", closure=[library, middleware], target=target("openwrt-25.12-x86_64"))
        graph = {"lib": [], "mid": ["lib"], "app": ["mid"]}

        def download(url, path):
            name = path.stem
            write_json(path, dict(schema=2, package=name, depends_on=[dict(package=p) for p in graph[name]],
                                  build_dependencies=dict(openwrt=["dependency"]),
                                  build=dict(openwrt="ci/package/build.sh")))
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
        register = "ci/package/openwrt-register.sh"
        self.assertEqual(trace, [("lib", register, []), ("mid", register, ["lib"]), ("app", register, ["mid"]),
                                "resolve feeds", "install", ("lib", "ci/package/build.sh", []),
                                ("mid", "ci/package/build.sh", ["lib"]), ("app", "ci/package/build.sh", ["mid"]),
                                "validate output"])
        self.assertEqual(list(Path("artifact").glob("*.apk")), [Path("artifact/app.apk")])
        return None
