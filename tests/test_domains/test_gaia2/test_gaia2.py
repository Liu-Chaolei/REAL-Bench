"""Native Gaia2 conversion, isolated application state and business scoring."""

import pytest

pytest.importorskip("termcolor")

from tau2.data_model.message import ToolCall
from tau2.domains.gaia2.importer import decode_arguments
from tau2.domains.gaia2.scoring import _equivalent, business_state


def test_optional_source_arguments_and_json_lists_are_decoded_safely():
    action = {
        "args": [
            {"name": "age", "value": "", "value_type": "Optional[int]"},
            {
                "name": "recipients",
                "value": '["a@example.com"]',
                "value_type": "list[str]",
            },
        ]
    }
    assert decode_arguments(action) == {"age": None, "recipients": ["a@example.com"]}
    with pytest.raises(ValueError):
        decode_arguments(
            {"args": [{"name": "active", "value": "not true", "value_type": "bool"}]}
        )


def test_generated_ids_preserve_relationships_and_entity_multiplicity():
    a, b, c = "a" * 32, "b" * 32, "c" * 32
    d, e, f = "d" * 32, "e" * 32, "f" * 32
    expected = {
        "contacts": {
            a: {"contact_id": a, "name": "Alice"},
            b: {"contact_id": b, "name": "Bob"},
        },
        "meeting": {"event_id": c, "attendee_id": a},
    }
    actual = {
        "contacts": {
            d: {"contact_id": d, "name": "Alice"},
            e: {"contact_id": e, "name": "Bob"},
        },
        "meeting": {"event_id": f, "attendee_id": d},
    }
    assert _equivalent(business_state(actual, set()), business_state(expected, set()))
    actual["meeting"]["attendee_id"] = e
    assert not _equivalent(
        business_state(actual, set()), business_state(expected, set())
    )


def test_generated_entity_mapping_backtracks_across_creation_order():
    a, b, c, d = "a" * 32, "b" * 32, "c" * 32, "d" * 32
    expected = {
        "records": {
            a: {"event_id": a, "name": "Alice"},
            b: {"event_id": b, "name": "Bob"},
        },
        "selected": a,
    }
    actual = {
        "records": {
            c: {"event_id": c, "name": "Bob"},
            d: {"event_id": d, "name": "Alice"},
        },
        "selected": d,
    }
    assert _equivalent(business_state(actual, set()), business_state(expected, set()))
    actual["selected"] = c
    assert not _equivalent(
        business_state(actual, set()), business_state(expected, set())
    )


def test_explicit_labels_remain_literal_while_free_text_can_be_semantic(monkeypatch):
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.data_model.simulation import CommunicateCheck

    monkeypatch.setattr(
        "tau2.evaluator.evaluator_communicate.CommunicateEvaluator.evaluate_communicate_info",
        lambda *args, **kwargs: [
            CommunicateCheck(
                info="Work Party", met=True, justification="Equivalent meaning"
            )
        ],
    )
    scoring = CommunicateScoring()
    assert _equivalent(
        {"title": "Party at work"}, {"title": "Work Party"}, scoring=scoring
    )
    assert not _equivalent(
        {"title": "Party at work"},
        {"title": "Work Party"},
        scoring=scoring,
        literal_texts={"Work Party"},
    )


def test_voice_literal_matching_ignores_case_and_punctuation_but_keeps_words():
    assert _equivalent(
        {"subject": "properties list!"},
        {"subject": "Properties List"},
        literal_texts={"Properties List"},
        relaxed_literal_texts={"Properties List"},
    )
    assert not _equivalent(
        {"subject": "Property List"},
        {"subject": "Properties List"},
        literal_texts={"Properties List"},
        relaxed_literal_texts={"Properties List"},
    )
    assert not _equivalent(
        {"subject": "List Properties"},
        {"subject": "Properties List"},
        literal_texts={"Properties List"},
        relaxed_literal_texts={"Properties List"},
    )


def test_voice_literal_allowlist_covers_the_audited_sources():
    from tau2.domains.gaia2.scoring import _VOICE_RELAXED_LITERAL_SOURCES

    assert len(_VOICE_RELAXED_LITERAL_SOURCES) == 177
    assert "0052_snsq4rd73gytoo0wr75bgd2nyzra07z1" in (_VOICE_RELAXED_LITERAL_SOURCES)


def test_location_source_rules_keep_environment_fragments_exact():
    from tau2.domains.gaia2.location_rules import VOICE_LOCATION_RULES

    source = "0097_kxccpkni1h796gejnt95f5yz1jottv2j"
    expected = "Fort Worth Flats, 9012 W 7th St, Fort Worth"
    rules = VOICE_LOCATION_RULES[source]
    assert _equivalent(
        {"start_location": "Fort Worth Flats; 9012 W 7th St, Fort Worth"},
        {"start_location": expected},
        location_rules=rules,
    )
    assert not _equivalent(
        {"start_location": "fort worth flats, 9012 w 7th st, fort worth"},
        {"start_location": expected},
        location_rules=rules,
    )
    assert _equivalent(
        {"end_location": "meir 90 leuven belgium"},
        {"end_location": "Meir 90, Leuven, Belgium"},
        location_rules={"end_location": {"Meir 90, Leuven, Belgium": {"mode": "user"}}},
    )


def test_domain_uses_standard_registry_and_all_source_ids():
    from tau2.domains.gaia2.environment import get_tasks, get_tasks_split
    from tau2.domains.gaia2.utils import GAIA2_TASKS_PATH
    from tau2.registry import registry

    if not GAIA2_TASKS_PATH.exists():
        pytest.skip("Gaia2 data not imported")
    tasks = get_tasks()
    splits = get_tasks_split()
    expected = 1100 if "async" in splits else (1000 if "mind_change" in splits else 800)
    assert len(tasks) == expected
    assert len(set(t.id for t in tasks)) == expected
    assert set(splits["base"]) == {t.id for t in tasks}
    assert all(
        len(splits[name]) == 160
        for name in ["search", "execution", "ambiguity", "adaptability", "time"]
    )
    if expected >= 1000:
        assert len(splits["mind_change"]) == len(splits["repetition"]) == 100
    assert registry.get_env_constructor("gaia2")


def test_sealed_tasks_reject_changed_bytes_and_substituted_task_objects(tmp_path):
    import hashlib
    import json

    from tau2.data_model.tasks import Task, UserScenario
    from tau2.domains.gaia2.environment import get_tasks
    from tau2.domains.gaia2.utils import canonical_json, verify_task_artifact

    task = Task(id="frozen", user_scenario=UserScenario(instructions="Original task"))
    raw = canonical_json([task.model_dump(mode="json")])
    path = tmp_path / "tasks.json"
    path.write_bytes(raw)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"verified": True, "tasks_sha256": hashlib.sha256(raw).hexdigest()})
    )
    assert get_tasks(root=tmp_path) == [task]
    verify_task_artifact(tmp_path, task)
    changed = task.model_copy(deep=True)
    changed.user_scenario.instructions = "A different objective"
    with pytest.raises(ValueError, match="sealed task definition"):
        verify_task_artifact(tmp_path, changed)
    path.write_bytes(raw + b"\n")
    with pytest.raises(ValueError, match="sealed manifest"):
        get_tasks(root=tmp_path)


def test_five_parallel_trials_keep_application_rng_clock_and_state_isolated(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tau2.data_model.scenario import ScenarioSpec
    from tau2.domains.gaia2.tools import Gaia2Tools
    from tau2.environment.environment import Environment
    from tau2.environment.scenario import ScenarioRuntime

    snapshot = [
        {
            "name": "Contacts",
            "class_name": "Contacts",
            "app_state": {"contacts": {}, "view_limit": 10},
        }
    ]
    barrier = Barrier(5)

    def trial(seed, synchronize):
        runtime = ScenarioRuntime(
            ScenarioSpec(
                start_time=1000 + seed,
                source_id=str(seed),
                source_sha256="0" * 64,
                snapshot="1" * 64,
                category="test",
                events=[],
            ),
            lambda *args: None,
        )
        tools = Gaia2Tools(snapshot, assets=tmp_path, clock=runtime, seed=seed)
        env = Environment("gaia2", "Isolation fixture", tools=tools)
        env.scenario_runtime = runtime
        try:
            if synchronize:
                barrier.wait(timeout=5)
            result = env.get_response(
                ToolCall(
                    id="same-call-id-in-each-trial",
                    name="Contacts__add_new_contact",
                    arguments={"first_name": f"Person{seed}", "last_name": "Test"},
                    requestor="assistant",
                )
            )
            assert not result.error, result.content
            identifier = result.content
            clock = tools.get_current_time()["current_timestamp"]
            contacts = tools.db.apps["Contacts"]["contacts"]
            assert len(contacts) == 1
            assert contacts[identifier]["first_name"] == f"Person{seed}"
            return identifier, clock
        finally:
            env.close()
            tools.close()

    expected = [trial(seed, False) for seed in range(5)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        actual = list(pool.map(lambda seed: trial(seed, True), range(5)))
    assert actual == expected
    assert [clock for _, clock in actual] == [1000 + seed for seed in range(5)]


def test_real_application_tools_mutate_only_the_selected_trial(tmp_path):
    from tau2.domains.gaia2.tools import Gaia2Tools

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
    first = Gaia2Tools(snapshot, assets=tmp_path)
    second = Gaia2Tools(snapshot, assets=tmp_path)
    try:
        from tau2.environment.environment import Environment

        environment = Environment("gaia2", "Fixture policy", tools=first)
        result = environment.get_response(
            ToolCall(
                id="edit",
                name="Contacts__edit_contact",
                arguments={"contact_id": "original", "updates": {"age": 31}},
                requestor="assistant",
            )
        )
        assert not result.error
        assert first.db.apps["Contacts"]["contacts"]["original"]["age"] == 31
        assert second.db.apps["Contacts"]["contacts"]["original"]["age"] == 30
        assert "assert_scenario_goal" not in first.get_tools()
    finally:
        first.close()
        second.close()
