"""Key-free inventory of all task tools, parameter fields and effect validators."""

import json
from collections import Counter
from pathlib import Path

from tau2.data_model.tasks import Task
from tau2.diagnostics.execution.checks import POLICY
from tau2.diagnostics.execution.effects import EffectEvaluator
from tau2.diagnostics.execution.gaia2 import metadata, reference_operations


def scan(root):
    """Unsupported definitions are explicit failures, never default passes."""
    root = Path(root)
    tools = metadata(root)
    counts, fields, effects, missing = Counter(), Counter(), Counter(), []
    tasks = json.loads((root / "tasks.json").read_text())
    for payload in tasks:
        task = Task.model_validate(payload)
        for op in reference_operations(task, tools):
            name = op["tool"]
            counts[name] += 1
            if name not in EffectEvaluator.HANDLERS:
                missing.append({"task": task.id, "tool": name, "kind": "effect"})
            else:
                effects[name] += 1
            for field, value in op["args"].items():
                if value not in (None, "", [], {}):
                    fields[name + ":" + field] += 1
                    if field not in POLICY["tool_args"].get(name, {}):
                        missing.append(
                            {
                                "task": task.id,
                                "tool": name,
                                "field": field,
                                "kind": "parameter",
                            }
                        )
    return {
        "version": "tau-real-coverage/1",
        "tasks": len(tasks),
        "tools": dict(counts),
        "parameters": dict(fields),
        "effects": dict(effects),
        "missing": missing,
        "complete": not missing,
    }
