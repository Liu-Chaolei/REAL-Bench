"""Outcome scoring accepts equivalent executions and rejects missing effects."""

import gzip
import hashlib
import json

import pytest

pytest.importorskip("termcolor")

from tau2.data_model.message import ToolCall
from tau2.data_model.scenario import ScenarioAction, ScenarioSpec
from tau2.data_model.tasks import (
    EnvAssertion,
    EvaluationCriteria,
    RewardType,
    Task,
    UserScenario,
)
from tau2.domains.gaia2.environment import get_environment
from tau2.domains.gaia2.scoring import check_goal
from tau2.domains.gaia2.utils import canonical_json
from tau2.evaluator.scenario import calculate_scenario_reward


def make_environment(tmp_path, snapshot, actions, resources=None):
    raw = canonical_json(snapshot)
    digest = hashlib.sha256(raw).hexdigest()
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / f"{digest}.json.gz").write_bytes(
        gzip.compress(raw, mtime=0)
    )
    assets = tmp_path / "assets"
    assets.mkdir()
    for relative, content in (resources or {}).items():
        path = assets / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (tmp_path / "manifest.json").write_text(json.dumps({"assets": str(assets)}))
    scene = ScenarioSpec(
        start_time=1728975600,
        source_id="fixture",
        source_sha256="0" * 64,
        snapshot=digest,
        category="execution",
        events=[
            ScenarioAction(
                id="u",
                kind="user",
                tool="input",
                arguments={"content": "Perform the requested work"},
            )
        ]
        + actions,
    )
    task = Task(
        id="fixture",
        scenario=scene,
        user_scenario=UserScenario(instructions="Perform the requested work"),
        evaluation_criteria=EvaluationCriteria(
            actions=[],
            env_assertions=[
                EnvAssertion(
                    env_type="assistant",
                    func_name="assert_scenario_goal",
                    arguments={"source_id": "fixture"},
                )
            ],
            reward_basis=[RewardType.ENV_ASSERTION],
        ),
    )
    environment = get_environment(task=task, root=tmp_path, allow_unverified=True)
    environment.scenario_runtime.delivered(
        "user", "Perform the requested work", event_id="u"
    )
    return task, environment


def test_missing_and_wrong_state_fail_but_correct_state_replays(tmp_path):
    snapshot = [
        {
            "name": "Contacts",
            "class_name": "Contacts",
            "app_state": {
                "contacts": {
                    "original": {
                        "contact_id": "original",
                        "first_name": "Alice",
                        "last_name": "Example",
                        "is_user": True,
                        "age": 30,
                    }
                },
                "view_limit": 10,
            },
        }
    ]
    task, environment = make_environment(
        tmp_path,
        snapshot,
        [
            ScenarioAction(
                id="edit",
                kind="reference",
                tool="Contacts__edit_contact",
                arguments={"contact_id": "original", "updates": {"age": 31}},
                dependencies=["u"],
            )
        ],
    )
    try:
        assert not check_goal(environment.tools)
        wrong = ToolCall(
            id="wrong",
            name="Contacts__edit_contact",
            arguments={"contact_id": "original", "updates": {"age": 32}},
            requestor="assistant",
        )
        assert not environment.get_response(wrong).error
        assert not check_goal(environment.tools)
        right = ToolCall(
            id="right",
            name="Contacts__edit_contact",
            arguments={"contact_id": "original", "updates": {"age": 31}},
            requestor="assistant",
        )
        assert not environment.get_response(right).error
        assert check_goal(environment.tools)
        trace = [
            row.model_dump(mode="json") for row in environment.scenario_runtime.records
        ]
        reward = calculate_scenario_reward(
            get_environment,
            task,
            trace,
            env_kwargs={"task": task, "root": tmp_path, "allow_unverified": True},
        )
        assert reward.reward == 1
        bad = list(trace)
        bad = [
            dict(row, result="a forged output") if row["kind"] == "tool" else row
            for row in bad
        ]
        with pytest.raises(ValueError, match="replay differs"):
            calculate_scenario_reward(
                get_environment,
                task,
                bad,
                env_kwargs={"task": task, "root": tmp_path, "allow_unverified": True},
            )
        relaxed = calculate_scenario_reward(
            get_environment,
            task,
            bad,
            env_kwargs={"task": task, "root": tmp_path, "allow_unverified": True},
            compare_tool_results=False,
        )
        assert relaxed.reward == 1
        assert relaxed.info["replay_warnings"] == [
            {"tool": "Contacts__edit_contact", "count": 2}
        ]
    finally:
        environment.close()


def test_extra_cab_quotes_do_not_change_the_booking_goal(tmp_path):
    snapshot = [
        {
            "name": "Cabs",
            "class_name": "CabApp",
            "app_state": {
                "ride_history": [],
                "quotation_history": [],
                "d_service_config": {
                    "Default": {
                        "nb_seats": 4,
                        "price_per_km": 1,
                        "base_delay_min": 5,
                        "max_distance_km": 25,
                    }
                },
            },
        }
    ]
    arguments = {
        "start_location": "Home",
        "end_location": "Airport",
        "service_type": "Default",
        "ride_time": "2024-10-21 10:00:00",
    }
    task, environment = make_environment(
        tmp_path,
        snapshot,
        [
            ScenarioAction(
                id="book",
                kind="reference",
                tool="Cabs__order_ride",
                arguments=arguments,
                dependencies=["u"],
            )
        ],
    )
    try:
        for index in range(3):
            assert not environment.get_response(
                ToolCall(
                    id=f"quote{index}",
                    name="Cabs__get_quotation",
                    arguments=arguments,
                    requestor="assistant",
                )
            ).error
        assert not environment.get_response(
            ToolCall(
                id="book",
                name="Cabs__order_ride",
                arguments=arguments,
                requestor="assistant",
            )
        ).error
        assert check_goal(environment.tools)
        environment.tools.apps["Cabs"].ride_history[
            -1
        ].end_location = "Wrong destination"
        assert not check_goal(environment.tools)
    finally:
        environment.close()


def test_file_effects_are_scored_and_read_materialization_is_neutral(tmp_path):
    snapshot = [
        {
            "name": "Files",
            "class_name": "SandboxLocalFileSystem",
            "app_state": {
                "files": {
                    "name": "/",
                    "type": "directory",
                    "children": [
                        {
                            "name": "notes.txt",
                            "type": "file",
                            "real_path": "demo_filesystem/notes.txt",
                            "children": [],
                        }
                    ],
                }
            },
        }
    ]
    task, environment = make_environment(
        tmp_path,
        snapshot,
        [
            ScenarioAction(
                id="move",
                kind="reference",
                tool="Files__mv",
                arguments={"path1": "/notes.txt", "path2": "/renamed.txt"},
                dependencies=["u"],
            )
        ],
        resources={"demo_filesystem/notes.txt": "The original content\n"},
    )
    try:
        assert not check_goal(environment.tools)
        result = environment.get_response(
            ToolCall(
                id="read",
                name="Files__cat",
                arguments={"path": "/notes.txt"},
                requestor="assistant",
            )
        )
        assert not result.error and "original content" in result.content
        assert not check_goal(environment.tools)
        result = environment.get_response(
            ToolCall(
                id="move",
                name="Files__mv",
                arguments={"path1": "/notes.txt", "path2": "/renamed.txt"},
                requestor="assistant",
            )
        )
        assert not result.error
        assert check_goal(environment.tools)
        filesystem = environment.tools.apps["Files"]
        with pytest.raises(PermissionError):
            from pathlib import Path

            filesystem._validate_path(
                "../" + Path(filesystem.tmpdir).name + "_outside/secret"
            )
    finally:
        environment.close()
