"""V7 failure certificates from ARE's necessary write-action count condition."""

from __future__ import annotations

import copy
import hashlib
import inspect
from collections import Counter

if __package__:
    from .task_contract import derive_contract, fingerprint
else:
    from task_contract import derive_contract, fingerprint

VERSION = "gaia2-qwen-audio/7"
BASIS = "official_action_count_failure"
AUI = "AgentUserInterface__send_message_to_user"
ARE_IMAGE = "sha256:dbb0c94145258db2e0bbeb3cf7cfa90d3161ac79e0992214c10b16104fcfc36f"
RULE_SHA256 = "9cbca89144fd9597daf57047450e985c561132d5d00b1a98fdb933244d1699e3"
POLICY = {
    "name": BASIS,
    "are_version": "1.2.0",
    "rule_sha256": RULE_SHA256,
    "offline_image": ARE_IMAGE,
    "unknown_speech": "assume_AUI_count_satisfied_only",
    "counts_matching": "remain_unscored",
}


def action_count_proof(scenario, events, contract, source_text):
    from are.simulation.validation.judge import GraphPerEventJudge
    from are.simulation.validation.utils.event_utils import EnvAgentEventFilter

    protocol = contract.get("protocol")
    if protocol not in {VERSION, "gaia2-qwen-audio/8"}:
        raise ValueError("Unsupported action certificate protocol")
    judge = scenario.judge
    if (
        type(judge) is not GraphPerEventJudge
        or type(judge.event_filter) is not EnvAgentEventFilter
    ):
        raise ValueError("Unsupported official action-count rule")
    if (
        scenario.nb_turns != 1
        or derive_contract(scenario, source_text, protocol=protocol) != contract
    ):
        raise ValueError("Action proof requires the identical single-turn contract")

    def select(items):
        selected = []
        # Official filtering normalizes aliases in place. Never mutate the real log.
        for original in items:
            event = copy.copy(original)
            event.action = copy.copy(original.action)
            if (
                judge.event_filter(event)
                and event.event_type.value == "AGENT"
                and event.tool_name != AUI
            ):
                selected.append({"event_id": event.event_id, "tool": event.tool_name})
        return sorted(selected, key=lambda e: e["event_id"])

    actual = select(events)
    expected = select(scenario.oracle_run_event_log)
    a = Counter(e["tool"] for e in actual)
    o = Counter(e["tool"] for e in expected)
    answers = contract["required_answer_count"]
    # Logical best case for speech, not an invented AUI event or transcript.
    verdict = judge.check_tool_call_counts(
        a, answers, o, answers, judge.config.extra_send_message_to_user_allowed
    )
    if bool(verdict.success) != (a == o):
        raise ValueError("Official necessary-count semantics changed")
    proof = {
        "schema": "gaia2-action-decision/1",
        "protocol": protocol,
        "source_sha256": contract["source_sha256"],
        "contract_sha256": fingerprint(contract),
        "judge_policy_sha256": contract["judge_policy_sha256"],
        "are_version": contract["are_version"],
        "rule_sha256": hashlib.sha256(
            (
                inspect.getsource(GraphPerEventJudge.check_tool_call_counts)
                + inspect.getsource(EnvAgentEventFilter)
                + inspect.getsource(type(judge.event_filter.agent_event_filer))
            ).encode()
        ).hexdigest(),
        "actual_events": actual,
        "required_events": expected,
        "actual_counts": dict(sorted(a.items())),
        "required_counts": dict(sorted(o.items())),
        "missing": dict(sorted((o - a).items())),
        "extra": dict(sorted((a - o).items())),
        "speech_assumption": "required_AUI_count_satisfied_without_creating_events",
        "official_check_success": bool(verdict.success),
        "decision": "failure" if not verdict.success else "inconclusive",
        "model_calls": 0,
    }

    if proof["rule_sha256"] != RULE_SHA256:
        raise ValueError("Pinned official counting rule changed")
    return proof


def apply_decision(evidence, proof):
    result = dict(evidence)
    if evidence["task_score_status"] != "unresolved_speech":
        raise ValueError("Count certificate is only for unresolved speech")
    if (
        proof["contract_sha256"] != evidence["contract_sha256"]
        or proof["model_calls"] != 0
    ):
        raise ValueError("Decision proof contract or model calls differ")
    if proof["decision"] == "failure" and proof["official_check_success"] is False:
        result.update(task_score_status="scored", score_basis=BASIS)
    elif (
        proof["decision"] != "inconclusive"
        or proof["official_check_success"] is not True
    ):
        raise ValueError("Inconsistent count certificate")
    result["decision_proof_sha256"] = fingerprint(proof)
    return result
