"""Explicit SDK signing qualification: python3 tests/openwrt_signing.py SDK_DIR."""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from indices import index as build_index


def main():
    tools = Path(sys.argv[1]).resolve() / "staging_dir/host/bin"
    root = Path("/tmp/codex/build/Packages/tests")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        directory = Path(temporary)
        keys = directory / "keys"
        keys.mkdir()
        subprocess.run([str(tools / "usign"), "-G", "-s", str(directory / "usign.key"),
                        "-p", str(keys / "usign.pub")], check=True)
        control = b"Package: fixture\nVersion: 1.0.0-r1\nArchitecture: x86_64\nDescription: Signing fixture\n"
        inner_data = io.BytesIO()
        with tarfile.open(fileobj=inner_data, mode="w:gz") as archive:
            member = tarfile.TarInfo("./control")
            member.size = len(control)
            archive.addfile(member, io.BytesIO(control))
        ipk = directory / "openwrt/24.10/x86_64"
        ipk.mkdir(parents=True)
        with tarfile.open(ipk / "fixture_1.0.0-r1_x86_64.ipk", "w:gz") as archive:
            member = tarfile.TarInfo("./control.tar.gz")
            member.size = len(inner_data.getvalue())
            archive.addfile(member, io.BytesIO(inner_data.getvalue()))
        build_index(directory, {}, dict(family="ipk", distribution="openwrt", release="24.10", arch="x86_64"),
                    None, tools, None, {"usign": directory / "usign.key"})
        index = ipk / "Packages"
        assert "SHA256sum:" in index.read_text()
        subprocess.run([str(tools / "usign"), "-V", "-p", str(keys / "usign.pub"), "-m", str(index),
                        "-x", str(ipk / "Packages.sig")], check=True)
        subprocess.run(["openssl", "genrsa", "-traditional", "-out", str(directory / "apk.pem"), "2048"], check=True)
        subprocess.run(["openssl", "rsa", "-in", str(directory / "apk.pem"), "-pubout", "-out", str(keys / "apk.pub")], check=True)
        apk = directory / "openwrt/25.12/x86_64"
        apk.mkdir(parents=True)
        files = directory / "files"
        files.mkdir()
        (files / "fixture").write_text("fixture\n")
        subprocess.run([str(tools / "apk"), "mkpkg", "--info", "name:fixture", "--info", "version:1.0.0-r1",
                        "--info", "arch:x86_64", "--files", str(files), "--output", str(apk / "fixture-1.0.0-r1.apk")], check=True)
        subprocess.run([str(tools / "apk"), "mkpkg", "--info", "name:fixture", "--info", "version:1.0.0-r2",
                        "--info", "arch:x86_64", "--files", str(files), "--output", str(apk / "fixture-1.0.0-r2.apk")], check=True)
        build_index(directory, {}, dict(family="apk", distribution="openwrt", release="25.12", arch="x86_64"),
                    None, tools, None, {"apk": directory / "apk.pem"})
        subprocess.run([str(tools / "apk"), "verify", "--keys-dir", str(keys), str(apk / "packages.adb")], check=True)
        downloads = directory / "downloads"
        downloads.mkdir()
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(apk)))
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            repository = f"http://127.0.0.1:{server.server_port}/packages.adb"
            fetch = [str(tools / "apk"), "--no-cache", "--arch", "x86_64", "--keys-dir", str(keys), "--repository", repository,
                     "fetch", "--all-matches", "--output", str(downloads), "fixture"]
            subprocess.run(fetch, check=True)
            assert (downloads / "fixture-1.0.0-r1.apk").read_bytes() == (apk / "fixture-1.0.0-r1.apk").read_bytes()
            assert len(list(downloads.glob("*.apk"))) == 2
            install_root = directory / "installed"
            install_root.mkdir()
            repositories = directory / "repositories"
            repositories.touch()
            tool = str(tools / "apk")
            subprocess.run([tool, "--root", str(install_root), "--arch", "x86_64", "--initdb", "--usermode",
                            "--allow-untrusted", "--repositories-file", str(repositories), "add",
                            "--force-non-repository", "--no-scripts", str(downloads / "fixture-1.0.0-r1.apk")], check=True)
            output = subprocess.check_output([tool, "--root", str(install_root), "query", "--installed",
                                              "--fields", "name,version,arch", "--format", "json", "fixture"])
            assert dict(name="fixture", version="1.0.0-r1", arch="x86_64") in json.loads(output)
            (downloads / "fixture-1.0.0-r1.apk").unlink()
            (files / "fixture").write_text("replaced package\n")
            subprocess.run([str(tools / "apk"), "mkpkg", "--info", "name:fixture", "--info", "version:1.0.0-r1",
                            "--info", "arch:x86_64", "--files", str(files), "--output", str(apk / "fixture-1.0.0-r1.apk")], check=True)
            assert subprocess.run(fetch, capture_output=True).returncode != 0, "signed index accepted a replaced package"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        print("OpenWrt ipk and apk index signing/verification passed")
    return None


if __name__ == "__main__":
    os.umask(0o077)
    main()
