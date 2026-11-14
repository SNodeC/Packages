"""Explicit SDK signing qualification: python3 tests/openwrt_signing.py SDK_DIR."""
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from publish import ipk_index


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
        with tarfile.open(directory / "fixture_1.0.0-r1_x86_64.ipk", "w:gz") as archive:
            member = tarfile.TarInfo("./control.tar.gz")
            member.size = len(inner_data.getvalue())
            archive.addfile(member, io.BytesIO(inner_data.getvalue()))
        ipk_index(directory)
        index = directory / "Packages"
        assert "SHA256sum:" in index.read_text()
        subprocess.run([str(tools / "usign"), "-S", "-s", str(directory / "usign.key"), "-m", str(index)], check=True)
        subprocess.run([str(tools / "usign"), "-V", "-p", str(keys / "usign.pub"), "-m", str(index)], check=True)
        subprocess.run(["openssl", "genrsa", "-traditional", "-out", str(directory / "apk.pem"), "2048"], check=True)
        subprocess.run(["openssl", "rsa", "-in", str(directory / "apk.pem"), "-pubout", "-out", str(keys / "apk.pub")], check=True)
        files = directory / "files"
        files.mkdir()
        (files / "fixture").write_text("fixture\n")
        subprocess.run([str(tools / "apk"), "mkpkg", "--info", "name:fixture", "--info", "version:1.0.0-r1",
                        "--info", "arch:x86_64", "--files", str(files), "--output", str(directory / "fixture.apk")], check=True)
        subprocess.run([str(tools / "apk"), "mkndx", "--allow-untrusted", "--sign-key", str(directory / "apk.pem"),
                        "--output", str(directory / "packages.adb"), str(directory / "fixture.apk")], check=True)
        subprocess.run([str(tools / "apk"), "verify", "--keys-dir", str(keys), str(directory / "packages.adb")], check=True)
        print("OpenWrt ipk and apk index signing/verification passed")
    return None


if __name__ == "__main__":
    os.umask(0o077)
    main()
