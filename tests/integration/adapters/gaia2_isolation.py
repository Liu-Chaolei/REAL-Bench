"""Run in the main image as root; verifies the actual backend OS boundary."""

import json
import os
import subprocess
from pathlib import Path

root = Path("/run/evaluation")
root.mkdir(parents=True, exist_ok=True)
root.chmod(0o700)
secret = root / "isolation-control.txt"
secret.write_text("test-only-control-secret")
secret.chmod(0o600)
probe = """
import os
from pathlib import Path
assert os.getuid() != 0
for target in ['/run/evaluation/isolation-control.txt', '/proc/1/environ', '/evaluation/scenario.json']:
    try:
        Path(target).read_bytes()
    except (PermissionError, FileNotFoundError):
        continue
    raise RuntimeError('Backend could read a protected resource: ' + target)
print('protected paths inaccessible')
"""
result = subprocess.run(
    ["runuser", "-u", "node", "--", "python3", "-c", probe],
    check=False,
    capture_output=True,
    text=True,
    env={"PATH": os.environ["PATH"]},
)
if result.returncode:
    raise RuntimeError(result.stderr)
print(
    json.dumps(
        {
            "passed": True,
            "non_root": True,
            "control_secret_unreadable": True,
            "root_process_environment_unreadable": True,
            "reference_not_mounted": True,
        }
    )
)
