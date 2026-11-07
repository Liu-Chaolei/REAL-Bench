"""Post-call evidence windows for one continuous, jointly scored task."""

from tau2.data_model.message import AssistantMessage, SystemMessage
from tau2.data_model.simulation import RewardInfo
from tau2.data_model.tasks import RewardType
from tau2.orchestrator.scenario_stages import SERIES_VERSION


def validate_delivery(task, summary):
    """Validate recorded stage identities and positions; return undelivered stages."""
    import math

    from tau2.orchestrator.scenario_stages import stage_requests

    if not isinstance(summary, dict) or not summary.get("version"):
        raise ValueError("Continuous tasks require their actual stage delivery record")
    rows = summary.get("stages")
    if not isinstance(rows, list) or len(rows) != len(task.scenario.stages):
        raise ValueError("Continuous stage delivery record has the wrong shape")
    previous = None
    gap = False
    missing = []
    for stage, request, row in zip(
        task.scenario.stages, stage_requests(task.scenario), rows
    ):
        if row.get("stage") != stage.stage or row.get("event_id") != request.id:
            raise ValueError(
                "Continuous stage delivery identities differ from the task"
            )
        start, end = row.get("started"), row.get("completed")
        for position in (start, end):
            if position is not None and (
                not isinstance(position, dict)
                or not isinstance(position.get("time"), (int, float))
                or not math.isfinite(position["time"])
                or any(
                    type(position.get(k)) is not int or position[k] < 0
                    for k in ("index", "sequence")
                )
            ):
                raise ValueError("Invalid continuous delivery position")
        if end is not None and (start is None or end["time"] < start["time"]):
            raise ValueError("Continuous request completed before it started")
        if start is not None:
            if gap or (previous is not None and start["time"] < previous):
                raise ValueError("Continuous requests were delivered out of order")
            previous = end["time"] if end else start["time"]
        if end is None:
            missing.append(stage.stage)
            gap = True
    return missing


def communication_reward(task, records, summary, scoring, language, bindings):
    """Evaluate all required facts within their actual playback windows."""
    from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

    checks = []
    rows = summary["stages"]
    for index, stage in enumerate(task.scenario.stages):
        started = rows[index]["started"]
        following = rows[index + 1]["started"] if index + 1 < len(rows) else None
        messages = []
        if started is not None:
            lower = started["time"]
            upper = following["time"] if following else float("inf")
            messages = [
                AssistantMessage(role="assistant", content=str(row["result"]))
                for row in records or []
                if row["kind"] == "agent"
                and lower <= row["time"] < upper
                and row.get("result")
            ]
        checks.extend(
            CommunicateEvaluator.evaluate_communicate_info(
                messages,
                stage.criteria.get("communicate_info") or [],
                scoring=scoring,
                customer_language=language,
                runtime_bindings=bindings,
            )
        )
    reward = float(all(check.met for check in checks))
    return RewardInfo(
        reward=reward,
        communicate_checks=checks,
        reward_breakdown={RewardType.COMMUNICATE: reward},
        info={
            "series_version": SERIES_VERSION,
            "communicate_scoring": scoring.model_dump(mode="json"),
        },
    )


def boundary_evidence(summary):
    """Describe time windows to the existing process judge, after the call only."""
    import json

    return SystemMessage(
        role="system",
        content=(
            "Continuous task stage boundaries (scenario Unix seconds). Evaluate each "
            "stage's conditions only in its actual window, ending at the next request. "
            "A user STOP records interaction closure, not business success. Later "
            "corrections cannot satisfy an earlier required response; explicit later "
            "revisions are allowed. Recorded tool results, not completion claims, prove "
            "application effects. A stage with no started position was never requested; "
            "do not invent dialogue or credit its completion. A started stage without "
            "a completed position was only partially delivered.\n"
            + json.dumps(summary, ensure_ascii=False)
        ),
    )
