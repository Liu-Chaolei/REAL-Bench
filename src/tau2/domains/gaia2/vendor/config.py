"""Local resource defaults; never read global ARE configuration or credentials."""

import tempfile
from pathlib import Path

from tau2.utils.utils import DATA_DIR

ARE_SIMULATION_SANDBOX_PATH = tempfile.gettempdir()
FS_PATH = str(Path(DATA_DIR) / "tau2" / "domains" / "gaia2" / "assets")
DEMO_FS_DIR = "demo_filesystem"
DEMO_FS_PATH = str(Path(FS_PATH) / DEMO_FS_DIR)
