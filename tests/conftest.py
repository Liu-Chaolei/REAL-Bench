import sys
from pathlib import Path

import pytest

# Add test directories to path for fixture imports
tests_dir = Path(__file__).parent
sys.path.insert(0, str(tests_dir / "test_streaming"))


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-api",
        action="store_true",
        default=False,
        help="Run live_api tests; requires provider credentials and may incur costs",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live-api"):
        return
    skip = pytest.mark.skip(reason="Live model calls require --run-live-api")
    for item in items:
        if item.get_closest_marker("live_api") is not None:
            item.add_marker(skip)
