"""Standard tau2 environment and task loaders for imported Gaia2 scenarios."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tau2.data_model.tasks import Task
from tau2.domains.gaia2.utils import GAIA2_DATA_DIR, read_snapshot
from tau2.environment.environment import Environment
from tau2.environment.scenario import ScenarioRuntime

POLICY = """You are the caller's personal assistant. Use the available application
tools to carry out the request. Preserve all supplied dates, quantities and
conditions. Ask when needed information is genuinely missing; do not invent it.
The authoritative date and time are supplied by System__get_current_time in UTC,
not the host computer's date. Calendar weeks start on Monday. When monitoring
future updates, use System__wait_for_notification and check application data again.
Read notifications with System__get_notifications. These tools do not pause the
call. Retain normal native permissions: ask the caller for approval when required.
Speak to the caller through the normal voice channel, not through an application
tool. Progress announcements must not claim that unfinished operations succeeded.
"""


class Gaia2Environment(Environment):
    """A domain environment; orchestration and evaluation remain shared."""

    supports_qwen_agent = True

    def __init__(
        self,
        task: Task,
        root: Path,
        *,
        semantic_scoring=None,
        allow_unverified=False,
        allow_stale_runtime=False,
        stage_control=True,
        runtime_mode="online",
    ):
        from tau2.domains.gaia2.tools import Gaia2Tools

        self.task = task
        self.root = Path(root)
        manifest = json.loads((self.root / "manifest.json").read_text())
        spec = task.scenario
        if spec is None:
            raise ValueError("Gaia2 task has no scenario configuration")
        if not allow_unverified:
            from tau2.domains.gaia2.utils import runtime_digest, verify_task_artifact
            from tau2.domains.gaia2.verify import verify_pinned_resources

            if not allow_stale_runtime and (
                not manifest.get("verified")
                or (
                    runtime_mode != "replay" and spec.runtime_sha256 != runtime_digest()
                )
            ):
                raise ValueError(
                    "Gaia2 data/runtime is not verified; run 'tau2 gaia2 verify --reference --seal'"
                )
            if len(spec.stages) > 1:
                from tau2.orchestrator.scenario_stages import SERIES_VERSION

                if (
                    not manifest.get("execution_ready")
                    or manifest.get("series_runtime_version") != SERIES_VERSION
                ):
                    raise ValueError(
                        "Gaia2 continuous runtime is not verified; run reference verification and seal"
                    )
            # Hidden-intent tasks are independently sealed and deliberately
            # reuse the canonical Gaia2 snapshot, assets and runtime. Their
            # package manifest owns task identity, so only the canonical task
            # byte lookup is skipped for this task-set prefix.
            if not task.id.startswith("hidden_"):
                verify_task_artifact(self.root, task)
            verify_pinned_resources(self.root, spec.assets_sha256)
        if runtime_mode not in {"online", "reference", "replay"}:
            raise ValueError("Unknown Gaia2 runtime mode")
        self.runtime_mode = runtime_mode
        self.stage_control = stage_control and not task.id.startswith("hidden_")
        managed = set()
        if stage_control and len(spec.stages) > 1:
            from tau2.orchestrator.scenario_stages import stage_requests

            managed = {event.id for event in stage_requests(spec)[1:]}
        self.scenario_runtime = ScenarioRuntime(
            spec, self._environment_operation, managed_user_events=managed
        )
        tools = Gaia2Tools(
            read_snapshot(spec.snapshot, self.root),
            assets=Path(manifest["assets"]),
            clock=self.scenario_runtime,
        )
        tools.semantic_scoring = semantic_scoring
        from tau2.domains.gaia2.tools import location_aliases, normalize_arguments

        aliases = location_aliases(tools.source_snapshot)
        self.scenario_runtime.argument_normalizer = (
            lambda name, arguments: normalize_arguments(name, arguments, aliases)
        )
        if semantic_scoring is not None and semantic_scoring.scorer.value == "semantic":
            from tau2.data_model.message import AssistantMessage
            from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

            def match_text(actual, expected):
                checks = CommunicateEvaluator.evaluate_communicate_info(
                    [AssistantMessage(role="assistant", content=actual)],
                    [expected],
                    scoring=semantic_scoring,
                )
                return checks[0].met

            self.scenario_runtime.semantic_matcher = match_text
        if runtime_mode == "online":
            from tau2.domains.gaia2.replies import ActualMailReplies
            from tau2.domains.gaia2.tools import json_value

            def lookup_mail(identifier):
                for folder in tools.apps["Emails"].folders.values():
                    try:
                        return json_value(folder.get_email_by_id(identifier))
                    except (KeyError, ValueError, IndexError):
                        continue
                raise ValueError(
                    "Successful outgoing email is missing from application state"
                )

            runtime = self.scenario_runtime
            runtime.event_triggers = ActualMailReplies(runtime, lookup_mail)
            # Only retain oracle speech matching for events that still need it.
            runtime._needed_anchors = {
                p
                for e in spec.events
                if e.kind != "reference"
                and e.id not in managed
                and e.id not in runtime.event_triggers.managed
                for p in e.dependencies
            }
            for _ in spec.events:
                before = set(runtime._needed_anchors)
                runtime._needed_anchors.update(
                    p for e in spec.events if e.id in before for p in e.dependencies
                )
                if before == runtime._needed_anchors:
                    break
        date = datetime.fromtimestamp(spec.start_time, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S %A UTC"
        )
        super().__init__(
            "gaia2",
            POLICY
            + f"\nThe scenario starts at {date}. Interpret relative dates against this scenario clock.\n",
            tools=tools,
        )

    def _environment_operation(self, name, arguments):
        return self.tools.execute(name, arguments)

    def close(self):
        """Release the trial's scheduler and owned filesystem."""
        super().close()
        self.tools.close()

    def evaluation_kwargs(self):
        """Replay the same task, resource root and semantic scoring profile."""
        return {
            "task": self.task,
            "root": self.root,
            "semantic_scoring": self.tools.semantic_scoring,
            "stage_control": self.stage_control,
            "runtime_mode": "replay",
        }


def get_environment(
    db=None,
    user_db=None,
    solo_mode=False,
    *,
    task=None,
    root=GAIA2_DATA_DIR,
    semantic_scoring=None,
    allow_unverified=False,
    allow_stale_runtime=False,
    stage_control=True,
    runtime_mode="online",
):
    """Construct one isolated environment through the normal domain registry."""
    if solo_mode:
        raise ValueError("Solo mode not supported for gaia2")
    if db is not None or user_db is not None:
        raise ValueError("Gaia2 initial state is selected by its pinned task snapshot")
    if task is None:
        task = get_tasks(root=root)[0]
    return Gaia2Environment(
        task,
        Path(root),
        semantic_scoring=semantic_scoring,
        allow_unverified=allow_unverified,
        allow_stale_runtime=allow_stale_runtime,
        stage_control=stage_control,
        runtime_mode=runtime_mode,
    )


def get_tasks(task_split_name=None, *, root=GAIA2_DATA_DIR) -> list[Task]:
    """Load converted native tasks; no Parquet dependency is needed at runtime."""
    path = Path(root) / "tasks.json"
    if not path.exists():
        raise FileNotFoundError("Import Gaia2 first with 'tau2 gaia2 import'")
    manifest = Path(root) / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()).get("verified"):
        from tau2.domains.gaia2.utils import verify_task_artifact

        verify_task_artifact(Path(root))
    tasks = [Task.model_validate(value) for value in json.loads(path.read_text())]
    if task_split_name is None:
        return tasks
    splits = get_tasks_split(root=root)
    if task_split_name not in splits:
        raise ValueError(f"Unknown Gaia2 split {task_split_name}")
    selected = set(splits[task_split_name])
    return [task for task in tasks if task.id in selected]


def get_tasks_split(*, root=GAIA2_DATA_DIR):
    """Expose base and five capability subsets through standard task selection."""
    return json.loads((Path(root) / "split_tasks.json").read_text())
