"""Current evidence access and explicit boundaries for retired task protocols."""


def evidence_source(agent):
    """Return the Agent's optional public, passive backend evidence port."""
    getter = getattr(agent, "get_async_evidence_source", None)
    return getter() if getter else None


def reject_retired_task(task):
    """Reject a retired embedded scenario before any provider is started."""
    scenario = getattr(task, "scenario", None)
    embedded = getattr(scenario, "async_interaction", None)
    if (
        embedded is not None
        and embedded.get("version") != "gaia2-interaction-variant-v1"
    ):
        raise ValueError(
            "Legacy Persistent Task scenarios are retired; use active_metrics"
        )
    criteria = getattr(task, "evaluation_criteria", None)
    if "PERSISTENT_TASK" in (getattr(criteria, "reward_basis", None) or []):
        raise ValueError("Legacy PERSISTENT_TASK rewards are retired")


def reject_retired_simulation(simulation, task=None):
    """Old results can be viewed, but cannot be regraded using current rules."""
    if (simulation.info or {}).get("persistent_tasks") is not None:
        raise ValueError(
            "Legacy Persistent Task regrading is retired; view saved scores"
        )
    if task is not None:
        reject_retired_task(task)


def reject_retired_results(results):
    """Reject resuming an old experiment while allowing Results.load for viewing."""
    if results.info.persistent_task_config is not None:
        raise ValueError("Legacy Persistent Task checkpoint resume is retired")
    for task in results.tasks:
        reject_retired_task(task)
    for simulation in results.simulations:
        reject_retired_simulation(simulation)
