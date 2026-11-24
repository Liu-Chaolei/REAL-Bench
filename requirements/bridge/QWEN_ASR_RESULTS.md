# Qwen frontend ASR switch — 2026-10-27

This is a historical connectivity check. The current configuration restores
MiMo on the Agent frontend and uses Qwen on the simulated caller's listening
side instead; see [USER_ASR_HOTWORDS.md](USER_ASR_HOTWORDS.md).

The retail and Gaia2 speech configurations now use
`qwen-audio-3.1-asr-flash` for the Agent's frontend recognition. They select
`QWEN_AUDIO_REALTIME_PROVIDER`, `QWEN_AUDIO_REALTIME_BASE_URL` and
`DASHSCOPE_API_KEY3` from the existing private dotenv file. The configured
DashScope WebSocket address supplies the regional host for its HTTP recognition
endpoint. Uploads are normalized to PCM16 mono WAV at 16 kHz.

Frontend LLM remains MiniMax-M3 with the existing fallback. Agent TTS and the
evaluation-side caller/played-audio observer retain their MiMo configuration.
No hotword list was added. The private dotenv file was verified unchanged.

Image `docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.2` is built locally:
`sha256:d4aaa3419c03059458b37bfcd5535600b9d082fe902b721a53c4d3c08c6b6366`.
It inherits the exact filesystem layers of the verified `0.5.1` image after
checking the Qwen source revision/archive and dependency locks. Images without
the Qwen ASR capability are rejected before launch.

Validation:

- 70 targeted Python tests and 63 streaming/recovery regressions passed.
- All 14 complete image contracts passed, including real Gateway, native ACP,
  Qwen Code, MCP permissions and both ASR provider paths; remote models were
  stubbed for these image contracts.
- A separate live Qwen ASR request using the selected credential succeeded on
  the public `check_order_12345.wav` fixture, returning
  “Can you check the status of my order number 12345?” in approximately 0.86 s.
  Reported usage: 4 audio seconds, 45 input tokens and 16 output tokens.
  This single request does not establish general latency or recognition quality.
- Standard provider suite: 132 tests skipped by their live-provider gates.
- Changed Python files pass Ruff lint and formatting; no credential values were
  found in the checked source or validation artifacts.

No full online benchmark was run. Evidence is saved under
[`data/simulations/qwen-frontend-asr-switch-20261027/`](../../data/simulations/qwen-frontend-asr-switch-20261027/),
including the summary, live response and complete image-contract log.

Rebuild with the same verified dependency base:

```bash
PYTHONNOUSERSITE=True UV_FROZEN=1 .venv/bin/python \
  integrations/qwen_audio_agent/build_image.py /path/to/qwen-audio-agent \
  --base-image docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.1 \
  --tag docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.2
```
