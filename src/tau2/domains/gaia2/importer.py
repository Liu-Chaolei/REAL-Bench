"""Deterministic conversion of Gaia2 Parquet rows into native tau2 tasks."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from tau2.data_model.scenario import ScenarioAction, ScenarioSpec
from tau2.data_model.tasks import (
    Description,
    EnvAssertion,
    EvaluationCriteria,
    RewardType,
    StructuredUserInstructions,
    Task,
    UserScenario,
)
from tau2.domains.gaia2.utils import GAIA2_DATA_DIR, GAIA2_VERSION, canonical_json

CATEGORIES = ("search", "execution", "ambiguity", "adaptability", "time")
USER_CONSTRAINTS = (
    "Your first utterance is supplied verbatim by the scenario. Afterwards, "
    "respond naturally to what you actually hear. Repeat or clarify the original "
    "request, answer using only known facts, and approve actions only when they "
    "implement that request. Do not invent preferences, change dates or add tasks. "
    "If asked for unknown information, say you do not know. Wait when the agent "
    "is doing requested work or monitoring future updates. Do not treat a progress "
    "announcement as proof of completion. You cannot see future messages, "
    "application databases, reference answers or evaluation criteria."
)


def decode_arguments(action: dict) -> dict:
    """Decode the source's explicitly typed JSON argument values safely."""
    result = {}
    for argument in action.get("args", []):
        name, value = argument["name"], argument.get("value")
        kind = argument.get("value_type")
        if value == "" and kind and kind.startswith("Optional["):
            value = None
        if isinstance(value, str) and kind not in ("str", "Optional[str]"):
            if "{{" not in value:
                if kind in ("int", "Optional[int]"):
                    value = int(value) if value not in ("None", "null") else None
                elif kind == "float":
                    value = float(value)
                elif kind == "bool":
                    if value.lower() not in ("true", "false"):
                        raise ValueError(f"Invalid boolean argument {name}")
                    value = value.lower() == "true"
                elif kind and (kind.startswith("list") or kind.startswith("dict")):
                    value = json.loads(value)
                elif kind in (None, "None") and value in ("None", "null"):
                    value = None
        result[name] = value
    return result


def convert_record(record: dict, category: str, snapshot: str) -> Task:
    """Compile data, causal references and scoring requirements for one task."""
    raw = record["data"]
    data = json.loads(raw) if isinstance(raw, str) else raw
    events = []
    answers = []
    for source in data.get("events", []):
        kind = {"USER": "user", "AGENT": "reference", "ENV": "environment"}.get(
            source.get("event_type")
        )
        if kind is None:
            raise ValueError(
                f"Unsupported source event type {source.get('event_type')}"
            )
        action = source["action"]
        arguments = decode_arguments(action)
        parents = list(source.get("dependencies", []))
        refs = re.findall(r"\{\{([^{}]+)\}\}", json.dumps(arguments))
        parents.extend(ref for ref in refs if ref not in parents)
        comparator = source.get("event_time_comparator")
        event = ScenarioAction(
            id=source["event_id"],
            kind=kind,
            tool=f"{action['app']}__{action['function']}",
            arguments=arguments,
            dependencies=parents,
            delay_seconds=source.get("event_relative_time") or 0,
            time_comparator=str(comparator) if comparator is not None else None,
            channel="agent"
            if kind == "reference" and action["app"] == "AgentUserInterface"
            else "tool",
            match_fields=[
                key
                for key in arguments
                if key not in {"content", "subject", "description", "title"}
            ]
            if kind == "reference" and action["app"] != "AgentUserInterface"
            else None,
            semantic_fields=[
                key
                for key in arguments
                if key in {"content", "subject", "description", "title"}
            ]
            if kind == "reference" and action["app"] != "AgentUserInterface"
            else [],
        )
        events.append(event)
        if (
            kind == "reference"
            and event.tool == "AgentUserInterface__send_message_to_user"
        ):
            answers.append(str(arguments["content"]))
    identifiers = {event.id for event in events}
    if len(identifiers) != len(events):
        raise ValueError("Duplicate event identifier")
    pending, resolved = list(events), set()
    while pending:
        available = [e for e in pending if set(e.dependencies) <= resolved]
        if not available:
            raise ValueError("Cyclic or missing scenario dependencies")
        resolved.update(e.id for e in available)
        pending = [e for e in pending if e not in available]
    initial = [e for e in events if e.kind == "user" and not e.dependencies]
    if len(initial) != 1:
        raise ValueError("Expected one initial user request")
    request = initial[0].arguments["content"]
    own_information = {}
    for app in data["apps"]:
        if app["name"] == "Contacts":
            for person in app["app_state"].get("contacts", {}).values():
                if person.get("is_user"):
                    own_information = {
                        key: person[key]
                        for key in (
                            "first_name",
                            "last_name",
                            "gender",
                            "age",
                            "phone",
                            "email",
                            "address",
                            "city_living",
                            "country",
                            "nationality",
                            "status",
                            "job",
                        )
                        if person.get(key) not in (None, "")
                    }
    spec = ScenarioSpec(
        start_time=data["metadata"]["definition"]["start_time"],
        source_id=record["id"],
        source_sha256=hashlib.sha256(canonical_json(data)).hexdigest(),
        snapshot=snapshot,
        category=category,
        events=events,
    )
    basis = [RewardType.ENV_ASSERTION]
    basis.append(RewardType.NL_ASSERTION)
    if answers:
        basis.append(RewardType.COMMUNICATE)
    return Task(
        id=record["id"],
        scenario=spec,
        description=Description(
            purpose=f"Gaia2 {category}; adapted tau2 scoring {GAIA2_VERSION}",
            notes=f"Original scenario: {record['scenario_id']}",
        ),
        user_scenario=UserScenario(
            instructions=StructuredUserInstructions(
                domain="gaia2",
                reason_for_call=request,
                known_info=json.dumps(own_information, ensure_ascii=False)
                if own_information
                else None,
                task_instructions=USER_CONSTRAINTS,
            )
        ),
        user_tools=[],
        evaluation_criteria=EvaluationCriteria(
            actions=[],
            env_assertions=[
                EnvAssertion(
                    env_type="assistant",
                    func_name="assert_scenario_goal",
                    arguments={"source_id": record["id"]},
                )
            ],
            communicate_info=answers or None,
            nl_assertions=[
                "Check the caller's explicit timing, conditional, prohibition, clarification, budget, ordering, explicit label and selection requirements against the actual conversation and execution evidence. In particular, do not credit an early completion claim, a missed explicit deadline, or a prohibited notification. For cabs, judge explicit fare or delay constraints against actual returned offers and bookings; extra quote reads alone are not a failure. Do not impose an unstated deadline or require the reference number of tool calls. Requirements that are absent are satisfied. Original request: "
                + request
            ],
            reward_basis=basis,
        ),
    )


def import_dataset(
    source: Path,
    *,
    output: Path = GAIA2_DATA_DIR,
    assets: Path | None = None,
    replace_incomplete=False,
) -> dict:
    """Import all five source configurations without running models or ARE."""
    try:
        import fastparquet
    except ImportError as error:
        raise RuntimeError("Install the gaia2 extra to read Parquet inputs") from error
    output = Path(output)
    if (output / "manifest.json").exists():
        previous = json.loads((output / "manifest.json").read_text())
        if previous.get("verified") or not replace_incomplete:
            raise FileExistsError(
                "Preserve the existing import; choose a new output directory"
            )
    (output / "snapshots").mkdir(parents=True, exist_ok=True)
    tasks, splits, hashes, failures = [], {}, {}, []
    for category in CATEGORIES:
        splits[category] = []
        paths = sorted((Path(source) / category).glob("*.parquet"))
        if not paths:
            raise FileNotFoundError(f"Missing Gaia2 configuration {category}")
        for path in paths:
            hashes[str(path.relative_to(source))] = hashlib.file_digest(
                path.open("rb"), "sha256"
            ).hexdigest()
            for group in fastparquet.ParquetFile(path).iter_row_groups():
                for row in group.to_dict(orient="records"):
                    try:
                        data = (
                            json.loads(row["data"])
                            if isinstance(row["data"], str)
                            else row["data"]
                        )
                        raw = canonical_json(data["apps"])
                        digest = hashlib.sha256(raw).hexdigest()
                        target = output / "snapshots" / f"{digest}.json.gz"
                        if not target.exists():
                            target.write_bytes(gzip.compress(raw, mtime=0))
                        task = convert_record(row, category, digest)
                        tasks.append(task)
                        splits[category].append(task.id)
                    except Exception as error:
                        failures.append(
                            {"id": row["id"], "category": category, "error": str(error)}
                        )
    identifiers = [task.id for task in tasks]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Duplicate task IDs across source configurations")
    splits["base"] = sorted(identifiers)
    tasks.sort(key=lambda task: task.id)
    (output / "tasks.json").write_bytes(
        canonical_json([t.model_dump(mode="json") for t in tasks])
    )
    (output / "split_tasks.json").write_bytes(canonical_json(splits))
    manifest = {
        "version": GAIA2_VERSION,
        "source_files": hashes,
        "tasks": len(tasks),
        "categories": dict(Counter(t.scenario.category for t in tasks)),
        "snapshots": len(list((output / "snapshots").glob("*.json.gz"))),
        "assets": str(Path(assets).resolve()) if assets else str(output / "assets"),
        "tasks_sha256": hashlib.sha256(
            (output / "tasks.json").read_bytes()
        ).hexdigest(),
        "failures": failures,
        "converted_complete": len(tasks) == 800 and not failures,
        "complete": False,
        "verified": False,
        "score_basis": "tau_environment_assertions_and_semantic_communication",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if failures:
        raise ValueError(f"{len(failures)} tasks failed conversion; see manifest.json")
    return manifest
