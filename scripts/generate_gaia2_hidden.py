"""Generate the independent 100-task Gaia2 hidden-intent package.

The generator intentionally uses the frozen pre-variant 800-task archive. It
creates a reviewable first data cut by masking concrete caller-provided values
from one stage request and recording the full request privately for the caller
simulator. It never edits the canonical Gaia2 task files.
"""

from __future__ import annotations

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
            value = match.group(1) if kind == "quoted" else match.group(0)
            value = " ".join(value.split())
            if len(value) >= 4 and value.casefold() not in {
                v.casefold() for _, v in values
            }:
                values.append((kind, value))
    return values


def replacement(kind: str) -> str:
    return {
        "email": "the recipient's email address",
        "quoted": "the exact title or message",
        "amount": "the exact amount",
        "date": "the exact date",
        "time": "the exact time",
        "clause": "the exact remaining request details",
    }[kind]


def mask_text(text: str, hidden: list[tuple[str, str]]) -> str:
    masked = text
    for kind, value in hidden:
        masked = re.sub(re.escape(value), replacement(kind), masked, flags=re.I)
    return masked


def stage_requests(task: dict) -> list[str]:
    return [stage["request"] for stage in task["scenario"]["stages"]]


def user_event_ids(task: dict) -> set[str]:
    return {
        event["id"] for event in task["scenario"]["events"] if event["kind"] == "user"
    }


def redact_task(task: dict, hidden: list[tuple[str, str]], target_stage: int) -> dict:
    result = copy.deepcopy(task)
    all_user_events = user_event_ids(result)
    for event in result["scenario"]["events"]:
        if event["id"] in all_user_events and isinstance(
            event["arguments"].get("content"), str
        ):
            event["arguments"]["content"] = mask_text(
                event["arguments"]["content"], hidden
            )
    for stage in result["scenario"]["stages"]:
        stage["request"] = mask_text(stage["request"], hidden)
    result["user_scenario"]["instructions"]["reason_for_call"] = mask_text(
        result["user_scenario"]["instructions"]["reason_for_call"], hidden
    )
    result["user_scenario"]["instructions"]["task_instructions"] += (
        "\n\n# Hidden-intent caller protocol\n"
        "The caller knows the complete private request below but must not volunteer "
        "the withheld details. Answer a direct, specific question about a withheld "
        "detail with its exact value. Do not reveal all withheld details together. "
        "If the Agent guesses or asks an unrelated question, say that you need the "
        "Agent to ask for the specific missing detail.\n"
        "Private complete task requests:\n"
        + "\n".join(
            f"Stage {stage_index}: {request}"
            for stage_index, request in enumerate(stage_requests(task))
        )
        + "\n"
        "Withheld details: " + "; ".join(f"{kind}={value}" for kind, value in hidden)
    )
    result["id"] = result["id"].replace("__hidden_intent_v1", "")
    result["id"] = f"hidden_{result['id']}_v1"
    result["description"]["purpose"] = "Gaia2 dynamic hidden-intent benchmark task"
    result["scenario"]["category"] = "hidden_intent"
    result["scenario"]["source_id"] = task["id"]
    return result


def build_graph(
    task: dict, hidden: list[tuple[str, str]], source_id: str
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
    for index, (kind, value) in enumerate(hidden, 1):
        nodes.append(
            HiddenGoalNode(
                id=f"g_hidden_{index}",
                kind="tool_argument",
                visibility="withheld",
                description=f"Withheld caller-provided {kind} detail; ask for the exact {kind}",
                argument_path=f"hidden.detail_{index}",
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
        nodes=nodes,
        protected_values=[value for _, value in hidden],
        backend_only_values=[
            event["id"]
            for event in task["scenario"]["events"]
            if event["kind"] == "reference"
        ],
    )
    return graph


def main() -> None:
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
    selected = []
    for category in CATEGORIES:
        candidates = list(by_category[category])
        rng.shuffle(candidates)
        selected.extend(candidates[:20])
    if len({task["id"] for task in selected}) != 100:
        raise ValueError("Hidden selection contains duplicate source tasks")

    hidden_tasks, plans, graphs, provenance = [], [], {}, {}
    hidden_counts = {1: 0, 2: 0, 3: 0}
    for index, source in enumerate(selected, 1):
        requests = stage_requests(source)
        target_stage = (index - 1) % 3
        candidates = candidate_values(requests[target_stage])
        if not candidates:
            for candidate_stage in range(3):
                candidates = candidate_values(requests[candidate_stage])
                if candidates:
                    target_stage = candidate_stage
                    break
        if not candidates:
            sentences = [
                part.strip()
                for part in re.split(r"(?<=[.!?])\s+", requests[target_stage])
            ]
            sentence = max(sentences, key=len, default=requests[target_stage]).strip()
            words = sentence.split()
            if len(words) < 5:
                raise ValueError(f"No hideable caller value found in {source['id']}")
            candidates = [("clause", sentence)]
        desired_count = 1 if index <= 50 else 2 if index <= 85 else 3
        if len(candidates) < desired_count:
            sentences = [
                part.strip()
                for part in re.split(r"(?<=[.!?])\s+", requests[target_stage])
            ]
            existing = {value for _, value in candidates}
            for sentence in sorted(sentences, key=len, reverse=True):
                if len(sentence.split()) < 5 or sentence in existing:
                    continue
                candidates.append(("clause", sentence))
                existing.add(sentence)
                if len(candidates) >= desired_count:
                    break
        hidden = candidates[: min(desired_count, len(candidates))]
        if len(hidden) not in hidden_counts:
            hidden = hidden[:3]
        hidden_counts[len(hidden)] += 1
        task = redact_task(source, hidden, target_stage)
        task_id = task["id"]
        hidden_tasks.append(task)
        graph = build_graph(source, hidden, source["id"])
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
                target_stage=target_stage,
                hidden_requirements=requirements,
                visible_request=task["scenario"]["stages"][target_stage]["request"],
                private_request=requests[target_stage],
                leakage_checks={"source_values_removed_from_user_events": True},
            ).model_dump(mode="json")
        )
        provenance[task_id] = {
            "source_task_id": source["id"],
            "source_task_sha256": digest(source),
            "source_category": source["scenario"]["category"],
            "target_stage": target_stage,
            "hidden_count": len(hidden),
        }

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "tasks.json").write_text(
        json.dumps(hidden_tasks, ensure_ascii=False, indent=2) + "\n"
    )
    (OUTPUT_ROOT / "split_tasks.json").write_text(
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
    (OUTPUT_ROOT / "goal_graphs.json").write_text(
        json.dumps(graphs, ensure_ascii=False, indent=2) + "\n"
    )
    (OUTPUT_ROOT / "hidden_requirements.json").write_text(
        json.dumps(plans, ensure_ascii=False, indent=2) + "\n"
    )
    (OUTPUT_ROOT / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n"
    )
    manifest = {
        "version": "gaia2-hidden-intent-v1",
        "source_root": str(SOURCE_ROOT.resolve()),
        "source_tasks_sha256": digest(source_tasks),
        "tasks": 100,
        "categories": {category: 20 for category in CATEGORIES},
        "hidden_count_distribution": hidden_counts,
        "target_stage_distribution": {
            str(stage): sum(1 for plan in plans if plan["target_stage"] == stage)
            for stage in range(3)
        },
        "status": "authoring_draft",
    }
    (OUTPUT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    (OUTPUT_ROOT / "review.json").write_text(
        json.dumps({"status": "pending", "checked": 0, "total": 100}, indent=2) + "\n"
    )
    for item in hidden_tasks:
        Task.model_validate(item)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
