"""Contracts for opt-in contextual rendering of later scenario goals."""

import json

import pytest

from tau2.data_model.dynamic_followup import DynamicFollowupConfig
from tau2.data_model.message import AssistantMessage, SystemMessage, UserMessage
from tau2.user.dynamic_followup import (
    classify_status_affect,
    generate_dynamic_followup,
    infer_status_affect,
)


def test_qwen_dynamic_followup_config_defaults_off_and_validates_stages():
    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

    assert QwenAgentConfig().dynamic_followup.enabled is False
    with pytest.raises(ValueError, match="stages"):
        DynamicFollowupConfig(stages=[1, 3])


def test_status_affect_inference_is_coarse_and_grounded():
    messages = [
        AssistantMessage(
            role="assistant",
            content="It is still running and the email has not been sent.",
        ),
        UserMessage(
            role="user",
            content="Please make sure you do not contact the wrong person.",
        ),
    ]
    status, affect = infer_status_affect(messages, [])
    assert status == "incomplete"
    assert affect == "cautious"


def test_llm_context_classifier_uses_only_dialogue_and_structured_labels(monkeypatch):
    captured = {}

    def generate(**kwargs):
        captured.update(kwargs)
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    "status": "partial",
                    "affect": "relieved but still cautious",
                    "rationale": "The caller accepted the saved items but held the email.",
                    "evidence": ["The caller said to leave the email unsent."],
                }
            ),
            cost=0.1,
            generation_time_seconds=0.2,
        )

    monkeypatch.setattr("tau2.user.dynamic_followup.generate", generate)
    context, reply = classify_status_affect(
        model="test-model",
        model_args={},
        conversation=[
            UserMessage(role="user", content="Please hold off on the email."),
            AssistantMessage(role="assistant", content="The listings are saved."),
            SystemMessage(role="system", content="hidden tool result"),
        ],
        max_context_characters=2000,
    )
    assert context.status == "partial"
    assert context.affect == "relieved but still cautious"
    assert reply.cost == 0.1
    assert captured["call_name"] == "dynamic_followup_context"
    assert captured["temperature"] == 0
    prompt = captured["messages"][1].content
    assert "Please hold off on the email." in prompt
    assert "The listings are saved." in prompt
    assert "hidden tool result" not in prompt
    assert "tool GT" in captured["messages"][0].content


def test_dynamic_followup_is_disabled_by_default():
    config = DynamicFollowupConfig()
    assert config.enabled is False
    assert config.stages == [1, 2]


def test_dynamic_followup_preserves_goal_values(monkeypatch):
    verification_prompts = []
    verification_system_prompts = []
    generation_system_prompts = []
    generation_user_prompts = []

    def generate(**kwargs):
        if kwargs["call_name"] == "dynamic_followup_check":
            verification_prompts.append(kwargs["messages"][1].content)
            verification_system_prompts.append(kwargs["messages"][0].content)
            return AssistantMessage(
                role="assistant",
                content=json.dumps(
                    {
                        "valid": True,
                        "reason": "",
                        "missing_requirements": [],
                        "invented_requirements": [],
                        "missing_tools": [],
                        "extra_tools": [],
                        "leaked_information": [],
                    }
                ),
                cost=0.1,
                generation_time_seconds=0.2,
            )
        generation_system_prompts.append(kwargs["messages"][0].content)
        generation_user_prompts.append(kwargs["messages"][1].content)
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    "utterance": (
                        "That email is ready. Please send it to "
                        "mbeaumont@gaia2mail.com with the subject "
                        '"Dallas Apartment Recommendation".'
                    ),
                    "covered_requirements": ["send the email"],
                    "referenced_entities": ["the prepared email"],
                    "new_requirements": [],
                }
            ),
            cost=0.2,
            generation_time_seconds=0.4,
        )

    monkeypatch.setattr("tau2.user.dynamic_followup.generate", generate)
    candidate, reply, verification, verification_reply = result = (
        generate_dynamic_followup(
            model="test-model",
            model_args={},
            stage=1,
            source_request=(
                "Send an email to mBEAUMONT@gaia2mail.com with the subject "
                '"Dallas Apartment Recommendation".'
            ),
            conversation=[],
            released_facts=[],
            config=DynamicFollowupConfig(enabled=True, stages=[1], max_retries=0),
            tool_call_gt=[
                {
                    "event_id": "send-email",
                    "tool": "Email__send",
                    "arguments": {"to": "mbeaumont@gaia2mail.com"},
                }
            ],
        )
    )
    assert len(result) == 4
    assert "Dallas Apartment Recommendation" in candidate.utterance
    assert candidate.new_requirements == []
    assert reply.cost == 0.2
    assert verification.valid is True
    assert verification_reply.cost == 0.1
    assert "Email__send" in verification_prompts[0]
    assert "Dallas Apartment Recommendation" in verification_prompts[0]
    assert "must not expose" in verification_system_prompts[0]
    assert "change in tone" in verification_system_prompts[0]
    assert "unsupported_historical_claims" in verification_system_prompts[0]
    assert "USER/AGENT HISTORY" in verification_system_prompts[0]
    verification_system = verification_system_prompts[0].replace("\n", " ")
    assert "reopen that stage" in verification_system
    assert "reopen that stage" in verification_system_prompts[0]

    assert "Adjust the utterance's tone" in generation_system_prompts[0]
    assert "do not reopen it" in generation_system_prompts[0]
    assert "PREVIOUS STAGE STATUS: unknown" in generation_user_prompts[0]
    assert "CALLER AFFECT: neutral" in generation_user_prompts[0]


def test_dynamic_followup_rejects_unsupported_historical_claim(monkeypatch):
    calls = []

    def generate(**kwargs):
        calls.append(kwargs["call_name"])
        if kwargs["call_name"] == "dynamic_followup_request":
            return AssistantMessage(
                role="assistant",
                content=json.dumps(
                    {
                        "utterance": (
                            "Now that the right Aurelia is confirmed, please email "
                            "alice@example.com with subject Follow-up."
                        ),
                        "covered_requirements": ["send the email"],
                        "referenced_entities": [],
                        "new_requirements": [],
                    }
                ),
            )
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    # The caller-side guard must honor the dedicated claim
                    # list even if a checker model inconsistently says valid.
                    "valid": True,
                    "reason": "The history says the contact was unresolved.",
                    "missing_requirements": [],
                    "invented_requirements": [],
                    "missing_tools": [],
                    "extra_tools": [],
                    "leaked_information": [],
                    "unsupported_historical_claims": [
                        "The right Aurelia was confirmed earlier."
                    ],
                }
            ),
        )

    monkeypatch.setattr("tau2.user.dynamic_followup.generate", generate)
    try:
        generate_dynamic_followup(
            model="test-model",
            model_args={},
            stage=1,
            source_request=(
                'Email Alice at alice@example.com. The subject is "Follow-up".'
            ),
            conversation=[
                UserMessage(
                    role="user",
                    content="I don't know which Aurelia is right; hold off on the email.",
                )
            ],
            released_facts=[],
            config=DynamicFollowupConfig(enabled=True, stages=[1], max_retries=0),
            tool_call_gt=[],
        )
    except ValueError as error:
        assert "failed independent verification" in str(error)
    else:
        raise AssertionError("unsupported historical claim was accepted")
    assert calls == ["dynamic_followup_request", "dynamic_followup_check"]


def test_dynamic_followup_rejects_new_requirements(monkeypatch):
    def generate(**kwargs):
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    "utterance": "Also book me a flight to Tokyo.",
                    "covered_requirements": ["send the email"],
                    "referenced_entities": [],
                    "new_requirements": ["book a flight"],
                }
            ),
        )

    monkeypatch.setattr("tau2.user.dynamic_followup.generate", generate)
    try:
        generate_dynamic_followup(
            model="test-model",
            model_args={},
            stage=2,
            source_request="Send the prepared email.",
            conversation=[],
            released_facts=[],
            config=DynamicFollowupConfig(enabled=True, stages=[2]),
            tool_call_gt=[],
        )
    except ValueError as error:
        assert "changed or omitted" in str(error)
    else:
        raise AssertionError("unsafe dynamic request was accepted")


def test_dynamic_followup_retries_after_failed_semantic_check(monkeypatch):
    generation_messages = []
    check_count = 0

    def generate(**kwargs):
        nonlocal check_count
        if kwargs["call_name"] == "dynamic_followup_request":
            generation_messages.append(list(kwargs["messages"]))
            return AssistantMessage(
                role="assistant",
                content=json.dumps(
                    {
                        "utterance": "Please send the prepared email to alice@example.com.",
                        "covered_requirements": ["send the email"],
                        "referenced_entities": ["the prepared email"],
                        "new_requirements": [],
                    }
                ),
            )
        check_count += 1
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    "valid": check_count > 1,
                    "reason": "missing send operation" if check_count == 1 else "",
                    "missing_requirements": (
                        ["send the email"] if check_count == 1 else []
                    ),
                    "invented_requirements": [],
                    "missing_tools": ["Email__send"] if check_count == 1 else [],
                    "extra_tools": [],
                    "leaked_information": [],
                }
            ),
        )

    monkeypatch.setattr("tau2.user.dynamic_followup.generate", generate)
    _, _, verification, _ = generate_dynamic_followup(
        model="test-model",
        model_args={},
        stage=1,
        source_request="Send the prepared email to alice@example.com.",
        conversation=[],
        released_facts=[],
        config=DynamicFollowupConfig(enabled=True, stages=[1], max_retries=1),
        tool_call_gt=[{"tool": "Email__send", "arguments": {}}],
    )
    assert verification.valid is True
    assert check_count == 2
    assert len(generation_messages) == 2
    assert len(generation_messages[1]) == len(generation_messages[0]) + 1


def test_dynamic_followup_rejects_answer_leakage_from_tool_gt(monkeypatch):
    generation_count = 0
    check_count = 0

    def generate(**kwargs):
        nonlocal check_count, generation_count
        if kwargs["call_name"] == "dynamic_followup_request":
            generation_count += 1
            utterance = (
                "The search found Lianying. Please send the message to her."
                if generation_count == 1
                else "Please send the message to the person found in the search."
            )
            return AssistantMessage(
                role="assistant",
                content=json.dumps(
                    {
                        "utterance": utterance,
                        "covered_requirements": ["send the message"],
                        "referenced_entities": [],
                        "new_requirements": [],
                    }
                ),
            )
        check_count += 1
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    "valid": check_count > 1,
                    "reason": "answer leaked" if check_count == 1 else "",
                    "missing_requirements": [],
                    "invented_requirements": [],
                    "missing_tools": [],
                    "extra_tools": [],
                    "leaked_information": ["Lianying"] if check_count == 1 else [],
                }
            ),
        )

    monkeypatch.setattr("tau2.user.dynamic_followup.generate", generate)
    candidate, _, verification, _ = generate_dynamic_followup(
        model="test-model",
        model_args={},
        stage=1,
        source_request="Send a message to the person found in the search.",
        conversation=[],
        released_facts=[],
        config=DynamicFollowupConfig(enabled=True, stages=[1], max_retries=1),
        tool_call_gt=[
            {
                "tool": "Chats__search",
                "arguments": {"result": "Lianying"},
            }
        ],
    )
    assert candidate.utterance == (
        "Please send the message to the person found in the search."
    )
    assert verification.valid is True
    assert generation_count == check_count == 2
