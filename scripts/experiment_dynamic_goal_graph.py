"""Small offline experiment for graph-based dynamic follow-up rendering.

This script deliberately lives outside the runtime dynamic-followup path.  It
reuses prompts captured from two real Gaia2 runs, compiles the current source
request and its tool GT into a goal graph, and asks the caller model to render
the graph using only the preceding history.  The runtime implementation is
not imported or modified by this experiment.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "data/tau2/domains/gaia2/tasks.json"
OUTPUT_PATH = ROOT / "data/experiments/dynamic_goal_graph_20261117.json"


CASES = [
    {
        "task_id": "0002_46myz87t501b0c902dy81rlbqbye2dyi",
        "stage": 1,
        "request_log": ROOT
        / "data/simulations/dynamic-metrics-task0002-20261117/artifacts/task_0002_46myz87t501b0c902dy81rlbqbye2dyi/sim_fe5588dc-2edf-4d2a-8d61-c3f655b435dd/llm_debug/20261117_214546_852_dynamic_followup_request_7416fd78.json",
    },
    {
        "task_id": "0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o",
        "stage": 1,
        "request_log": ROOT
        / "data/simulations/dynamic-metrics-task0138-20261116/artifacts/task_0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o/sim_ad25b004-15e4-418b-b93b-45e0153c3d25/llm_debug/20261116_232146_415_dynamic_followup_request_68f4e635.json",
    },
    {
        "task_id": "0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o",
        "stage": 2,
        "request_log": ROOT
        / "data/simulations/dynamic-metrics-task0138-focus-20261117-retry/artifacts/task_0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o/sim_cf793b64-22b7-43ef-9406-456f22413ece/llm_debug/20261117_203305_295_dynamic_followup_request_0ee60cf0.json",
    },
]


class GoalNode(BaseModel):
    """One executable, communicative, or constraining goal."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["tool_action", "communication", "constraint", "report"]
    description: str
    visibility: Literal["caller_visible", "backend_only"] = "caller_visible"
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    protected_values: list[str] = Field(default_factory=list)
    backend_only_values: list[str] = Field(default_factory=list)


class GoalGraph(BaseModel):
    """Structured contract used by the renderer instead of source prose."""

    model_config = ConfigDict(extra="forbid")

    stage: int
    nodes: list[GoalNode]
    edges: list[list[str]] = Field(default_factory=list)
    protected_values: list[str] = Field(default_factory=list)
    backend_only_values: list[str] = Field(default_factory=list)
    extraction_notes: list[str] = Field(default_factory=list)


class RawGoalGraph(BaseModel):
    """Tolerant wire format for model-produced graph JSON."""

    model_config = ConfigDict(extra="allow")

    stage: int | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    goals: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[Any] = Field(default_factory=list)
    ordering_constraints: list[Any] = Field(default_factory=list)
    protected_values: Any = Field(default_factory=list)
    backend_only_values: Any = Field(default_factory=list)
    extraction_notes: list[str] = Field(default_factory=list)
    goal_graph: dict[str, Any] | None = None


class RenderedRequest(BaseModel):
    """The natural-language rendering produced by the graph experiment."""

    model_config = ConfigDict(extra="forbid")

    utterance: str = Field(min_length=1)
    covered_node_ids: list[str] = Field(default_factory=list)
    referenced_entities: list[str] = Field(default_factory=list)


class GraphCheck(BaseModel):
    """Semantic safety result for the graph rendering."""

    model_config = ConfigDict(extra="allow")

    valid: bool
    reason: str = ""
    missing_nodes: list[str] = Field(default_factory=list)
    invented_requirements: list[str] = Field(default_factory=list)
    broken_dependencies: list[str] = Field(default_factory=list)
    wrong_values: list[str] = Field(default_factory=list)
    leaked_backend_values: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


GRAPH_SYSTEM = """
You are a benchmark goal-graph compiler. Convert the CURRENT SOURCE REQUEST
into a small directed goal graph for a later natural-language rendering.

The source request is authoritative for caller-visible goals. EXPECTED TOOL GT
is execution evidence: use it to identify backend actions and dependencies, but
never turn a value that appears only in GT into a caller-visible requirement.
Values that occur in GT but not in SOURCE REQUEST must be marked
backend_only_values. Internal IDs, database values, and tool results are always
backend-only unless the source explicitly asks the caller to say them.

Represent every substantive action, report-back requirement, exact wording,
prohibition, ordering constraint, and same-object dependency. Preserve exact
source literals in protected_values. Do not copy the source into a prose field;
the graph should contain atomic goals and arguments. Return JSON only.
""".strip()


RENDER_SYSTEM = """
You render the CURRENT STAGE REQUEST from a structured goal graph. The graph is
the only task specification; the preceding history is context for natural
transitions and references. Produce one natural caller utterance.

Cover every required node and preserve all graph edges and dependencies. Keep
protected_values exactly unchanged. Express every caller_visible node and its
caller-visible target, recipient, date, amount, title, or exact message.
Never mention backend_only nodes, backend_only_values, internal IDs, hidden
database fields, verification folders, readback limits, or tool results. Do
not add a preference, entity, deadline, operation, or output that is absent
from the graph.

Use a fresh conversational structure instead of copying the source wording.
You may change the opening, politeness, transitions, clause grouping, and
references to the current conversation. If earlier work failed or was
incomplete, move directly to the current graph and do not reopen or repair the
earlier work. Return JSON only.
""".strip()


CHECK_SYSTEM = """
You are an independent checker for a graph-based dynamic caller request.
Compare SOURCE REQUEST, GOAL GRAPH, and GENERATED REQUEST. Set valid=true only
if every caller_visible graph node is expressed, dependencies are preserved,
protected values are correct, no backend_only node or value is exposed, and no
new requirement is introduced. Natural paraphrase and changed punctuation are
allowed. A generic reference such as 'that chat' is allowed when the graph
establishes the referent. Return JSON only.
""".strip()


def _content(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    return "\n".join(content) if isinstance(content, list) else str(content)


def _section(text: str, start: str, end: str | None = None) -> str:
    if start not in text:
        return ""
    value = text.split(start, 1)[1]
    if end and end in value:
        value = value.split(end, 1)[0]
    return value.strip()


def load_request_case(case: dict[str, Any]) -> dict[str, Any]:
    raw = json.loads(case["request_log"].read_text())
    request = raw["request"]
    user_prompt = _content(request["messages"][1])
    source = _section(user_prompt, "SOURCE REQUEST (immutable goal):", "RELEASED FACTS")
    released = _section(
        user_prompt,
        "RELEASED FACTS FROM EARLIER STAGES:",
        "ACTUAL CONVERSATION AND TOOL OBSERVATIONS:",
    )
    full_history = _section(user_prompt, "ACTUAL CONVERSATION AND TOOL OBSERVATIONS:")
    # The captured prompt appends the current request after the previous STOP.
    # Remove it so the graph experiment cannot succeed by copying source text
    # from the history supplied to the renderer.
    history = full_history.split("###STOP###", 1)[0].strip()
    match = re.search(r"STAGE:\s*(\d+)", user_prompt)
    return {
        "task_id": case["task_id"],
        "stage": int(match.group(1)) if match else case["stage"],
        "source": source,
        "released": released,
        "history": history,
        "original_utterance": json.loads(raw["response"]["content"])["utterance"],
        "model": request["model"],
    }


def load_task(task_id: str) -> dict[str, Any]:
    tasks = json.loads(TASKS_PATH.read_text())
    return next(task for task in tasks if task["id"] == task_id)


def tool_gt_for_stage(task: dict[str, Any], stage: int) -> list[dict[str, Any]]:
    stage_data = next(item for item in task["scenario"]["stages"] if item["stage"] == stage)
    wanted = set(stage_data.get("event_ids", []))
    events = []
    for event in task["scenario"]["events"]:
        if event["id"] not in wanted:
            continue
        if event["tool"].startswith("AgentUserInterface__"):
            continue
        events.append(
            {
                "event_id": event["id"],
                "tool": event["tool"],
                "arguments": event.get("arguments", {}),
                "dependencies": event.get("dependencies", []),
                "kind": event.get("kind"),
            }
        )
    return events


def make_messages(system: str, user: str):
    from tau2.data_model.message import SystemMessage, UserMessage

    return [
        SystemMessage(role="system", content=system),
        UserMessage(role="user", content=user),
    ]


def compact_metrics(source: str, original: str, generated: str) -> dict[str, float]:
    def tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", text.casefold()))

    source_tokens = tokens(source)
    original_tokens = tokens(original)
    generated_tokens = tokens(generated)
    return {
        "source_generated_jaccard": len(source_tokens & generated_tokens)
        / max(1, len(source_tokens | generated_tokens)),
        "source_original_jaccard": len(source_tokens & original_tokens)
        / max(1, len(source_tokens | original_tokens)),
        "generated_chars": float(len(generated)),
        "original_chars": float(len(original)),
    }


def _value_list(values: Any) -> list[str]:
    """Normalize strings and the model's {value: ...} records."""
    if values is None:
        return []
    if isinstance(values, dict):
        # Exact protected values are represented as {value: ...}; arbitrary
        # backend-only records are retained only by key so their hidden values
        # never enter the renderer's prompt.
        if "value" in values or "text" in values:
            values = [values]
        else:
            values = [f"<backend-only:{key}>" for key in values]
    if not isinstance(values, list):
        values = [values]
    normalized = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("value", value.get("text", ""))
        if value and str(value) not in normalized:
            normalized.append(str(value))
    return normalized


def _source_literals(source: str) -> list[str]:
    """Return exact quoted values that must survive graph rendering."""
    values = []
    for match in re.finditer(r'["“]([^"”]+)["”]', source):
        value = " ".join(match.group(1).split())
        if len(value) >= 3 and value not in values:
            values.append(value)
    for match in re.finditer(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", source):
        if match.group(0) not in values:
            values.append(match.group(0))
    return values


def normalize_graph(raw: RawGoalGraph, stage: int, source: str) -> GoalGraph:
    """Accept equivalent `goals`/`nodes` model vocabularies."""
    if raw.goal_graph:
        nested = RawGoalGraph.model_validate(raw.goal_graph)
        # The nested object is the graph; top-level metadata is only a
        # fallback for models that put protected values beside it.
        if not nested.protected_values:
            nested.protected_values = raw.protected_values
        if not nested.backend_only_values:
            nested.backend_only_values = raw.backend_only_values
        raw = nested
    source_nodes = raw.nodes or raw.goals
    nodes = []
    for index, item in enumerate(source_nodes, start=1):
        kind = item.get("kind") or item.get("type") or "tool_action"
        if kind in {"action", "execution", "tool"}:
            kind = "tool_action"
        if kind in {"communication_goal", "communication"}:
            kind = "communication"
        if kind in {"report_goal", "report"}:
            kind = "report"
        if kind not in {"tool_action", "communication", "constraint", "report"}:
            kind = "constraint"
        node_id = str(item.get("id") or item.get("node_id") or f"g{index}")
        dependencies = item.get("depends_on") or item.get("dependencies") or []
        dependencies = [
            str(value.get("id") if isinstance(value, dict) else value)
            for value in dependencies
        ]
        visibility = item.get("visibility")
        if visibility is None:
            visibility = "caller_visible" if item.get("caller_visible", True) else "backend_only"
        if visibility not in {"caller_visible", "backend_only"}:
            visibility = "caller_visible"
        protected = _value_list(
            item.get("protected_values")
            or item.get("literal_values")
            or item.get("exact_values")
            or []
        )
        backend_only = _value_list(item.get("backend_only_values") or [])
        nodes.append(
            GoalNode(
                id=node_id,
                kind=kind,
                description=str(
                    item.get("description")
                    or item.get("action")
                    or item.get("goal")
                    or item.get("text")
                    or node_id
                ),
                visibility=visibility,
                tool=item.get("tool") or item.get("tool_name"),
                arguments=item.get("arguments") or item.get("args") or {},
                depends_on=dependencies,
                protected_values=protected,
                backend_only_values=backend_only,
            )
        )
    edges = []
    for edge in raw.edges:
        if isinstance(edge, dict):
            before = edge.get("from") or edge.get("before")
            after = edge.get("to") or edge.get("after")
            if before and after:
                edges.append([str(before), str(after)])
        elif isinstance(edge, list) and len(edge) == 2:
            edges.append([str(edge[0]), str(edge[1])])
    for constraint in raw.ordering_constraints:
        if not isinstance(constraint, dict):
            continue
        before = constraint.get("before") or constraint.get("from")
        after = constraint.get("after") or constraint.get("to")
        if before and after:
            edges.append([str(before), str(after)])
    source_literals = _source_literals(source)
    source_lower = source.casefold()
    report_match = re.search(
        r"\b(?:report back|tell me|let me know|confirm)\b[^.]*[.]?",
        source,
        flags=re.IGNORECASE,
    )
    report_required = report_match is not None
    report_text = report_match.group(0).strip() if report_match else ""
    visible_nodes = []
    for node in nodes:
        # Readback/listing tools in GT are normally evaluator implementation
        # details unless the source explicitly asks for that readback.
        if (
            node.tool
            and any(word in node.tool.casefold() for word in ("get_", "list_", "read_"))
            and not any(word in source_lower for word in ("check", "verify", "sent folder"))
        ):
            node.visibility = "backend_only"
        if node.kind == "report":
            if report_required:
                if not node.description or node.description == node.id:
                    node.description = report_text
                if not node.arguments:
                    node.arguments = {"required_output": report_text}
            elif not node.arguments and (not node.description or node.description == node.id):
                # A report node invented solely from a backend readback is not
                # a caller goal when the source contains no report request.
                continue
        visible_nodes.append(node)
    nodes = visible_nodes
    if report_required and not any(node.kind == "report" for node in nodes):
        nodes.append(
            GoalNode(
                id="source_report",
                kind="report",
                description=report_text,
                arguments={"required_output": report_text},
                depends_on=[node.id for node in nodes if node.visibility == "caller_visible"],
            )
        )
    valid_ids = {node.id for node in nodes}
    edges = [edge for edge in edges if edge[0] in valid_ids and edge[1] in valid_ids]
    for node in nodes:
        node.depends_on = [value for value in node.depends_on if value in valid_ids]
    model_literals = [
        value for value in _value_list(raw.protected_values) if value in source
    ]
    protected_values = []
    for value in [*source_literals, *model_literals]:
        if value not in protected_values:
            protected_values.append(value)
    return GoalGraph(
        stage=raw.stage or stage,
        nodes=nodes,
        edges=edges,
        protected_values=protected_values,
        backend_only_values=_value_list(raw.backend_only_values),
        extraction_notes=raw.extraction_notes,
    )


def main() -> int:
    # Resolve credentials only through the same local config reference used by
    # the smoke runner. They are never serialized into the experiment output.
    from tau2.utils.llm_utils import extract_json_from_llm_response, generate
    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

    config_path = ROOT / "integrations/qwen_audio_agent/config.gaia2.dynamic.metrics.deepseek-user.json"
    config = QwenAgentConfig.model_validate_json(config_path.read_text())
    model, base_args = config.user_llm_settings()
    base_args = dict(base_args)
    base_args["num_retries"] = 0

    output: dict[str, Any] = {
        "experiment": "dynamic_goal_graph",
        "graph_temperature": 0.0,
        "render_temperature": 0.8,
        "cases": [],
    }

    for case in CASES:
        data = load_request_case(case)
        task = load_task(data["task_id"])
        gt = tool_gt_for_stage(task, data["stage"])
        graph_user = f"""
STAGE: {data['stage']}
CURRENT SOURCE REQUEST:
{data['source']}

EXPECTED TOOL GT (execution evidence; internal values must remain backend-only):
{json.dumps(gt, ensure_ascii=False, indent=2)}
""".strip()
        graph_args = {**base_args, "temperature": 0.0, "seed": 626729}
        graph = None
        graph_reply = None
        for graph_attempt in range(2):
            graph_reply = generate(
                model=model,
                messages=make_messages(GRAPH_SYSTEM, graph_user),
                call_name="goal_graph_experiment_extract",
                response_model=RawGoalGraph,
                response_validation_retries=1,
                **graph_args,
            )
            raw_graph = RawGoalGraph.model_validate_json(
                extract_json_from_llm_response(graph_reply.content or "")
            )
            graph = normalize_graph(raw_graph, data["stage"], data["source"])
            if graph.nodes:
                break
            graph_user += (
                "\n\nThe previous extraction was empty or unusable. Return at least "
                "one node for every substantive current-stage requirement. "
                "Use a top-level `nodes` or `goals` array; do not return an empty graph."
            )
        if graph is None or not graph.nodes:
            raise RuntimeError("goal graph extraction returned an empty graph")

        render_user = f"""
STAGE: {data['stage']}
GOAL GRAPH:
{graph.model_dump_json(indent=2)}

RELEASED FACTS FROM EARLIER STAGES:
{data['released'] or '(none)'}

PRECEDING HISTORY (the current request is intentionally omitted):
{data['history'] or '(none)'}
""".strip()
        render_args = {**base_args, "temperature": 0.8, "seed": 626729 + data["stage"]}
        render_reply = generate(
            model=model,
            messages=make_messages(RENDER_SYSTEM, render_user),
            call_name="goal_graph_experiment_render",
            response_model=RenderedRequest,
            response_validation_retries=1,
            **render_args,
        )
        rendered = RenderedRequest.model_validate_json(
            extract_json_from_llm_response(render_reply.content or "")
        )

        check_user = f"""
SOURCE REQUEST:
{data['source']}

GOAL GRAPH:
{graph.model_dump_json(indent=2)}

GENERATED REQUEST:
{rendered.utterance}

EXPECTED TOOL GT:
{json.dumps(gt, ensure_ascii=False, indent=2)}
""".strip()
        check_args = {**base_args, "temperature": 0.0, "seed": 626729}
        check_reply = generate(
            model=model,
            messages=make_messages(CHECK_SYSTEM, check_user),
            call_name="goal_graph_experiment_check",
            response_model=GraphCheck,
            response_validation_retries=1,
            **check_args,
        )
        check = GraphCheck.model_validate_json(
            extract_json_from_llm_response(check_reply.content or "")
        )
        output["cases"].append(
            {
                "task_id": data["task_id"],
                "stage": data["stage"],
                "source": data["source"],
                "original_utterance": data["original_utterance"],
                "goal_graph": graph.model_dump(mode="json"),
                "generated_utterance": rendered.utterance,
                "covered_node_ids": rendered.covered_node_ids,
                "verification": check.model_dump(mode="json"),
                "metrics": compact_metrics(
                    data["source"], data["original_utterance"], rendered.utterance
                ),
            }
        )
        print(
            f"completed task={data['task_id']} stage={data['stage']} "
            f"valid={check.valid}",
            file=sys.stderr,
            flush=True,
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
