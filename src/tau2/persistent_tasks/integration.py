"""Evaluator-private integration with the ordinary voice scenario runtime."""

from dataclasses import replace
from queue import Empty, SimpleQueue

from tau2.persistent_tasks.availability import (
    check_capabilities,
    check_task,
    evidence_source,
)
from tau2.persistent_tasks.config import PersistentTaskConfig
from tau2.persistent_tasks.runtime.coordinator import (
    PersistentTaskCoordinator as BackendTaskRuntime,
)
from tau2.persistent_tasks.runtime.finalization import finalize


def install(orchestrator, config):
    """Validate and attach native observation before any model connection."""
    config = PersistentTaskConfig.model_validate(config)
    check_task(orchestrator.task, config)
    if orchestrator.domain != "gaia2":
        raise ValueError("Async tasks require the Gaia2 domain")
    source = evidence_source(orchestrator.agent)
    check_capabilities(config, source)
    if (
        orchestrator.call_context
        and orchestrator.call_context.language.language != "en"
    ):
        raise ValueError("persistent tasks v1 requires English")
    if config.overlay:
        from tau2.evaluator.persistent_tasks.overlay import verify_overlay

        verify_overlay(config, orchestrator.task, orchestrator.environment.root)
    coordinator = BackendTaskRuntime(config)
    coordinator.record_capabilities(source.get_capabilities())
    orchestrator.user.scenario_goal_update_ids = {
        request.request_id
        for request in config.requests
        if request.operation in {"modify", "cancel"}
    }
    runtime = orchestrator.environment.scenario_runtime
    orchestrator.persistent_task_runtime = coordinator
    orchestrator.async_evidence_source = source
    source.enable()
    completions = SimpleQueue()
    orchestrator._persistent_source_events = completions
    runtime.add_event_completion_listener(
        lambda event_id, result, timestamp: completions.put(
            (event_id, result, timestamp)
        )
    )
    from tau2.orchestrator.scenario_stages import stage_requests as source_requests

    stage_requests = {
        event.id: index
        for index, event in enumerate(source_requests(orchestrator.task.scenario))
    }
    if config.initial_request_prefix:
        initial_id = source_requests(orchestrator.task.scenario)[0].id
        original_queue = orchestrator.user.queue_scenario_input

        def queue_public_request(event_id, text):
            if event_id == initial_id:
                text = config.initial_request_prefix + "\n\n" + text
            return original_queue(event_id, text)

        orchestrator.user.queue_scenario_input = queue_public_request

    def delivered(role, _text, event_id, timestamp):
        if role == "agent":
            coordinator.agent_delivery_seen(timestamp)
        if role == "user" and event_id:
            if event_id in stage_requests:
                coordinator.activate_stage(stage_requests[event_id], timestamp)
            coordinator.delivery_started(event_id, timestamp)

    runtime.add_delivery_listener(delivered)
    orchestrator.environment.persistent_task_summary = lambda: finalize(orchestrator)
    return coordinator


def advance(orchestrator, boundary):
    """Drain in the orchestration thread; only this thread mutates projections."""
    coordinator = getattr(orchestrator, "persistent_task_runtime", None)
    if coordinator is None:
        return
    runtime = orchestrator.environment.scenario_runtime
    if not runtime.started:
        return
    completions = orchestrator._persistent_source_events
    while True:
        try:
            event_id, result, timestamp = completions.get_nowait()
        except Empty:
            break
        coordinator.source_event_completed(event_id, result, timestamp)
    for event in orchestrator.async_evidence_source.drain_events():
        if event.received_monotonic is None:
            coordinator.issues.append({"reason": "missing_receiver_clock"})
            continue
        when = runtime.timestamp_at(event.received_monotonic)
        coordinator.observe(replace(event, timestamp=when))
    for request in coordinator.advance(runtime.time(), boundary):
        with runtime.lock:
            runtime.user_inputs.append((request.request_id, request.text))
