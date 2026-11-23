from types import SimpleNamespace

from tau2.diagnostics.request_changes import management_events


def test_management_projection_joins_actual_calls_without_using_user_requests():
    evidence = SimpleNamespace(
        source={
            "active_metrics": {
                "metrics": {
                    "persistent_task": {
                        "operations": [
                            {
                                "call_id": "c",
                                "operation": "modify",
                                "backend_task_id": "task",
                                "timestamp": 1,
                                "arguments": {"instruction": "15:00"},
                            },
                            {
                                "call_id": "c",
                                "operation": "modify",
                                "backend_task_id": "task",
                                "timestamp": 2,
                                "result": {"accepted": True},
                                "success": True,
                            },
                            {
                                "call_id": "d",
                                "operation": "cancel",
                                "backend_task_id": "other",
                                "timestamp": 3,
                                "success": False,
                            },
                        ]
                    }
                }
            }
        }
    )
    rows = management_events(evidence)
    assert len(rows) == 2
    assert rows[0]["arguments"] == {"instruction": "15:00"}
    assert rows[0]["result"] == {"accepted": True}
    assert rows[1]["task_id"] == "other" and rows[1]["success"] is False
    assert management_events(SimpleNamespace(source={})) == []


def test_feedback_change_boundary_is_a_view_not_trace_mutation():
    from tau2.data_model.diagnostics import DiagnosticEvidence, UserInput
    from tau2.diagnostics.request_changes import feedback_evidence

    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        user_inputs=[
            UserInput(id="u", text="14:00", started=0, ended=1, stage=0),
            UserInput(
                id="a",
                text="Created at 14:00",
                started=2,
                ended=3,
                stage=0,
                role="assistant_context",
            ),
            UserInput(
                id="m",
                event_id="modify",
                text="15:00 instead",
                started=4,
                ended=5,
                stage=0,
            ),
            UserInput(
                id="b",
                text="Updated",
                started=6,
                ended=7,
                stage=0,
                role="assistant_context",
            ),
        ],
        stages={"stages": [{"stage": 0, "started": {"time": 100}, "completed": True}]},
        source={
            "active_metrics": {
                "metrics": {
                    "persistent_task": {
                        "request_id": "modify",
                        "delivery": {"delivered_at": 104},
                    }
                }
            }
        },
    )
    view = feedback_evidence(evidence)
    assert [r.stage for r in view.user_inputs] == [0, 0, 1, 1]
    assert [r.stage for r in evidence.user_inputs] == [0, 0, 0, 0]
    assert view.stages["stages"][1]["started"]["time"] == 104


def test_cancel_has_two_delivered_request_boundaries_and_keeps_old_reply():
    from tau2.data_model.diagnostics import DiagnosticEvidence, UserInput
    from tau2.diagnostics.request_changes import feedback_evidence, request_relations

    inputs = [
        UserInput(id="a", event_id="A", text="Do A", started=0, ended=1, stage=0),
        UserInput(
            id="r",
            text="A is ready",
            role="assistant_context",
            started=2,
            ended=3,
            stage=0,
        ),
        UserInput(id="b", event_id="B", text="Also do B", started=4, ended=5, stage=0),
        UserInput(
            id="c",
            event_id="cancel",
            text="Cancel B, keep A",
            started=8,
            ended=9,
            stage=0,
        ),
    ]
    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        user_inputs=inputs,
        stages={"stages": [{"stage": 0, "started": {"time": 100}, "completed": True}]},
        source={
            "active_metrics": {
                "metrics": {
                    "persistent_task": {
                        "variant_type": "cancel",
                        "requests": [
                            {
                                "request_id": "B",
                                "phase": "temporary",
                                "previous_request_id": "A",
                                "delivery": {
                                    "delivery_started_at": 104,
                                    "delivered_at": 105,
                                },
                            },
                            {
                                "request_id": "cancel",
                                "phase": "followup",
                                "previous_request_id": "B",
                                "delivery": {
                                    "delivery_started_at": 108,
                                    "delivered_at": 109,
                                },
                            },
                            {"request_id": "never", "delivery": {"delivered_at": None}},
                        ],
                    }
                }
            }
        },
    )
    view = feedback_evidence(evidence)
    assert [r.stage for r in view.user_inputs] == [0, 0, 1, 2]
    assert [r.stage for r in evidence.user_inputs] == [0, 0, 0, 0]
    assert request_relations(evidence)["cancel"]["previous_request_id"] == "B"
    assert "never" not in request_relations(evidence)


def test_late_management_result_is_hidden_from_earlier_feedback_window():
    from tau2.data_model.diagnostics import DiagnosticEvidence, UserInput
    from tau2.diagnostics.conciseness.evidence import build_windows
    from tau2.diagnostics.conciseness.models import FeedbackConfig

    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        user_inputs=[
            UserInput(id="a", text="14:00", started=0, ended=1, stage=0),
            UserInput(id="b", text="15:00", started=5, ended=6, stage=1),
        ],
        stages={
            "stages": [
                {"stage": 0, "started": {"time": 100}, "completed": True},
                {"stage": 1, "started": {"time": 105}, "completed": True},
            ]
        },
        audio={
            "timeline": [
                {"start": 0, "end": 20, "monotonic_start": 0, "monotonic_end": 20}
            ]
        },
        source={
            "scenario_start_time": 100,
            "scenario_monotonic_origin": 0,
            "active_metrics": {
                "metrics": {
                    "persistent_task": {
                        "operations": [
                            {
                                "call_id": "c",
                                "operation": "modify",
                                "timestamp": 104,
                                "kind": "native_operation_started",
                                "arguments": {"time": "14:00"},
                            },
                            {
                                "call_id": "c",
                                "operation": "modify",
                                "timestamp": 108,
                                "kind": "native_operation_completed",
                                "result": {"accepted": True},
                            },
                        ]
                    }
                }
            },
        },
    )
    windows = build_windows(evidence, FeedbackConfig())
    before = windows[0]["sources"]["management:c"]
    after = windows[1]["sources"]["management:c"]
    assert "result" not in before
    assert before["completed_media"] is None
    assert after["result"] == {"accepted": True}
    assert after["completed_media"] == 8
