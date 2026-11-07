"""Historical rescoring validates the original artifact binding after reseals."""

from pathlib import Path

import pytest

from tau2.data_model.simulation import Results
from tau2.domains.gaia2.recorded import archive_data, validate_recorded_data


def test_archived_data_verifies_embedded_tasks_and_rejects_tampering(tmp_path):
    from tau2.data_model.simulation import Info, SimulationRun
    from tau2.domains.gaia2.environment import get_tasks
    from tau2.environment.environment import EnvironmentInfo

    task = get_tasks()[0]
    root = archive_data(Path("data/tau2/domains/gaia2"), tmp_path / "archive")
    result = Results(
        info=Info(
            git_commit="test",
            num_trials=1,
            max_steps=1,
            max_errors=1,
            user_info={"implementation": "test"},
            agent_info={"implementation": "test"},
            environment_info=EnvironmentInfo(domain_name="gaia2", policy="test"),
        ),
        tasks=[task],
        simulations=[
            SimulationRun(
                id="test",
                task_id=task.id,
                start_time="start",
                end_time="end",
                duration=1,
                termination_reason="max_steps",
            )
        ],
    )
    assert validate_recorded_data(result, root) == root
    altered = result.model_copy(deep=True)
    altered.tasks[0].id = "changed"
    with pytest.raises(ValueError):
        validate_recorded_data(altered, root)
    p = root / "pack.yaml"
    p.write_bytes(p.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="integrity"):
        validate_recorded_data(result, root)


def test_strict_replay_normalizes_only_unordered_participant_membership():
    from tau2.domains.gaia2.tools import Gaia2Tools

    left = [
        {
            "participant_ids": ["a", "b"],
            "messages": [{"content": "first"}, {"content": "second"}],
        }
    ]
    right = [
        {
            "participant_ids": ["b", "a"],
            "messages": [{"content": "first"}, {"content": "second"}],
        }
    ]
    assert Gaia2Tools.replay_result_equal(
        "Messages__list_recent_conversations", left, right
    )
    right[0]["participant_ids"] = ["a", "c"]
    assert not Gaia2Tools.replay_result_equal(
        "Messages__list_recent_conversations", left, right
    )
    right[0]["participant_ids"] = ["a", "b"]
    right[0]["messages"].reverse()
    assert not Gaia2Tools.replay_result_equal(
        "Messages__list_recent_conversations", left, right
    )
