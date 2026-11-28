# Salesforce Launa v1

The `launa` provider connects τ-voice to a hosted full-duplex voice service.
The adapter handles audio conversion and transport; benchmark tools execute in τ-voice.

## Setup

Install the `voice` extra and obtain the endpoint and key from the operator:

```bash
uv sync --extra voice
export LAUNA_WS_URL='wss://<operator-provided-host>/v1/voice/ws'
# Set LAUNA_API_KEY through your secret-management mechanism.
```

The adapter authenticates with `Authorization: Bearer <API_KEY>`. WSS is required;
credentials must not appear in the URL or repository.

The endpoint accepts the approved Gaia2 policy and complete tool schema. Coordinate
runtime revisions, concurrency limits, and availability with the operator.
Arbitrary toy policies and extra tools are rejected.

## Evaluation

```bash
for task_id in 0002_46myz87t501b0c902dy81rlbqbye2dyi; do
  tau2 run --domain gaia2 --audio-native \
    --audio-native-provider launa --audio-native-model launa-v1 \
    --user-llm gpt-5.2-2025-12-11 --user-llm-args '{"temperature": 0}' \
    --review-model vertex_ai/claude-opus-4-5@20251101 \
    --seed 300 --tick-duration 0.2 --max-steps-seconds 1200 \
    --wait-to-respond-self 5 \
    --speech-complexity regular --num-trials 1 --verbose-logs \
    --task-ids "$task_id" --save-to "launa_v1_${task_id}"
done
```

Provide separate credentials for the customer simulator, customer TTS, and
Vertex AI reviewer. `LAUNA_API_KEY` covers only the agent endpoint. Follow the
[submission guide](../../../../../docs/leaderboard-submission.md) and
[voice configuration guide](../../../../../src/tau2/voice/README.md).

## Methodology

This is a **Custom** submission with unchanged benchmark prompts, tools, policies,
and evaluator. No privileged utterance boundaries or hidden task/evaluator inputs
are sent to the agent. Scores and interaction metrics are in
[submission.json](../../../../../web/leaderboard/public/submissions/salesforce-launa-v1_salesforce_2026-10-06/submission.json).

The reported run covered all 278 tasks, one scored trial each, using voice user
simulator v1.0 and account-owned ElevenLabs `eleven_v3` customer voices. It requested
`gpt-5.2` at temperature 0; recorded responses identify `gpt-5.2-2025-12-11`.

**Configuration exception:** the scored run used 16,384 context tokens except
Retail tasks 98 and 104 at 32,768. Task 98 was retried after ungraded context-overflow
errors; task 104 used the planned larger-context phase. Previously graded results
were retained. These scores require Sierra's acceptance of the exception or a
uniform-configuration rerun. The current endpoint uses 32,768 tokens for every
session; the command above does not reproduce the historical mixed-context run.

## Tests

```bash
uv run pytest tests/test_voice/test_audio_native/test_launa -v
```

These tests use mock servers and require no endpoint credentials.
