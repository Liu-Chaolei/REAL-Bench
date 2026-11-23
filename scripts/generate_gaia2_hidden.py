"""Generate the independent 100-task Gaia2 hidden-intent package.

The generator intentionally uses the frozen pre-variant 800-task archive. It
creates a reviewable first data cut by masking concrete caller-provided values
from one stage request and recording the full request privately for the caller
simulator. It never edits the canonical Gaia2 task files.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import re
from pathlib import Path

from tau2.data_model.hidden_intent import (
    HiddenGoalGraph,
    HiddenGoalNode,
    HiddenIntentPlan,
    HiddenRequirement,
)
from tau2.data_model.tasks import Task

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "data/archives/gaia2-before-variants-v1-20261107"
OUTPUT_ROOT = ROOT / "data/tau2/domains/gaia2_hidden"
SEED = 314159
CATEGORIES = ("search", "execution", "ambiguity", "adaptability", "time")

EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
QUOTED = re.compile(r"[\"“‘]([^\"”’]{6,})[\"”’]")
MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
DATE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:,\s*\d{4})?\b",
    re.I,
)
TIME = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)(?:\s+UTC)?\b", re.I)


def digest(value) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def candidate_values(text: str) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for kind, pattern in (
        ("email", EMAIL),
        ("quoted", QUOTED),
        ("amount", MONEY),
        ("date", DATE),
        ("time", TIME),
    ):
        for match in pattern.finditer(text):
            candidate_kind = kind
            value = match.group(1) if candidate_kind == "quoted" else match.group(0)
            if candidate_kind == "quoted":
                context = text[
                    max(0, match.start() - 100) : match.end() + 100
                ].casefold()
                candidate_kind = (
                    "email_subject" if "subject" in context else "group_title"
                )
            value = " ".join(value.split())
            if len(value) >= 4 and value.casefold() not in {
                v.casefold() for _, v in values
            }:
                values.append((candidate_kind, value))
    return values


def replacement(kind: str, text: str) -> str:
    context = text.casefold()
    if kind == "group_title":
        if "group chat" in context:
            return "a group name I will provide"
        if "title" in context:
            return "a title I will provide"
        return "wording I will provide"
    if kind == "email_subject":
        return "a subject line I will provide"
    if kind == "amount":
        if "cost more" in context or "price" in context:
            return "my chosen price limit"
        return "the amount I will provide"
    if kind == "date":
        return "the date I will confirm"
    if kind == "time":
        return "the time I will confirm"
    return {
        "email": "the email address I will provide",
        "clause": "the exact remaining request details",
    }[kind]


FIELD_TYPES = {
    "email": "email_recipient",
    "group_title": "group_title",
    "email_subject": "email_subject",
    "amount": "amount",
    "date": "date",
    "time": "time",
    "clause": "constraint_clause",
}

ALIASES = {
    "email": ["email address", "recipient email", "where should I send it"],
    "group_title": [
        "title",
        "name",
        "group name",
        "chat name",
        "what should I call",
        "what should I name",
        "what should the group be called",
        "what should the event be called",
        "event title",
        "reminder title",
    ],
    "email_subject": [
        "subject",
        "subject line",
        "email subject",
        "what should the subject say",
    ],
    "amount": [
        "amount",
        "budget",
        "price threshold",
        "maximum price",
        "cost limit",
        "price cap",
    ],
    "date": ["date", "which day", "what day"],
    "time": ["time", "what time", "start time"],
    "clause": [
        "constraint",
        "condition",
        "requirement",
        "what details",
        "which details",
    ],
}


def mask_text(text: str, hidden: list[tuple[str, str]]) -> str:
    masked = text
    for kind, value in hidden:
        masked = re.sub(re.escape(value), replacement(kind, masked), masked, flags=re.I)
    return masked


def stage_requests(task: dict) -> list[str]:
    return [stage["request"] for stage in task["scenario"]["stages"]]


def user_event_ids(task: dict) -> set[str]:
    return {
        event["id"] for event in task["scenario"]["events"] if event["kind"] == "user"
    }


def redact_task(task: dict, hidden: list[tuple[str, str]], target_stage: int) -> dict:
    result = copy.deepcopy(task)
    target_event_ids = set(result["scenario"]["stages"][target_stage]["event_ids"])
    for event in result["scenario"]["events"]:
        if event["id"] in target_event_ids and isinstance(
            event["arguments"].get("content"), str
        ):
            event["arguments"]["content"] = mask_text(
                event["arguments"]["content"], hidden
            )
    result["scenario"]["stages"][target_stage]["request"] = mask_text(
        result["scenario"]["stages"][target_stage]["request"], hidden
    )
    if target_stage == 0:
        result["user_scenario"]["instructions"]["reason_for_call"] = mask_text(
            result["user_scenario"]["instructions"]["reason_for_call"], hidden
        )
    result["user_scenario"]["instructions"]["task_instructions"] = (
        "Continue the visible request using only information already released in "
        "the conversation. Keep private caller details private. Answer a specific "
        "question only when the Agent asks for that detail. Do not volunteer or "
        "guess withheld information."
    )
    result["id"] = result["id"].replace("__hidden_intent_v1", "")
    result["id"] = f"hidden_{result['id']}_v1"
    result["description"]["purpose"] = "Gaia2 dynamic hidden-intent benchmark task"
    result["scenario"]["category"] = "hidden_intent"
    result["scenario"]["source_id"] = task["id"]
    return result


def build_graph(
    task: dict, hidden: list[tuple[str, str]], source_id: str, target_stage: int
) -> HiddenGoalGraph:
    nodes = [
        HiddenGoalNode(
            id="g_source_request",
            kind="communication",
            visibility="caller_visible",
            description="Continue the source task without inventing a new business goal.",
            source_event_ids=[],
        )
    ]

    def locate_value(value: str):
        def walk(item, path=""):
            if isinstance(item, dict):
                for key, child in item.items():
                    found = walk(child, f"{path}.{key}" if path else key)
                    if found is not None:
                        return found
            elif isinstance(item, list):
                for index, child in enumerate(item):
                    found = walk(child, f"{path}[{index}]")
                    if found is not None:
                        return found
            elif str(item).casefold() == str(value).casefold():
                return path
            elif isinstance(item, str) and str(value).casefold() in {
                part.casefold() for part in item.replace("\\", "/").split("/") if part
            }:
                return path
            return None

        for event in task["scenario"]["events"]:
            if event["kind"] != "reference":
                continue
            path = walk(event.get("arguments", {}))
            if path is not None:
                return event["tool"], event["id"], path
        return None, None, None

    def infer_binding(kind: str, request: str):
        """Bind a caller field to the tool that consumes it in the task."""
        text = request.casefold()
        if kind == "amount" and any(
            term in text for term in ("apartment", "property", "properties")
        ):
            argument = (
                "min_price"
                if any(
                    term in text
                    for term in ("cost more than", "more than", "above", "over")
                )
                else "max_price"
            )
            return "RentAFlat__search_apartments", argument
        if kind == "group_title":
            if "group chat" in text or "chats" in text:
                return "Chats__create_group_conversation", "title"
            if any(term in text for term in ("event", "calendar", "reminder")):
                return "Calendar__add_calendar_event", "title"
        if kind == "time":
            if "cab" in text or "ride" in text:
                return "Cabs__order_ride", "ride_time"
            if any(term in text for term in ("event", "calendar", "reminder")):
                return "Calendar__add_calendar_event", "start_datetime"
        if kind == "date":
            if any(term in text for term in ("event", "calendar", "reminder")):
                return "Calendar__add_calendar_event", "start_datetime"
        if kind == "email_subject":
            if "email" in text or "subject" in text:
                return "Emails__send_email", "subject"
        if kind == "email":
            return "Emails__send_email", "recipients"
        return None, None

    for index, (kind, value) in enumerate(hidden, 1):
        tool, event_id, argument_path = locate_value(value)
        if tool is None:
            tool, argument_path = infer_binding(
                kind, task["scenario"]["stages"][target_stage]["request"]
            )
        nodes.append(
            HiddenGoalNode(
                id=f"g_hidden_{index}",
                kind="tool_argument",
                visibility="withheld",
                field_type=FIELD_TYPES[kind],
                description=f"The withheld caller-provided {kind} detail",
                aliases=ALIASES[kind],
                tool=tool,
                argument_path=argument_path,
                gold_value=value,
                depends_on=["g_source_request"],
                release_policy="after_valid_question",
            )
        )
    for event in task["scenario"]["events"]:
        if event["kind"] == "reference":
            nodes.append(
                HiddenGoalNode(
                    id=f"g_backend_{event['id']}",
                    kind="tool_action",
                    visibility="backend_only",
                    description=f"Execute reference operation {event['tool']}",
                    tool=event["tool"],
                    source_event_ids=[event["id"]],
                )
            )
    graph = HiddenGoalGraph(
        source_task_id=source_id,
        source_category=task["scenario"]["category"],
        target_stage=target_stage,
        public_request=mask_text(
            task["scenario"]["stages"][target_stage]["request"], hidden
        ),
        private_request=task["scenario"]["stages"][target_stage]["request"],
        nodes=nodes,
        protected_values=[value for _, value in hidden],
        backend_only_values=[
            event["id"]
            for event in task["scenario"]["events"]
            if event["kind"] == "reference"
        ],
    )
    return graph


def main(
    *,
    count: int = 100,
    output_root: Path = OUTPUT_ROOT,
    source_task_ids: list[str] | None = None,
    target_stage: int | None = None,
) -> None:
    source_tasks = json.loads((SOURCE_ROOT / "tasks.json").read_text())
    if len(source_tasks) != 800:
        raise ValueError(f"Expected frozen 800-task source, found {len(source_tasks)}")
    by_category = {
        category: [
            task for task in source_tasks if task["scenario"]["category"] == category
        ]
        for category in CATEGORIES
    }
    rng = random.Random(SEED)
    if count < 5 or count > 100:
        raise ValueError("count must be between 5 and 100")
    selected = []
    source_by_id = {task["id"]: task for task in source_tasks}
    quotas = {category: count // len(CATEGORIES) for category in CATEGORIES}
    for category in CATEGORIES[: count % len(CATEGORIES)]:
        quotas[category] += 1
    if source_task_ids:
        missing = sorted(set(source_task_ids) - set(source_by_id))
        if missing:
            raise ValueError(f"Unknown source task IDs: {missing}")
        selected = [source_by_id[task_id] for task_id in source_task_ids]
        count = len(selected)
        quotas = {
            category: sum(task["scenario"]["category"] == category for task in selected)
            for category in CATEGORIES
        }
        if (
            count == 5
            and target_stage is None
            and sorted(quotas.values()) != [1, 1, 1, 1, 1]
        ):
            raise ValueError(
                "five-task trial must contain exactly one task per category"
            )
    for category in CATEGORIES if not source_task_ids else ():
        candidates = list(by_category[category])
        rng.shuffle(candidates)
        selected.extend(candidates[: quotas[category]])
    if len({task["id"] for task in selected}) != count:
        raise ValueError("Hidden selection contains duplicate source tasks")

    hidden_tasks, plans, graphs, provenance = [], [], {}, {}
    hidden_counts = {1: 0, 2: 0, 3: 0}
    for index, source in enumerate(selected, 1):
        requests = stage_requests(source)
        selected_stage = target_stage if target_stage is not None else (index - 1) % 3
        candidates = candidate_values(requests[selected_stage])
        if not candidates and target_stage is None:
            for candidate_stage in range(3):
                candidates = candidate_values(requests[candidate_stage])
                if candidates:
                    selected_stage = candidate_stage
                    break
        if not candidates:
            raise ValueError(f"No natural hideable value found in {source['id']}")
        bindable = []
        for candidate in candidates:
            probe = build_graph(source, [candidate], source["id"], selected_stage)
            node = next(node for node in probe.nodes if node.visibility == "withheld")
            if node.tool and node.argument_path:
                bindable.append(candidate)
        candidates = bindable
        if not candidates:
            raise ValueError(
                f"No hideable stage-{selected_stage} value with a tool binding "
                f"found in {source['id']}"
            )
        desired_count = (
            1
            if target_stage is not None or count < 100
            else 1
            if index <= 50
            else 2
            if index <= 85
            else 3
        )
        if len(candidates) < desired_count:
            raise ValueError(
                f"Not enough independent hideable values in {source['id']} "
                f"for requested count {desired_count}"
            )
        hidden = candidates[: min(desired_count, len(candidates))]
        if len(hidden) not in hidden_counts:
            hidden = hidden[:3]
        hidden_counts[len(hidden)] += 1
        task = redact_task(source, hidden, selected_stage)
        task_id = task["id"]
        hidden_tasks.append(task)
        graph = build_graph(source, hidden, source["id"], selected_stage)
        graphs[task_id] = graph.model_dump(mode="json")
        requirements = [
            HiddenRequirement(
                node_id=f"g_hidden_{n}",
                field=f"detail_{n}",
                gold_value=value,
                question_hint=f"Ask specifically for the withheld {kind} detail.",
            )
            for n, (kind, value) in enumerate(hidden, 1)
        ]
        plans.append(
            HiddenIntentPlan(
                task_id=task_id,
                source_task_id=source["id"],
                source_task_sha256=digest(source),
                source_category=source["scenario"]["category"],
                target_stage=selected_stage,
                hidden_requirements=requirements,
                visible_request=task["scenario"]["stages"][selected_stage]["request"],
                private_request=requests[selected_stage],
                leakage_checks={"source_values_removed_from_user_events": True},
            ).model_dump(mode="json")
        )
        provenance[task_id] = {
            "source_task_id": source["id"],
            "source_task_sha256": digest(source),
            "source_category": source["scenario"]["category"],
            "target_stage": selected_stage,
            "hidden_count": len(hidden),
        }

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "tasks.json").write_text(
        json.dumps(hidden_tasks, ensure_ascii=False, indent=2) + "\n"
    )
    (output_root / "split_tasks.json").write_text(
        json.dumps(
            {
                "base": [task["id"] for task in hidden_tasks],
                "hidden_intent": [task["id"] for task in hidden_tasks],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    (output_root / "goal_graphs.json").write_text(
        json.dumps(graphs, ensure_ascii=False, indent=2) + "\n"
    )
    (output_root / "hidden_requirements.json").write_text(
        json.dumps(plans, ensure_ascii=False, indent=2) + "\n"
    )
    (output_root / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n"
    )
    manifest = {
        "version": "gaia2-hidden-intent-v1",
        "source_root": str(SOURCE_ROOT.resolve()),
        "source_tasks_sha256": digest(source_tasks),
        "tasks": count,
        "categories": quotas,
        "hidden_count_distribution": hidden_counts,
        "target_stage_distribution": {
            str(stage): sum(1 for plan in plans if plan["target_stage"] == stage)
            for stage in range(3)
        },
        "status": "authoring_draft",
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    (output_root / "review.json").write_text(
        json.dumps({"status": "pending", "checked": 0, "total": count}, indent=2) + "\n"
    )
    for item in hidden_tasks:
        Task.model_validate(item)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--source-task-ids", nargs="+")
    parser.add_argument("--target-stage", type=int, choices=[0, 1, 2])
    args = parser.parse_args()
    main(
        count=args.count,
        output_root=args.output_root,
        source_task_ids=args.source_task_ids,
        target_stage=args.target_stage,
    )
