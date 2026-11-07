"""Unplayed user corrections preserve objective, history and accounting."""

import json

import pytest

from tau2.data_model.message import AssistantMessage
from tau2.user.grounding import generate_grounded_reply
from tau2.user.user_simulator_base import UserError


def reply(text, cost=0.1):
    return AssistantMessage(
        role="assistant",
        content=text,
        cost=cost,
        usage={"prompt_tokens": 10},
        generation_time_seconds=0.1,
    )


def test_no_issue_decision_passes_real_response_validation_without_retry(monkeypatch):
    """The five live valid=true/none replies must not terminate a conversation."""
    from tau2.user.grounding import validate_user_candidate

    calls = []

    def provider(*args, **kwargs):
        calls.append(kwargs)
        return reply(
            '{"valid":true,"reason":"No unsupported fact or changed goal",'
            '"issue_kind":"none"}'
        )

    monkeypatch.setattr("tau2.utils.llm_utils._generate_once", provider)
    decision = validate_user_candidate(
        allowed="Confirm the existing order.",
        conversation="agent: I am placing the order.",
        candidate="Please let me know when it is actually placed.",
        model="unused",
        model_args={},
    )
    assert json.loads(decision.content)["valid"] is True
    assert len(calls) == 1


def test_none_classification_does_not_accept_a_rejected_reply(monkeypatch):
    from tau2.user.grounding import GroundingRejected, validate_user_candidate

    monkeypatch.setattr(
        "tau2.utils.llm_utils._generate_once",
        lambda *args, **kwargs: reply(
            '{"valid":false,"reason":"Changed the price","issue_kind":"price"}'
        ),
    )
    with pytest.raises(GroundingRejected, match="Changed the price"):
        validate_user_candidate(
            allowed="Pay exactly 25.",
            conversation="",
            candidate="Pay 30 instead.",
            model="unused",
            model_args={},
        )


@pytest.mark.parametrize("kind", ["none", "unrecognized"])
def test_contradictory_or_unknown_rejection_remains_a_checker_error(monkeypatch, kind):
    from tau2.user.grounding import validate_user_candidate

    calls = []

    def provider(*args, **kwargs):
        calls.append(1)
        return reply(
            json.dumps({"valid": False, "reason": "Invalid", "issue_kind": kind})
        )

    monkeypatch.setattr("tau2.utils.llm_utils._generate_once", provider)
    with pytest.raises(UserError, match="invalid structured decision"):
        validate_user_candidate(
            allowed="Original request",
            conversation="",
            candidate="Candidate",
            model="unused",
            model_args={},
        )
    assert len(calls) == 2


def test_price_change_is_corrected_before_commit(monkeypatch):
    texts = iter(
        ["Buy six at $19.99", "Keep $25 per item; I do not know another variant."]
    )
    prompts, events = [], []

    def generate(feedback):
        prompts.append(list(feedback))
        return reply(next(texts))

    def judge(**kw):
        invalid = "Buy six at $19.99" in kw["messages"][-1].content
        return reply(
            json.dumps(
                {
                    "valid": not invalid,
                    "reason": "Original unit price is $25",
                    "issue_kind": "price",
                }
            ),
            0.05,
        )

    monkeypatch.setattr("tau2.user.grounding.generate", judge)
    result = generate_grounded_reply(
        generate_reply=generate,
        allowed="Six at $25 each",
        conversation="",
        model="unused",
        model_args={},
        is_stop=lambda m: False,
        events=events,
    )
    assert "$25" in result.content
    assert [e["valid"] for e in events] == [False, True]
    assert len(prompts[0]) == 1 and "Original unit price" in prompts[1][-1].content
    assert result.cost == pytest.approx(0.3)
    assert result.usage["prompt_tokens"] == 40


def test_tomorrow_conflict_reviews_audio_once_and_recovers(monkeypatch):
    events, reviews, prompts = [], [], []
    decisions = iter([False, False, True])
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: reply(
            json.dumps(
                {
                    "valid": next(decisions),
                    "reason": "No same-day deadline was supplied",
                    "issue_kind": "time",
                    "heard_evidence": "tomorrow",
                }
            )
        ),
    )

    def generate(feedback):
        prompts.append(feedback.copy())
        return reply(
            "Today!" if len(prompts) < 3 else "Please continue the original task."
        )

    def recheck(evidence):
        reviews.append(evidence)
        return {
            "status": "ok",
            "original_text": "tomorrow",
            "review_text": "the moment",
        }

    result = generate_grounded_reply(
        generate_reply=generate,
        allowed="No deadline",
        conversation="agent: tomorrow",
        model="unused",
        model_args={},
        is_stop=lambda m: False,
        events=events,
        recheck=recheck,
    )
    assert result.content == "Please continue the original task."
    assert reviews == ["tomorrow"]
    assert "the moment" in prompts[1][-1].content


def test_exhaustion_is_bounded_and_keeps_all_reasons(monkeypatch):
    events = []
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: reply('{"valid":false,"reason":"Changed the fixed price"}'),
    )
    with pytest.raises(UserError, match="fixed price"):
        generate_grounded_reply(
            generate_reply=lambda _: reply("wrong"),
            allowed="fixed",
            conversation="",
            model="unused",
            model_args={},
            is_stop=lambda m: False,
            events=events,
        )
    assert len(events) == 3


def test_stop_never_goes_to_grounding(monkeypatch):
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: pytest.fail("STOP must bypass grounding"),
    )
    result = generate_grounded_reply(
        generate_reply=lambda _: reply("###STOP###"),
        allowed="fixed",
        conversation="",
        model="unused",
        model_args={},
        is_stop=lambda m: True,
        events=[],
    )
    assert result.content == "###STOP###"


def test_text_recovery_appends_input_once_and_never_commits_rejected_text(monkeypatch):
    from tau2.user.user_simulator import UserSimulator

    user = UserSimulator(llm="unused", instructions="Six at $25 each")
    user.scenario_constraints = "Six at $25 each"
    state = user.get_init_state()
    replies = iter(["Buy at $19.99", "Keep the original price."])
    prompts = []

    def generate(**kw):
        prompts.append(kw["messages"])
        return reply(next(replies))

    decisions = iter([False, True])
    monkeypatch.setattr("tau2.user.user_simulator.generate", generate)
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: reply(
            json.dumps({"valid": next(decisions), "reason": "Keep $25"})
        ),
    )
    message, state = user.generate_next_message(reply("Proposed price"), state)
    assert [m.content for m in state.messages] == [
        "Proposed price",
        "Keep the original price.",
    ]
    assert all(
        sum(m.content == "Proposed price" for m in prompt) == 1 for prompt in prompts
    )
    assert all("UNPLAYED" not in str(m.content) for m in state.system_messages)
    assert message.cost == pytest.approx(0.4)


def test_names_do_not_trigger_audio_review(monkeypatch):
    decisions = iter([False, True])
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: reply(
            json.dumps(
                {
                    "valid": next(decisions),
                    "reason": "Contact ambiguity",
                    "issue_kind": "time",
                    "heard_evidence": "Aurelia Munteanu",
                }
            )
        ),
    )
    generate_grounded_reply(
        generate_reply=lambda _: reply("Please repeat that."),
        allowed="original",
        conversation="",
        model="unused",
        model_args={},
        is_stop=lambda m: False,
        events=[],
        recheck=lambda _: pytest.fail("Name handling is deferred"),
    )


def test_grounding_uses_its_fixed_snapshot_without_an_obsolete_callback(monkeypatch):
    events, calls = [], []

    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kwargs: reply(
            json.dumps(
                {
                    "valid": True,
                    "reason": "No unsupported fact",
                    "issue_kind": "other",
                }
            )
        ),
    )

    def generate(_):
        calls.append(1)
        return reply("Please continue the original task.")

    result = generate_grounded_reply(
        generate_reply=generate,
        allowed="No deadline",
        conversation="",
        model="unused",
        model_args={},
        is_stop=lambda _: False,
        events=events,
        recheck=lambda _: pytest.fail("No rejected candidate needs audio review"),
        context_revision=52,
    )
    assert result.content == "Please continue the original task."
    assert len(calls) == 1
    assert events[0]["context_revision"] == 52
    assert "obsolete" not in events[0]
