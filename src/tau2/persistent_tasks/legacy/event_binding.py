"""Bind provider observations to native Gaia2 scenario events."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from tau2.data_model.scenario import ScenarioAction
from tau2.environment.scenario import equivalent_arguments, resolve_references
from tau2.persistent_tasks.models import ObservedEvent


@dataclass(frozen=True)
class BindingResult:
    """Result of attempting one observation binding."""

    status: str
    event_id: str | None = None
    reason: str | None = None


class EventBinder:
    """Match completed provider tool events to one native scenario event.

    The binder never treats free text or a provider lifecycle event as a native
    completion. It only commits a binding when exactly one eligible native tool
    event matches the tool and verifiable arguments.
    """

    def __init__(
        self,
        events: list[ScenarioAction | Any],
        *,
        normalize_arguments: Callable[[str, dict], dict] | None = None,
        on_bound: Callable[[str, Any, float], None] | None = None,
    ):
        self.events = list(events)
        self.completed: dict[str, float] = {}
        self.results: dict[str, Any] = {}
        self.normalize_arguments = normalize_arguments or (lambda _name, args: args)
        self.on_bound = on_bound

    def bind(self, observed: ObservedEvent) -> BindingResult:
        """Attempt to bind one observed event, returning an auditable status."""

        if observed.kind != "tool_call_completed":
            return BindingResult("ignored", reason="event is not a completed tool call")
        if observed.accepted is False:
            return BindingResult("ignored", reason="provider rejected the call")

        candidates = []
        for event in self.events:
            if event.id in self.completed or event.kind != "reference":
                continue
            if getattr(event, "channel", None) != "tool":
                continue
            if event.tool != observed.tool_name:
                continue
            if not all(parent in self.completed for parent in event.dependencies):
                continue
            try:
                expected = resolve_references(event.arguments, self.results)
            except KeyError:
                continue
            actual_args = self.normalize_arguments(event.tool, observed.arguments)
            expected_args = self.normalize_arguments(event.tool, expected)
            fields = getattr(event, "match_fields", None)
            if fields is not None:
                expected_args = {
                    key: expected_args[key] for key in fields if key in expected_args
                }
                actual_args = {
                    key: actual_args[key] for key in fields if key in actual_args
                }
            if equivalent_arguments(actual_args, expected_args):
                candidates.append(event)

        if len(candidates) == 0:
            return BindingResult("unmatched", reason="no eligible native event matched")
        if len(candidates) > 1:
            return BindingResult("ambiguous", reason="multiple native events matched")

        event = candidates[0]
        self.completed[event.id] = float(observed.timestamp)
        self.results[event.id] = copy.deepcopy(observed.result)
        if self.on_bound is not None:
            self.on_bound(event.id, copy.deepcopy(observed.result), observed.timestamp)
        return BindingResult("bound", event_id=event.id)

    def snapshot(self) -> dict[str, Any]:
        """Return replayable binding state."""

        return {
            "completed": dict(self.completed),
            "results": copy.deepcopy(self.results),
        }
