"""Exercise the declared ARMv7 containers through the real chain startup path."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import chain


def probe(container, target):
    chain.setup_run("container probe", "docker", "exec", container, "/bin/true")
    return None


chain.setup_run("runner", "uname", "-a")
chain.setup_run("runner", "lscpu")
targets = json.loads((ROOT / "ci/targets.json").read_text())["targets"]
targets = [target for target in targets if target["platform"] == "linux/arm/v7"]
assert targets, "No declared ARMv7 containers exercised"
failed = []
temporary = Path("/tmp/codex/build/Packages/container-startup")
temporary.mkdir(parents=True, exist_ok=True)
original = Path.cwd()
for target in targets:
    target = dict(target, units=[dict(package="probe", build_id="probe", action="build", dependencies=[])])
    with tempfile.TemporaryDirectory(dir=temporary) as directory:
        os.chdir(directory)
        Path("keys").mkdir()
        try:
            with patch.dict(os.environ, TARGET=json.dumps(target), CALLBACK_URL=""), \
                    patch.object(chain, "setup", side_effect=probe), patch.object(chain, "build", return_value={}):
                chain.main()
        except Exception as error:
            failed.append(target["id"])
            print(f'{target["id"]}: {error}', flush=True)
        finally:
            os.chdir(original)
assert not failed, "Container startup failed: " + ", ".join(failed)
