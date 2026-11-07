# Real MiMo audio + full Qwen Agent smoke — 2026-10-24

MiMo TTS (`mimo-v2.5-tts`) and observer ASR (`mimo-v2.5-asr`) are implemented
and authenticated using the private `MIMO_API_KEY` reference. No ElevenLabs or
Deepgram account is required for these calls. The Qwen frontend uses the
user-selected `DASHSCOPE_API_KEY3`; its model remains
`qwen-audio-3.0-realtime-plus`. Qwen Code remains `0.26.0` with `MiniMax-M3 / low`.
Qwen Agent source and the original private environment file were not modified.

## Real task results

These are English retail control connectivity calls with a **240-second audio
budget per call**, not the released benchmark's full time budget or voice setup.
The customer, listener reactions and independent communication judge use the
existing configured MiniMax Responses service.

| Task | Result | Business MCP calls/results | Source run |
| --- | --- | --- | --- |
| 0 | Time budget exhausted; reward 0 | 0 / 0 | `qwen-mimo-smoke-20261024-three` |
| 1 | Time budget exhausted; reward 0 | 10 / 10, no tool errors | `qwen-mimo-smoke-20261024-three` |
| 2 | Time budget exhausted; reward 0 | 0 / 0 | `qwen-mimo-smoke-20261024-task2-native-recovery` |

Task 1 executed `find_user_id_by_name_zip`, `get_user_details`,
`get_order_details` and seven `get_product_details` calls through the real
Qwen Code backend and tau MCP server. Results were returned to the native
backend, and Agent speech was observed through actual played audio. This
validates the full business-tool path in addition to real two-way speech.
The calls did not finish their business objectives in the short budget; no
successful task completion or official benchmark score is claimed.

The initial three-task batch contained an infrastructure failure for task 2.
That row was retained; task 2 above is a separate run after compatibility fixes.
Earlier setup attempts are also retained, rather than replacing their records.
All attempts and the selected valid results are indexed in
`data/simulations/qwen-mimo-smoke-20261024-summary.json`.

The actual semantic communication judge was separately exercised against task
1's played-audio transcript. It produced a validated positive authentication-
information check in `qwen-mimo-smoke-20261024-three/judge_connectivity_check.json`.
This is an unscored connectivity diagnostic and does not alter any task reward.

## Recordings and inspection

Every run has its own `results.json`, serialized simulations, caller/Agent WAV
taps, bridge JSONL trace and redacted container log. For example:

```bash
integrations/qwen_audio_agent/run_hpc.sh view --file data/simulations/qwen-mimo-smoke-20261024-three
integrations/qwen_audio_agent/run_hpc.sh view --file data/simulations/qwen-mimo-smoke-20261024-task2-native-recovery
```

Task 1 Agent playback is in
`data/simulations/qwen-mimo-smoke-20261024-three/artifacts/task_1/sim_ff5f7499-98a1-4dce-ba25-46c8acd2e397/audio/taps/agent_output.wav`.
The corresponding caller speech is `user_tts-output.wav` in the same directory.

## Compatibility and validation

- Real MiMo TTS→ASR and telephony-audio checks succeeded with the specified models.
- Audio is normalized to 16 kHz PCM for caller synthesis and 24 kHz WAV for ASR.
- ASR asynchronously recognizes only already-played segments. Its timestamps are
  local segment bounds; they are not provider word alignments.
- The latest ASR summary measures release delay from the actual wall-clock end
  of playback (mean 0.94 s, max 1.51 s in the final task-2 call). Earlier task
  0/1 summaries used the nominal sample clock and included accumulated tick
  scheduling drift; those older latency numbers should not be compared directly.
- Docker's initial TCP/HTTP readiness race is handled before a call starts.
  Authentication failures still abort clearly. Native recoverable Gateway
  notifications remain diagnostics, preserving the original Agent's recovery.
- Listener decisions use the configured evaluator model, avoiding accidental
  calls to an unavailable default OpenAI model on the MiniMax endpoint.
- Provider output can arrive faster than playback. The configured queue now
  accommodates the normal benchmark audio budget while playback stays paced;
  future captions/audio are never revealed to the caller early.
- Related Python regressions: **80 passed, 2 live cases skipped**. The streaming
  user regression tier also passed during implementation. Image contracts:
  **12 passed**, using the real archived Gateway/ACP/Qwen Code with model stubs.
- Standard live provider-suite cases remain opt-in (132 skipped). These skips
  are separate from the real calls documented above.
- Credential scan of generated JSON/log artifacts found no forwarded key values.
- Model pricing is not present in the pinned LiteLLM map for MiniMax-M3. Logged
  cost-map messages and printed totals are not provider billing estimates;
  complete native Agent cost remains unknown.

To reproduce a new independent smoke run:

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke --num-tasks 3 --max-seconds 240
```
