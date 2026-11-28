# Voice (Full-Duplex)

The active voice benchmark is English Gaia2. The user simulator streams
synthesized speech while the Agent responds through a full-duplex provider.

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke \
  --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.unified.json \
  --metrics-config examples/real-metrics.unified.yaml \
  --num-tasks 1
```

## Providers

| Provider | Flag | Requirements |
|----------|------|--------------|
| OpenAI Realtime | `--audio-native-provider openai` | `OPENAI_API_KEY` |
| xAI Grok Voice | `--audio-native-provider xai` | `XAI_API_KEY` |
| Avartha Pipecat | `--audio-native-provider avartha_pipecat` | Hosted Agent URL and `AVARTHA_API_KEY` |
| Qwen Audio Agent | `--audio-native-provider qwen_agent` | Docker and Qwen configuration |

The Qwen implementation is under `src/tau2/adapters/qwen_audio_agent/`; the
container and bridge material is under `integrations/qwen_audio_agent/`.

## Audio pipeline

`audio_native/` contains real-time provider adapters. `synthesis/` renders
caller speech and applies effects. `transcription/` provides ASR. `utils/`
contains format conversion, WAV I/O, and shared helpers.

The canonical formats are telephony μ-law 8kHz for provider transport, PCM16
16kHz for synthesis, and PCM16 24kHz for played-audio transcription. Preserve
the fixed effects order documented in the audio-effects module.

## Main options

- `--audio-native` enables full-duplex mode.
- `--audio-native-provider` selects the provider.
- `--speech-complexity` selects caller audio effects.
- `--tick-duration` controls the simulation tick.
- `--max-steps-seconds` controls the simulated conversation limit.
- `--verbose-logs` saves audio, tick data, and provider diagnostics.

Voice results use the standard `data/results/` output layout. See
[Running Gaia2 Simulations](../../../docs/running_simulations.md) and the
[Qwen integration guide](../../../integrations/qwen_audio_agent/README.md).
