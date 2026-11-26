"""Prefix-only state transitions, explicit completion and opportunity scoring."""

from dataclasses import dataclass

from tau2.diagnostics.state.adapter import DomainAdapter, compare, equal, pointer
from tau2.diagnostics.state.events import build_events
from tau2.diagnostics.state.models import (
    DialogueState,
    Interpretation,
    StateItem,
    StateTransition,
)

PROJECTOR_VERSION = "tau-dialogue-state/2"


def initialize_state(task=None):
    """Only a one-time public contact agenda; hidden task data stays private."""
    state = DialogueState()
    state.agenda["interaction:establish_contact"] = StateItem(
        id="interaction:establish_contact",
        kind="interaction",
        key="establish_contact",
        value="Establish contact once",
        visible_to=["all"],
    )
    return state


def _collection(state, kind):
    return {
        "goal": state.goals,
        "business": state.agenda,
        "interaction": state.agenda,
        "fact": state.facts,
        "constraint": state.constraints,
    }[kind]


def _status(item, status, event, shared=False):
    if not item.status_history:
        item.status_history.append(
            dict(
                status=item.status,
                visible_to=item.visible_to,
                source_refs=list(item.source_refs),
                sequence=item.visible_at,
                blocked_by=item.blocked_by,
            )
        )
    item.status = status
    item.updated_at = event.sequence
    item.status_history.append(
        dict(
            status=status,
            visible_to=["all"] if shared else event.visible_to,
            source_refs=list(item.source_refs) + [event.event_id],
            sequence=event.sequence,
            blocked_by=item.blocked_by,
        )
    )


def _update(state, update, event):
    collection = _collection(state, update.kind)
    old = collection.get(update.id)
    if old and update.mode == "assert" and equal(old.value, update.value):
        return
    if old:
        old.history.append(
            dict(
                value=old.value,
                status="stale"
                if update.mode == "correct" and old.kind == "fact"
                else old.status,
                at=old.updated_at,
                candidates=old.candidates,
                source_refs=list(old.source_refs),
            )
        )
        old.updated_at = event.sequence
        old.source_refs.append(event.event_id)
        if update.mode == "cancel":
            _status(
                old,
                "cancelled",
                event,
                shared=old.kind in ("goal", "business", "interaction", "constraint"),
            )
            for child in state.agenda.values():
                if child.goal_id == old.id:
                    _status(child, "cancelled", event, shared=True)
            return
        if update.mode == "deny":
            _status(old, "denied", event)
            return
        if update.mode == "candidates":
            old.candidates = update.candidates
            _status(old, "conflicted", event)
            return
        old.value = update.value
        _status(
            old,
            "mentioned" if old.kind == "fact" else "open",
            event,
            shared=old.kind != "fact",
        )
        old.candidates = []
        old.completion = update.completion or old.completion
    else:
        collection[update.id] = StateItem(
            id=update.id,
            kind=update.kind,
            key=update.key,
            value=update.value,
            status={
                "candidates": "conflicted",
                "deny": "denied",
                "cancel": "cancelled",
            }.get(update.mode, "mentioned" if update.kind == "fact" else "open"),
            goal_id=update.goal_id,
            candidates=update.candidates,
            source_refs=[event.event_id],
            visible_to=["all"]
            if update.kind in ("goal", "business", "interaction", "constraint")
            else list(event.visible_to),
            visible_at=event.sequence,
            updated_at=event.sequence,
            completion=update.completion,
        )


def _opportunity(use, event, visible):
    collections = ("goals", "agenda", "facts", "constraints", "operations")
    target = next(
        (visible[c][use.item_id] for c in collections if use.item_id in visible[c]),
        None,
    )
    status, reason = use.status, use.reason
    evidence_ids = {
        r
        for c in collections
        for item in visible[c].values()
        for r in item["source_refs"]
    }
    evidence_ids.update(r["id"] for r in visible["dialogue"])
    if any(r not in evidence_ids and r != event.event_id for r in use.evidence_ids):
        status, reason = "uncertain", "evidence_not_visible"
    elif target is None and status == "consistent":
        status, reason = "uncertain", "missing_required_state"
    elif target and use.path:
        expected, found = pointer(target["value"], use.path)
        if not found:
            status, reason = "uncertain", "missing_required_state"
        elif status in ("consistent", "inconsistent") and use.actual is not None:
            status = "consistent" if equal(use.actual, expected) else "inconsistent"
            if status == "inconsistent":
                reason = "contradicted_claim"
    elif (
        target
        and target["kind"] == "fact"
        and use.actual is not None
        and status in ("consistent", "inconsistent")
    ):
        if target["status"] == "conflicted":
            status, reason = "uncertain", "ambiguous_fact"
        elif target["status"] == "denied" and equal(use.actual, target["value"]):
            status, reason = "inconsistent", "stale_state_use"
        elif not equal(use.actual, target["value"]):
            status, reason = (
                "inconsistent",
                "stale_state_use" if target["history"] else "wrong_entity",
            )
    elif (
        target
        and target["kind"] in ("business", "interaction")
        and use.actual is not None
    ):
        condition = target.get("completion") or {}
        if (
            use.mode == "reply"
            and condition.get("comparison") == "exact"
            and condition.get("expected") is not None
        ):
            if not equal(use.actual, condition["expected"]):
                status, reason = "inconsistent", "contradicted_claim"
    elif (
        target
        and target["kind"] == "action"
        and use.actual in ("succeeded", "completed")
    ):
        if target["status"] == "failed":
            status, reason = "inconsistent", "contradicted_claim"
        elif target["status"] not in ("succeeded", "completed"):
            status, reason = "unsupported", "unsupported_claim"
    return dict(
        event_id=event.event_id,
        item_id=use.item_id,
        mode=use.mode,
        before_version=visible["version"],
        status=status,
        reason=reason,
        evidence_ids=use.evidence_ids,
        actual=use.actual,
        path=use.path,
    )


def _advance(state, identity, event, window, satisfied=False):
    item = state.agenda.get(identity)
    if item is None or item.status in ("satisfied", "cancelled"):
        return
    new = "satisfied" if satisfied else "in_progress"
    if item.status == new:
        return
    item.blocked_by = None
    _status(item, new, event)
    item.source_refs.append(event.event_id)
    window.delta.advanced_items.append(identity)
    if satisfied:
        window.delta.satisfied_items.append(identity)
    destination = (
        window.delta.interaction_progress
        if item.kind == "interaction"
        else window.delta.goal_progress
    )
    destination.append(identity)




@dataclass
class StateStepResult:
    """One event's state transition and semantic interpretation."""

    state_before: DialogueState
    interpretation: Interpretation
    state_after: DialogueState
    transition: StateTransition | None


class StateProjector:
    """Incremental state projector shared by replay and the online sidecar."""

    def __init__(self, task=None, interpreter=None, adapter=None, evidence=None):
        self.task = task
        self.interpreter = interpreter
        self.adapter = adapter or DomainAdapter(evidence)
        self.state = initialize_state(task)
        self.versions = [self.state.model_dump(mode="json")]
        self.windows = []
        self.window = StateTransition(window_id="window:1")
        self.pending_relations = {}

    def process_event(self, event) -> StateStepResult:
        """Interpret and apply one event in sequence order."""
        before = self.state.model_copy(deep=True)
        state = self.state
        window = self.window
        adapter = self.adapter
        pending_relations = self.pending_relations
        windows = self.windows
        versions = self.versions
        closed_transition = None
        interpretation = Interpretation()
        window.events.append(event.event_id)
        agent_action = event.kind in ("agent_turn", "tool_requested")
        visible = state.visible(
            event.role
            if agent_action
            else (event.visible_to[0] if event.visible_to else "agent")
        )
        if agent_action:
            if window.state_before_agent is None:
                window.state_before_agent = state.version
            snapshots = (
                window.state_before_tool_action
                if event.kind == "tool_requested"
                else window.state_before_agent_reply
            )
            snapshots[event.event_id] = state.version
        interpretation = Interpretation()
        if event.kind in ("user_turn", "agent_turn", "tool_requested"):
            try:
                if self.interpreter is None or event.metadata.get("incomplete"):
                    raise ValueError("semantic_interpretation_unavailable")
                interpretation = self.interpreter.interpret(event, visible)
            except Exception as exc:
                interpretation.uncertain = True
                window.issues.append(
                    dict(event_id=event.event_id, reason=type(exc).__name__)
                )
            if interpretation.uncertain:
                window.scoreable = False
                state.uncertainty.append(
                    dict(
                        event_id=event.event_id,
                        visible_to=event.visible_to,
                        reason="interpretation_uncertain",
                    )
                )
        if event.kind == "user_turn":
            for update in interpretation.updates:
                _update(state, update, event)
        elif agent_action:
            opportunities = {}
            for use in interpretation.uses:
                # One behavior/item opportunity even when phrased repeatedly.
                opportunity = _opportunity(use, event, visible)
                previous = opportunities.get(use.item_id)
                priority = {
                    "inconsistent": 4,
                    "unsupported": 3,
                    "uncertain": 2,
                    "consistent": 1,
                    "not_applicable": 0,
                }
                if (
                    previous is None
                    or priority[opportunity["status"]] > priority[previous["status"]]
                ):
                    opportunities[use.item_id] = opportunity
            window.opportunities.extend(opportunities.values())
            allowed = {
                k for k, v in opportunities.items() if v["status"] == "consistent"
            }
            for identity in allowed:
                fact = state.facts.get(identity)
                if fact and fact.status == "mentioned":
                    _status(fact, "grounded", event)
                    fact.source_refs.append(event.event_id)
            for identity in interpretation.advances:
                if identity in allowed:
                    _advance(state, identity, event, window)
            if interpretation.greeting:
                _advance(
                    state,
                    "interaction:establish_contact",
                    event,
                    window,
                    satisfied=True,
                )
            for fulfill in interpretation.fulfills:
                item = state.agenda.get(fulfill.item_id)
                if (
                    item is None
                    or item.completion is None
                    or fulfill.item_id not in allowed
                ):
                    continue
                if item.completion.mode not in ("communication", "interaction"):
                    continue
                condition = item.completion
                if condition.expected is None and fulfill.evidence_id:
                    operation = visible["operations"].get(fulfill.evidence_id)
                    expected, found = (
                        pointer(operation["value"].get("result"), fulfill.evidence_path)
                        if operation
                        else (None, False)
                    )
                    if not found:
                        continue
                    condition = condition.model_copy(update={"expected": expected})
                if condition.expected is None and condition.comparison != "semantic":
                    continue
                passed = compare(fulfill.actual, condition, fulfill.met)
                if passed is True:
                    _advance(state, item.id, event, window, satisfied=True)
            for identity in interpretation.regresses:
                if (
                    identity in opportunities
                    and opportunities[identity]["status"] == "inconsistent"
                ):
                    item = state.agenda.get(identity)
                    if item and item.status != "cancelled":
                        _status(item, "open", event)
                        window.delta.regressed_items.append(identity)
            if event.kind == "tool_requested":
                data = event.content
                id = "operation:" + data["call_id"]
                state.operations[id] = StateItem(
                    id=id,
                    kind="action",
                    key=data["name"],
                    value={"arguments": data.get("arguments", {})},
                    status="requested",
                    source_refs=[event.event_id],
                    visible_to=event.visible_to,
                    visible_at=event.sequence,
                    updated_at=event.sequence,
                )
                pending_relations[id] = [
                    k for k in interpretation.related_items if k in allowed
                ]
        elif event.kind in ("tool_completed", "tool_delivered"):
            data = event.content
            id = "operation:" + data["call_id"]
            operation = state.operations.get(id)
            if operation is not None:
                if event.kind == "tool_completed":
                    # Only the evaluator sees completion until a delivery event.
                    operation.value["observed_result"] = data.get("result")
                    operation.value["observed_error"] = data.get("error")
                    operation.updated_at = event.sequence
                else:
                    operation.value.pop("observed_result", None)
                    operation.value.pop("observed_error", None)
                    operation.value["result"] = data.get("result")
                    operation.status = "failed" if data.get("error") else "succeeded"
                    operation.updated_at = event.sequence
                    operation.visible_to = list(
                        set(operation.visible_to + event.visible_to)
                    )
                    operation.source_refs.append(event.event_id)
                    for identity in pending_relations.get(id, []):
                        item = state.agenda.get(identity)
                        if item is None or item.status == "cancelled":
                            continue
                        outcome = adapter.outcome(
                            item, operation, state.visible(event.role)
                        )
                        if outcome is True:
                            _advance(state, identity, event, window, satisfied=True)
                        elif (
                            outcome is None
                            and item.completion
                            and item.completion.mode in ("tool_result", "state_effect")
                        ):
                            window.scoreable = False
                            window.issues.append(
                                dict(
                                    event_id=event.event_id,
                                    reason="outcome_evidence_unavailable",
                                    item_id=item.id,
                                )
                            )
                        elif adapter.is_external_block(data):
                            item.blocked_by = id
                            _status(item, "blocked", event)
                        elif item.blocked_by is not None and not data.get("error"):
                            item.blocked_by = None
                            _status(item, "in_progress", event)
        elif event.kind == "backend_observation":
            # Store actual lifecycle observations; they are not Agent progress.
            data = (
                event.content
                if isinstance(event.content, dict)
                else {"result": event.content}
            )
            identity = "backend:" + str(
                data.get("backend_task_id") or data.get("task_id") or event.event_id
            )
            prior = state.operations.get(identity)
            for item in state.agenda.values():
                if item.blocked_by == identity and data.get("status") not in (
                    "waiting",
                    "failed",
                ):
                    item.blocked_by = None
                    _status(item, "in_progress", event)
            state.operations[identity] = StateItem(
                id=identity,
                kind="action",
                key="backend_task",
                value=data,
                status=data.get("status") or data.get("state") or "unknown",
                source_refs=(prior.source_refs if prior else []) + [event.event_id],
                visible_to=event.visible_to,
                visible_at=event.sequence,
                updated_at=event.sequence,
            )
        if event.kind in ("user_turn", "agent_turn"):
            state.dialogue.append(
                dict(
                    id=event.event_id,
                    role=event.role,
                    content=event.content,
                    sequence=event.sequence,
                    visible_to=event.visible_to,
                )
            )
        state.version += 1
        versions.append(state.model_dump(mode="json"))
        window.state_after_window = state.version
        if event.kind == "agent_turn":
            window.closed_by_reply = True
            _finish_window(state, window)
            closed_transition = window
            windows.append(window)
            window = StateTransition(window_id=f"window:{len(windows) + 1}")
        self.state = state
        self.window = window
        self.pending_relations = pending_relations
        self.windows = windows
        self.versions = versions
        return StateStepResult(
            state_before=before,
            interpretation=interpretation,
            state_after=state.model_copy(deep=True),
            transition=closed_transition,
        )

    def finish(self) -> tuple[list[StateTransition], list[dict], DialogueState]:
        """Close a trailing non-agent window and return the complete trace."""
        if self.window.events:
            _finish_window(self.state, self.window)
            self.window.scoreable &= bool(self.window.state_before_tool_action)
            self.windows.append(self.window)
            self.window = StateTransition(window_id=f"window:{len(self.windows) + 1}")
        return self.windows, self.versions, self.state


def project_state(task, evidence, interpreter=None, adapter=None):
    """Replay all events through the incremental projector."""
    projector = StateProjector(
        task, interpreter=interpreter, adapter=adapter, evidence=evidence
    )
    for event in build_events(task, evidence):
        projector.process_event(event)
    return projector.finish()


def _finish_window(state, window):
    window.delta.external_blocks = [
        i.id for i in state.agenda.values() if i.status == "blocked"
    ]
    if window.delta.regressed_items:
        window.progressivity = "regressed"
    elif window.delta.advanced_items:
        window.progressivity = "forward"
    elif window.delta.external_blocks:
        window.progressivity = "externally_blocked"
    else:
        window.progressivity = "stagnated"
