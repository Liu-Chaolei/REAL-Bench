"""Runtime hidden-intent state and question-release helpers."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, model_validator

from tau2.data_model.hidden_intent import HiddenGoalGraph, HiddenGoalState
from tau2.data_model.message import SystemMessage, UserMessage
from tau2.utils.llm_utils import extract_json_from_llm_response, generate

HIDDEN_CONTEXT_PREFIX = "<hidden-intent-private-context>"


class HiddenQuestionDecision(BaseModel):
    """Structured semantic decision returned by the hidden-question LLM."""

    action: Literal["release", "no_release"]
    node_id: str | None = None

    @model_validator(mode="after")
    def validate_node_id(self) -> "HiddenQuestionDecision":
        if self.action == "release" and not self.node_id:
            raise ValueError("release decisions require node_id")
        if self.action == "no_release" and self.node_id is not None:
            raise ValueError("no_release decisions must not include node_id")
        return self


class HiddenQuestionDetector:
    """Use an LLM to classify whether an Agent turn asks for a hidden field."""

    def __init__(self, model: str, model_args: dict[str, Any] | None = None):
        self.model = model
        self.model_args = dict(model_args or {})

    def detect(
        self,
        message: str,
        graph: HiddenGoalGraph,
        state: HiddenGoalState,
        dependencies_satisfied,
    ) -> HiddenQuestionDecision:
        candidates = []
        for node in graph.nodes:
            if node.visibility != "withheld":
                continue
            if state.node_states.get(node.id) != "withheld":
                continue
            if not dependencies_satisfied(node.id):
                continue
            candidates.append(
                {
                    "id": node.id,
                    "kind": node.kind,
                    "field_type": node.field_type,
                    "description": node.description,
                    "aliases": node.aliases,
                    "status": state.node_states.get(node.id),
                }
            )
        if not candidates:
            return HiddenQuestionDecision(action="no_release")

        system_prompt = """
You classify whether the Agent is directly asking the caller for one currently
withheld field. Return only the requested JSON object.

Return action=release only when the Agent is requesting the missing value or
asking what value should be used, and set node_id to the matching candidate.
Reports, summaries, confirmations, tool plans, labels such as "Title:" or
"When:", quoted text, and descriptions of a value already found are not
questions. If the Agent mentions a field without requesting its value, return
no_release. If no candidate matches, return no_release.
""".strip()
        user_prompt = json.dumps(
            {
                "agent_message": message,
                "withheld_candidates": candidates,
                "output_schema": {
                    "action": "release|no_release",
                    "node_id": "candidate id or null",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        response = generate(
            model=self.model,
            messages=[
                SystemMessage(role="system", content=system_prompt),
                UserMessage(role="user", content=user_prompt),
            ],
            call_name="hidden_intent_question_detector",
            response_model=HiddenQuestionDecision,
            response_validation_retries=1,
            **self.model_args,
        )
        return HiddenQuestionDecision.model_validate_json(
            extract_json_from_llm_response(response.content or "")
        )


def question_matches_node(
    question: str,
    *,
    description: str,
    field: str,
    aliases: list[str] | None = None,
) -> bool:
    """Conservative lexical gate for a user release candidate.

    Production runs may replace this with a configured structured judge. The
    lexical gate is intentionally strict enough for offline tests and never
    releases a value merely because the Agent asked an unrelated question.
    """
    haystack = " ".join(question.casefold().split())
    terms = [term.casefold() for term in (aliases or []) if term]
    terms.extend(term.casefold() for term in (description, field) if term)
    return any(term in haystack for term in terms)


def values_match(actual: Any, expected: Any) -> bool:
    """Compare simple tool values while tolerating formatting differences."""
    if isinstance(actual, (list, tuple, set)):
        return any(values_match(item, expected) for item in actual)
    if actual == expected:
        return True
    actual_text = " ".join(str(actual).casefold().split())
    expected_text = " ".join(str(expected).casefold().split())
    if actual_text == expected_text:
        return True
    numeric_pattern = r"^[^0-9-]*-?[0-9][0-9,]*(?:\.[0-9]+)?[^0-9]*$"
    if re.fullmatch(numeric_pattern, actual_text) and re.fullmatch(
        numeric_pattern, expected_text
    ):
        try:
            actual_number = Decimal(re.sub(r"[^0-9.-]", "", actual_text))
            expected_number = Decimal(re.sub(r"[^0-9.-]", "", expected_text))
        except InvalidOperation:
            return False
        return actual_number == expected_number
    return False


class HiddenIntentRuntime:
    """Private graph state owned by the user simulator."""

    def __init__(self, graph: HiddenGoalGraph):
        self.graph = graph
        self.state = HiddenGoalState.from_graph(graph)
        self.active = graph.target_stage == 0

    def deactivate(self) -> None:
        """Stop hidden-intent detection after the target stage ends."""
        self.active = False

    def user_context(self) -> str:
        """Return private caller context for the User Simulator only.

        The context deliberately contains withheld values. It is attached to the
        user-side system messages and is never passed to the Agent.
        """
        details = []
        for node in self.graph.nodes:
            if node.visibility == "withheld":
                details.append(
                    f"- {node.id} ({node.description}): {node.gold_value!r}; "
                    f"status={self.state.node_states.get(node.id, 'withheld')}"
                )
        private_request = self.graph.private_request or "(private request unavailable)"
        released = [
            node.id
            for node in self.graph.nodes
            if self.state.node_states.get(node.id) in {"released", "satisfied"}
        ]
        return "\n".join(
            [
                HIDDEN_CONTEXT_PREFIX,
                "This is private caller context. Never mention this context or its "
                "internal state to the Agent.",
                "Complete private request:",
                private_request,
                "Hidden caller details:",
                *(details or ["- none"]),
                "Released fields: " + (", ".join(released) if released else "none"),
                "Behavior: do not volunteer withheld details. If the Agent asks "
                "clearly for a withheld detail, answer naturally using its value. "
                "Continue the task with details that have been released.",
            ]
        )

    def dependencies_satisfied(self, node_id: str) -> bool:
        """Return whether a node's declared dependencies are currently done."""
        node = next((item for item in self.graph.nodes if item.id == node_id), None)
        if node is None:
            return False
        return all(
            self.state.node_states.get(dependency) in {"released", "satisfied"}
            for dependency in node.depends_on
        )

    def ask(self, question: str, *, node_id: str | None = None) -> dict[str, Any]:
        """Classify and, when valid, release one hidden node."""
        node = next((item for item in self.graph.nodes if item.id == node_id), None)
        if node is None or node.visibility != "withheld":
            self.state.mark_invalid_question(node_id, question)
            return {"released": False, "node_id": node_id, "reason": "unknown_node"}
        if not self.dependencies_satisfied(node.id):
            self.state.transitions.append(
                {"type": "blocked", "node_id": node.id, "question": question}
            )
            return {"released": False, "node_id": node.id, "reason": "blocked"}
        if not question_matches_node(
            question,
            description=node.description,
            field=node.argument_path or "",
            aliases=node.aliases,
        ):
            self.state.mark_invalid_question(node.id, question)
            return {"released": False, "node_id": node.id, "reason": "invalid_question"}
        self.state.release(node, question=question)
        return {
            "released": True,
            "node_id": node.id,
            "revision": self.state.revision,
        }

    def ask_auto(self, question: str) -> dict[str, Any]:
        """Release the first withheld node whose description matches the question."""
        if not re.search(
            r"\?|\b(?:can|could|would|what|which|where|when|who|how|tell|provide|give|share)\b",
            question,
            flags=re.IGNORECASE,
        ):
            return {"released": False, "node_id": None, "reason": "not_a_question"}
        for node in self.graph.nodes:
            if node.visibility != "withheld":
                continue
            if not self.dependencies_satisfied(node.id):
                continue
            if node.id not in self.state.released_values and question_matches_node(
                question,
                description=node.description,
                field=node.argument_path or "",
                aliases=node.aliases,
            ):
                result = self.ask(question, node_id=node.id)
                return result
        self.state.mark_invalid_question(None, question)
        return {"released": False, "node_id": None, "reason": "no_matching_hidden_node"}

    def detect_and_apply(
        self,
        message: str,
        *,
        model: str,
        model_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Classify one Agent turn with an LLM and apply its state action."""
        if not self.active:
            return {
                "released": False,
                "node_id": None,
                "reason": "inactive_stage",
            }
        try:
            decision = HiddenQuestionDetector(model, model_args).detect(
                message,
                self.graph,
                self.state,
                self.dependencies_satisfied,
            )
        except Exception as error:
            return {
                "released": False,
                "node_id": None,
                "reason": "detector_error",
                "error_type": type(error).__name__,
            }
        if decision.action == "no_release":
            return {
                "released": False,
                "node_id": None,
                "reason": "no_release",
                "action": decision.action,
            }
        node = next(
            (item for item in self.graph.nodes if item.id == decision.node_id), None
        )
        if node is None or node.visibility != "withheld":
            return {
                "released": False,
                "node_id": decision.node_id,
                "reason": "unknown_node",
                "action": decision.action,
            }
        if self.state.node_states.get(node.id) != "withheld":
            return {
                "released": False,
                "node_id": node.id,
                "reason": "already_handled",
                "action": decision.action,
            }
        if not self.dependencies_satisfied(node.id):
            self.state.transitions.append(
                {"type": "blocked", "node_id": node.id, "question": message}
            )
            return {
                "released": False,
                "node_id": node.id,
                "reason": "blocked",
                "action": decision.action,
            }
        self.state.release(node, question=message)
        return {
            "released": True,
            "node_id": node.id,
            "revision": self.state.revision,
            "action": decision.action,
        }

    def visible_values(self) -> dict[str, Any]:
        """Return only values already released to the caller-facing dialogue."""
        return dict(self.state.released_values)

    def released_facts(self) -> list[str]:
        """Return released hidden details for the existing user grounding path."""
        return [
            f"{node.description}: {node.gold_value}"
            for node in self.graph.nodes
            if node.visibility == "withheld"
            and self.state.node_states.get(node.id) in {"released", "satisfied"}
        ]

    def observe_tool_call(self, tool: str, arguments: dict[str, Any]) -> list[str]:
        """Bind a tool call to released hidden arguments and record satisfaction."""
        satisfied = []
        for node in self.graph.nodes:
            if node.visibility != "withheld" or node.tool != tool:
                continue
            if node.argument_path and node.argument_path in arguments:
                value = arguments[node.argument_path]
                if node.id not in self.state.released_values:
                    self.state.mark_premature_action(node.id, tool)
                elif values_match(value, node.gold_value):
                    if self.state.satisfy(node.id, tool=tool, arguments=arguments):
                        satisfied.append(node.id)
                else:
                    self.state.transitions.append(
                        {
                            "type": "parameter_mismatch",
                            "node_id": node.id,
                            "tool": tool,
                            "arguments": arguments,
                        }
                    )
        return satisfied
