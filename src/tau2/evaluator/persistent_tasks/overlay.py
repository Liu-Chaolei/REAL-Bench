"""Explicit final-stage reference revisions for authorized task mutations."""

from pathlib import Path

from tau2.data_model.tasks import RewardType
from tau2.persistent_tasks.scores import consolidate_score


def verify_overlay(config, source, root):
    """Verify a private derived reference without resealing the source dataset."""
    from tau2.domains.gaia2.utils import verify_task_artifact
    from tau2.domains.gaia2.verify import _check_reference

    verify_task_artifact(Path(root), source)
    derived = config.overlay.derive(source)
    import json

    manifest = json.loads((Path(root) / "manifest.json").read_text())
    result = _check_reference(
        derived.model_dump(mode="json"), str(root), manifest["assets"]
    )
    if not result["valid"] or result.get("authoring_check_failures"):
        raise ValueError("derived mutation reference verification failed")
    return result


def evaluate_overlay(
    simulation,
    task,
    *,
    evaluation_type,
    solo_mode,
    domain,
    mode,
    env_kwargs,
    strict_replay,
    communicate_scoring,
    customer_language,
):
    """Report original and derived outcomes over the same recorded operations."""
    from tau2.evaluator.evaluator import evaluate_simulation
    from tau2.evaluator.outcome import finalize_reward
    from tau2.evaluator.persistent_tasks.grader import grade
    from tau2.persistent_tasks.config import PersistentTaskConfig

    summary = simulation.info["persistent_tasks"]
    config = PersistentTaskConfig.model_validate(summary["config"])
    config.validate_task(task)
    cancellations = [
        op
        for op in summary["operations"]
        if op.get("operation") == "cancel"
        and op.get("success") is True
        and op.get("completed_at") is not None
    ]
    retained = set()
    if cancellations:
        cancelled_at = min(op["completed_at"] for op in cancellations)
        retained = {
            row["event_id"]
            for row in simulation.environment_trace or []
            if row.get("kind") == "anchor"
            and row.get("event_id")
            and row["time"] <= cancelled_at
        }
    derived = config.overlay.derive(task, completed_before_cancel=retained)
    independent = simulation.model_copy(
        update={
            "info": {
                k: v for k, v in simulation.info.items() if k != "persistent_tasks"
            }
        }
    )
    kwargs = dict(
        evaluation_type=evaluation_type,
        solo_mode=solo_mode,
        domain=domain,
        mode=mode,
        strict_replay=strict_replay,
        communicate_scoring=communicate_scoring,
        customer_language=customer_language,
    )
    original = evaluate_simulation(independent, task, env_kwargs=env_kwargs, **kwargs)
    # The source remains verified and immutable. Only this evaluator-owned,
    # explicitly validated derived reference may bypass source byte equality.
    derived_kwargs = {**(env_kwargs or {}), "task": derived, "allow_unverified": True}
    revised = evaluate_simulation(
        independent, derived, env_kwargs=derived_kwargs, **kwargs
    )
    persistent = consolidate_score(grade(summary, communicate_scoring))
    persistent["retained_before_cancel"] = sorted(
        retained & set(config.overlay.superseded_event_ids)
    )
    # Business execution is a separately reported outcome. Only the explicit
    # combined task reward composes it with native management performance.
    summary["grading"] = persistent
    reward = revised if config.combine_reward else original
    reward.info = {
        **(reward.info or {}),
        "original_gaia2_reward": original.model_dump(mode="json"),
        "derived_gaia2_reward": revised.model_dump(mode="json"),
    }
    if config.combine_reward:
        reward.reward_basis = list(
            set(reward.reward_basis or []) | {RewardType.PERSISTENT_TASK}
        )
    return finalize_reward(simulation, reward)
