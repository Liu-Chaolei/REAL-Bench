"""Published ARE semantic groups with tau's cached model transport."""

from types import SimpleNamespace

from tau2.diagnostics.execution.checks import POLICY


def native_compare(task, reference, row, model, user):
    """Execute the original joint checker group over selected text parameters."""
    from tau2.diagnostics.execution.native.constants import CheckerType, SoftCheckerType
    from tau2.diagnostics.execution.native.soft_judge import SoftToolJudge

    fields = row.get("fields", ["content"])
    checks = POLICY["soft_checks"].get(reference["tool"], ["content_checker"])
    config = SimpleNamespace(
        tool_name=reference["tool"],
        engine=model.text_engine,
        arg_to_checker_type={f: CheckerType.llm_checker for f in fields},
        soft_checker_types=[SoftCheckerType(c) for c in checks],
    )
    evaluator = SoftToolJudge(config)

    def event(values):
        return SimpleNamespace(
            get_args=lambda: values, event_time=task.scenario.start_time
        )

    source = next(
        (s for s in task.scenario.stages if s.stage == reference.get("stage", 0)), None
    )
    details = SimpleNamespace(
        first_name=user.get("first_name", ""),
        last_name=user.get("last_name", ""),
        address=user.get("address", ""),
    )
    return evaluator.compare(
        event(row["actual"]),
        event(row["expected"]),
        tasks=[source.request] if source else [],
        user_details=details,
    )
