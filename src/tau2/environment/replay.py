"""Replay scenario journals through the same environment operation interface."""

import copy

from tau2.data_model.message import ToolCall
from tau2.data_model.scenario import ScenarioRecord


def replay_environment(
    environment,
    records,
    *,
    strict=True,
    compare_tool_results=True,
    replay_warnings=None,
):
    """Reconstruct only recorded operations with their recorded application time."""
    runtime = environment.scenario_runtime
    runtime.replaying = True
    try:
        for value in records:
            record = (
                value
                if isinstance(value, ScenarioRecord)
                else ScenarioRecord.model_validate(value)
            )
            started = (
                record.started_time if record.started_time is not None else record.time
            )
            runtime._manual = started - runtime.spec.start_time
            if record.kind == "environment":
                with runtime.operation():
                    actual = runtime.execute(
                        record.name, copy.deepcopy(record.arguments)
                    )
                if strict and actual != record.result:
                    raise ValueError(f"Environment replay differs for {record.name}")
                runtime.completed[record.event_id] = (
                    record.time - runtime.spec.start_time
                )
                runtime.results[record.event_id] = actual
                runtime.notifications.append(
                    {
                        "time": record.time,
                        "operation": record.name,
                        "result": copy.deepcopy(actual),
                    }
                )
            elif record.kind == "tool":
                function = environment.tools.tools.get(record.name)
                if getattr(function, "__waits_for_environment__", False):
                    # Reproduce consumption without waiting or draining arrivals
                    # that were recorded after the original wait had returned.
                    for notification in (
                        record.result if isinstance(record.result, list) else []
                    ):
                        if (
                            not isinstance(notification, dict)
                            or not {"time", "operation", "result"}
                            <= notification.keys()
                        ):
                            continue

                        def same_notification(actual):
                            # Gaia2 serializes tool-return scalars as strings.
                            # Compare the exact numeric time, preserving all other
                            # fields and the requirement for a recorded arrival.
                            try:
                                return (
                                    float(actual["time"]) == float(notification["time"])
                                    and actual["operation"] == notification["operation"]
                                    and actual["result"] == notification["result"]
                                )
                            except (KeyError, TypeError, ValueError):
                                return actual == notification

                        matched = next(
                            (
                                i
                                for i, actual in enumerate(runtime.notifications)
                                if same_notification(actual)
                            ),
                            None,
                        )
                        if matched is not None:
                            runtime.notifications.pop(matched)
                        elif strict:
                            raise ValueError(
                                "Wait replay references an undelivered notification"
                            )
                    continue
                before = len(runtime.records)
                call_id = record.call_id or str(record.sequence)
                response = environment.get_response(
                    ToolCall(
                        id=call_id,
                        name=record.name,
                        arguments=copy.deepcopy(record.arguments),
                        requestor="assistant",
                    )
                )
                reproduced = next(
                    item
                    for item in runtime.records[before:]
                    if item.kind == "tool" and item.call_id == call_id
                )
                equal_result = getattr(
                    environment.tools,
                    "replay_result_equal",
                    lambda name, actual, recorded: actual == recorded,
                )
                if response.error != record.error:
                    raise ValueError(f"Tool replay differs for {record.name}")
                if not equal_result(record.name, reproduced.result, record.result):
                    if compare_tool_results and strict:
                        raise ValueError(f"Tool replay differs for {record.name}")
                    if replay_warnings is not None:
                        replay_warnings.append({"tool": record.name})
            elif record.kind == "trigger":
                if runtime.event_triggers is not None:
                    runtime.event_triggers.bindings[record.event_id] = copy.deepcopy(
                        record.result
                    )
            elif record.kind == "anchor":
                runtime.completed[record.event_id] = started - runtime.spec.start_time
                runtime.results[record.event_id] = record.result
                identifier = record.arguments.get("call_id")
                if identifier is not None:
                    runtime._observations = [
                        item for item in runtime._observations if item[4] != identifier
                    ]
            elif record.kind in {"user", "agent"}:
                runtime.delivered(
                    record.kind, str(record.result or ""), event_id=record.event_id
                )
            elif record.kind == "error":
                raise ValueError("Cannot score a failed scenario execution journal")
    finally:
        runtime.replaying = False
