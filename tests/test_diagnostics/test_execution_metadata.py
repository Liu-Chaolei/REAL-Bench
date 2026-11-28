"""Task-scoped Gaia2 execution metadata loading and caching."""

import json
from types import SimpleNamespace

from tau2.diagnostics.execution import gaia2


class _FakeTool:
    write_operation = True
    class_instance = SimpleNamespace(name="Demo")
    args = [SimpleNamespace(name="value", has_default=True, default="default")]


class _FakeTools:
    snapshots = []

    def __init__(self, snapshot, *, assets):
        del assets
        type(self).snapshots.append(snapshot[0]["snapshot"])
        self._operations = {"Demo__write": _FakeTool()}

    def close(self):
        pass


def _task(snapshot):
    return SimpleNamespace(scenario=SimpleNamespace(snapshot=snapshot))


def _root(tmp_path, snapshots):
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "assets": str(tmp_path / "assets"),
                "runtime_sha256": "runtime",
            }
        )
    )
    (tmp_path / "tasks.json").write_text(
        json.dumps([{"scenario": {"snapshot": snapshot}} for snapshot in snapshots])
    )
    return tmp_path


def test_task_metadata_uses_one_snapshot_and_persistent_cache(tmp_path, monkeypatch):
    root = _root(tmp_path, ["snapshot-a", "snapshot-b"])
    _FakeTools.snapshots = []
    # Patch the providers at their import locations; the optimized path imports
    # them lazily so ordinary module import remains inexpensive.
    from tau2.domains.gaia2 import tools as tools_module
    from tau2.domains.gaia2 import utils as utils_module

    monkeypatch.setattr(tools_module, "Gaia2Tools", _FakeTools)
    monkeypatch.setattr(
        utils_module,
        "read_snapshot",
        lambda snapshot, root: [{"snapshot": snapshot}],
    )

    first = gaia2.metadata_for_task(_task("snapshot-a"), root)
    second = gaia2.metadata_for_task(_task("snapshot-a"), root)

    assert (
        first
        == second
        == {
            "Demo__write": {
                "write": True,
                "app": "Demo",
                "defaults": {"value": "default"},
            }
        }
    )
    assert _FakeTools.snapshots == ["snapshot-a"]
    assert len(list((root / ".execution_metadata_cache").glob("*.json"))) == 1


def test_full_metadata_keeps_explicit_all_snapshot_path(tmp_path, monkeypatch):
    root = _root(tmp_path, ["snapshot-b", "snapshot-a"])
    _FakeTools.snapshots = []
    from tau2.domains.gaia2 import tools as tools_module
    from tau2.domains.gaia2 import utils as utils_module

    monkeypatch.setattr(tools_module, "Gaia2Tools", _FakeTools)
    monkeypatch.setattr(
        utils_module,
        "read_snapshot",
        lambda snapshot, root: [{"snapshot": snapshot}],
    )

    result = gaia2.metadata(root)

    assert list(result) == ["Demo__write"]
    assert _FakeTools.snapshots == ["snapshot-a", "snapshot-b"]
