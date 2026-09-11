import httpx
import pytest

from adapters.gaia2.evaluation_campaign import CONFIGS, check_gate, select
from adapters.gaia2.qwen_audio_template.environment.runner.model_proxy import make_app
from adapters.gaia2.qwen_audio_template.environment.runner.responses_judge import (
    ResponsesJudge,
)


def test_selection_is_fixed_balanced_and_excludes_debug():
    rows = [
        {
            "config": c,
            "source_id": f"{c}{i}",
            "duration_sec": i,
            "formal_replies": i % 3,
            "actions": i % 12,
        }
        for c in CONFIGS
        for i in range(25)
    ]
    first = select(rows)
    assert first == select(list(reversed(rows)))
    assert len(first) == 50
    assert all(sum(r["config"] == c for r in first) == 10 for c in CONFIGS)
    assert len({r["source_id"] for r in first}) == 50


def test_missing_evidence_cannot_open_scale_gate():
    report = check_gate({"seed_cases": []}, [], {}, {})
    assert not report["passed"]
    assert any("calibration" in e for e in report["failures"])
    assert any("Human" in e for e in report["failures"])


async def test_model_proxy_rejects_wrong_profile_and_protects_barrier(tmp_path):
    app = make_app(
        "http://upstream.invalid/v1",
        "SECRET",
        "gpt-6-astra",
        "low",
        "CONTROL",
        tmp_path,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://proxy"
    ) as client:
        for payload in [
            {"model": "gpt-6", "reasoning": {"effort": "low"}},
            {"model": "gpt-6-astra", "reasoning": {"effort": "high"}},
            {"model": "gpt-6-astra"},
        ]:
            response = await client.post("/v1/responses", json=payload)
            assert response.status_code == 400
            assert "SECRET" not in response.text
        assert (
            await client.post("/__barrier", json={"paused": True})
        ).status_code == 401
        assert (
            await client.post(
                "/__barrier",
                json={"paused": False},
                headers={"Authorization": "Bearer CONTROL"},
            )
        ).status_code == 200
        assert (await client.get("/v1/models")).json()["data"][0]["id"] == "gpt-6-astra"
    failures = (tmp_path / "provider-failures.jsonl").read_text()
    assert "SECRET" not in failures
    assert "adapter_model_profile" in failures


def test_judge_record_and_replay_require_exact_input(tmp_path):
    def respond(request):
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "[[Success]]"}],
                    }
                ],
                "usage": {"total_tokens": 5},
            },
        )

    path = tmp_path / "judge.jsonl"
    messages = [{"role": "user", "content": "compare"}]
    judge = ResponsesJudge(
        "gpt-6-astra",
        "http://judge/v1",
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        record_path=path,
    )
    expected = judge(messages)
    judge.close()
    assert "secret" not in path.read_text()
    replay = ResponsesJudge("gpt-6-astra", "", "", replay_path=path)
    assert replay(messages) == expected
    with pytest.raises(RuntimeError, match="exhausted"):
        replay(messages)
    replay.close()
    replay = ResponsesJudge("gpt-6-astra", "", "", replay_path=path)
    with pytest.raises(RuntimeError, match="differs"):
        replay([{"role": "user", "content": "different"}])
    replay.close()


async def test_backend_outage_is_recorded_as_provider_failure_without_secrets(tmp_path):
    def unavailable(request):
        assert request.headers["authorization"] == "Bearer SECRET"
        return httpx.Response(503, json={"error": {"code": "server_error"}})

    app = make_app(
        "http://backend.invalid/v1",
        "SECRET",
        "gpt-6-astra",
        "low",
        "CONTROL",
        tmp_path,
        transport=httpx.MockTransport(unavailable),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://proxy"
    ) as client:
        response = await client.post(
            "/v1/responses",
            json={
                "model": "gpt-6-astra",
                "reasoning": {"effort": "low"},
                "input": "probe",
            },
        )
        assert response.status_code == 503
    evidence = (tmp_path / "provider-failures.jsonl").read_text()
    assert "provider_unavailable" in evidence
    assert "SECRET" not in evidence


async def test_proxy_seal_drains_active_request_and_freezes_evidence(tmp_path):
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def unavailable(request):
        started.set()
        await release.wait()
        return httpx.Response(503, json={"error": {"code": "server_error"}})

    app = make_app(
        "http://backend.invalid/v1",
        "SECRET",
        "gpt-6-astra",
        "low",
        "CONTROL",
        tmp_path,
        transport=httpx.MockTransport(unavailable),
    )
    payload = {"model": "gpt-6-astra", "reasoning": {"effort": "low"}}
    headers = {"Authorization": "Bearer CONTROL"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://proxy"
    ) as client:
        assert (await client.post("/__seal")).status_code == 401
        async with asyncio.TaskGroup() as group:
            pending = group.create_task(client.post("/v1/responses", json=payload))
            await asyncio.wait_for(started.wait(), 2)
            sealing = group.create_task(client.post("/__seal", headers=headers))
            await asyncio.sleep(0)
            assert not sealing.done()
            assert (await client.post("/v1/responses", json=payload)).status_code == 409
            release.set()
        assert pending.result().status_code == 503
        assert sealing.result().json() == {"sealed": True}
        frozen = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        assert "request-000001.metadata.json" in frozen
        assert "provider-failures.jsonl" in frozen
        # Even profile-invalid retries must not append a failure after sealing.
        assert (await client.post("/v1/responses", json={})).status_code == 409
        assert (
            await client.post("/__barrier", headers=headers, json={"paused": True})
        ).status_code == 409
        assert (await client.post("/__seal", headers=headers)).status_code == 200
        assert frozen == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


async def test_proxy_seal_releases_paused_admission_without_dispatch(tmp_path):
    import asyncio

    def unexpected(request):
        pytest.fail("Sealed queued request reached upstream")

    app = make_app(
        "http://backend.invalid/v1",
        "SECRET",
        "gpt-6-astra",
        "low",
        "CONTROL",
        tmp_path,
        transport=httpx.MockTransport(unexpected),
    )
    headers = {"Authorization": "Bearer CONTROL"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://proxy"
    ) as client:
        await client.post("/__barrier", headers=headers, json={"paused": True})
        async with asyncio.TaskGroup() as group:
            pending = group.create_task(
                client.post(
                    "/v1/responses",
                    json={"model": "gpt-6-astra", "reasoning": {"effort": "low"}},
                )
            )
            await asyncio.sleep(0)
            assert (await client.post("/__seal", headers=headers)).json() == {
                "sealed": True
            }
        assert pending.result().status_code == 409
        assert not list(tmp_path.iterdir())
