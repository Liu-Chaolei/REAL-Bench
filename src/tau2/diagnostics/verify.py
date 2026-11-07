"""Key-free reference execution and strict replay with diagnostic state capture."""

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from tau2.data_model.diagnostics import DiagnosticEvidence, UserInput
from tau2.data_model.tasks import Task
from tau2.diagnostics.collect import EvidenceRecorder
from tau2.diagnostics.execution.gaia2 import Gaia2ExecutionAdapter, metadata
from tau2.diagnostics.intent.io import write_json


class NoModel:
    """Unresolved semantic checks are reported; verification never calls a model."""

    def judge(self, *args):
        raise RuntimeError("semantic_check_not_run_offline")

    def text_engine(self, *args, **kwargs):
        raise RuntimeError("semantic_check_not_run_offline")


def check_one(args):
    """Observe real reference/replay paths without changing the reference verdict."""
    payload, root, assets, output = args
    from tau2.domains.gaia2.verify import _check_reference

    observed = []
    task = Task.model_validate(payload)

    def attach(env):
        recorder = EvidenceRecorder(env, task, Path(output) / task.id / "evidence")
        env.diagnostic_recorder = recorder
        observed.append((env, recorder))

    result = _check_reference(payload, root, assets, observer_factory=attach)
    if not observed:
        return result | {
            "diagnostics": {
                "status": "unavailable",
                "reason": "reference_not_constructed",
            }
        }
    env, recorder = observed[0]
    recorder.close()
    rows = []
    for stage in task.scenario.stages:
        event_ids = set(stage.event_ids)
        starts = [
            r
            for r in env.scenario_runtime.records
            if r.kind == "user" and r.event_id in event_ids
        ]
        rows.append(
            {
                "stage": stage.stage,
                "started": {"time": starts[0].time} if starts else None,
                "completed": {"time": starts[0].time} if starts else None,
            }
        )
    delivered = []
    stage_by_event = {
        e: stage.stage for stage in task.scenario.stages for e in stage.event_ids
    }
    current_stage = 0
    for row in env.scenario_runtime.records:
        if row.kind == "user" and row.event_id in stage_by_event:
            current_stage = stage_by_event[row.event_id]
        if row.kind in {"user", "agent"} and row.result:
            delivered.append(
                UserInput(
                    id=str(row.sequence),
                    text=str(row.result),
                    started=row.time,
                    ended=row.time,
                    stage=current_stage,
                    role="user" if row.kind == "user" else "assistant_context",
                )
            )
    evidence = DiagnosticEvidence(
        simulation_id=task.id,
        task_id=task.id,
        domain="gaia2",
        user_inputs=delivered,
        operations=recorder.operations,
        frames=recorder.frames,
        stages={"stages": rows},
        source={
            "initial_frame": recorder.initial_frame,
            "final_frame": recorder.final_frame,
            "termination_reason": "user_stop",
        },
        errors=recorder.errors,
    )
    try:
        diagnostic = Gaia2ExecutionAdapter(metadata(root)).evaluate(
            task, evidence, NoModel()
        )
    except Exception as exc:
        diagnostic = {
            "status": "grader_error",
            "reason": type(exc).__name__ + ": " + str(exc),
        }
    write_json(Path(output) / task.id / "diagnostics.json", diagnostic)
    return result | {
        "diagnostics": {
            "status": diagnostic["status"],
            "dimensions": {
                k: {x: v.get(x) for x in ("score", "coverage", "status")}
                for k, v in diagnostic.get("dimensions", {}).items()
            },
            "capture_errors": recorder.errors,
        }
    }


def verify(root, output, workers=4, limit=None):
    """Write an auditable full-task reference report without semantic calls."""
    root, output = Path(root), Path(output)
    tasks = json.loads((root / "tasks.json").read_text())
    if limit is not None:
        tasks = tasks[:limit]
    assets = json.loads((root / "manifest.json").read_text())["assets"]
    inputs = [(t, root, assets, output) for t in tasks]
    if workers == 1:
        rows = [check_one(a) for a in inputs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(check_one, inputs))
    report = {
        "tasks": len(rows),
        "reference_valid": sum(r.get("valid", False) for r in rows),
        "replay_verified": sum(r.get("replay_verified", False) for r in rows),
        "grader_errors": sum(
            r.get("diagnostics", {}).get("status") == "grader_error" for r in rows
        ),
        "records": rows,
        "model_calls": 0,
    }
    write_json(output / "report.json", report)
    return report
