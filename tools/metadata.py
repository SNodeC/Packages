"""Read runtime inventory from native package metadata."""

import io
import json
import re
import tarfile

from common import NATIVE_NAME, capture, matches, require


def control_fields(text):
    fields = {}
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and fields:
            fields[next(reversed(fields))] += " " + line.strip()
        elif ": " in line:
            name, value = line.split(": ", 1)
            fields[name] = value
    return fields


def ipk_control(path):
    with tarfile.open(path) as archive:
        member = next(m for m in archive if m.name in ("control.tar.gz", "./control.tar.gz"))
        with archive.extractfile(member) as content:
            with tarfile.open(fileobj=io.BytesIO(content.read()), mode="r:gz") as control:
                member = next(m for m in control if m.name in ("control", "./control"))
                with control.extractfile(member) as stream:
                    result = stream.read().decode().rstrip()
    return result


def metadata(family, path, *, in_container=None, apk=None):
    filename = "/" + str(path) if in_container else str(path)

    def inspect(*args):
        if in_container:
            result = in_container(*args, capture_output=True, text=True).stdout.strip()
        else:
            result = capture(*args)
        return result

    if family in ("deb", "ipk"):
        stanza = inspect("dpkg-deb", "--field", filename) if family == "deb" else ipk_control(path)
        fields = control_fields(stanza)
        name, version, arch = (fields[field] for field in ("Package", "Version", "Architecture"))
        depends = [value.strip() for field in ("Pre-Depends", "Depends")
                   for value in fields.get(field, "").split(",") if value.strip()]
        provides = [value.strip() for value in fields.get("Provides", "").split(",") if value.strip()]
    elif family == "rpm":
        name, epoch, version, arch = inspect("rpm", "-qp", "--qf",
                                           "%{NAME}\t%{EPOCHNUM}\t%{VERSION}-%{RELEASE}\t%{ARCH}",
                                           filename).split("\t")
        version = f"{epoch}:{version}" if epoch != "0" else version
        depends = inspect("rpm", "-qp", "--requires", filename).splitlines()
        provides = inspect("rpm", "-qp", "--provides", filename).splitlines()
    else:
        info = json.loads(inspect(apk, "adbdump", "--format", "json", filename))["info"]
        name, version, arch = (info[key] for key in ("name", "version", "arch"))
        depends, provides = info.get("depends", []), info.get("provides", [])
    require(matches(NATIVE_NAME, name) and bool(re.fullmatch(r"[A-Za-z0-9._:+~%-]+", version)) and matches(NATIVE_NAME, arch),
            "invalid native package identity")
    return dict(name=name, version=version, arch=arch, runtime_dependencies=depends, provides=provides)
