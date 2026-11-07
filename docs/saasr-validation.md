# SAASR caller integration validation — 2026-10-28

The implementation uses a continuous, looping background prepared once per call,
with every new caller utterance synthesized through the SAASR foreground path.
It is optional and independent of the tested Agent provider and benchmark domain.

## Offline and reference checks

- Voice/streaming regression: 697 passed, 3 skipped; live integration tests excluded.
- Targeted Gaia2, scenario, replay, checkpoint and language-construction regression:
  66 passed.
- SAASR pipeline, audio, scheduling, CLI and continuous rendering regression:
  33 passed with fake TTS and no model calls.
- Final caller-renderer, cleanup, checkpoint and construction checks: 61 passed.
- Gaia2 reference execution and strict replay: 800/800 passed and resealed.
- Ruff lint, formatting and whitespace checks passed in the changed code.

The broad core snapshot recorded 2,895 passes, 48 failures and 6 setup errors.
Two new environment-cleanup compatibility failures were subsequently fixed and
rechecked. Other recorded failures concern frozen retail/intake bindings and
reviewed baseline snapshots; their published artifacts were preserved.
The SAASR checkout also has three existing full-suite failures: a missing local
`qwen3_tts_base.yaml` and two repository-hygiene checks affected by the locally
added Gaia2 resource/data directory. The targeted rendering regressions pass.

## Real GPU audio check

The downloaded Qwen3-TTS-12Hz-1.7B-Base weights and speech tokenizer were verified
against their published SHA-256 identities at revision
`fd4b254389122332181a7c3db7f27e918eec64e3`.
The worker used the SAASR runtime image with digest
`sha256:78ce4352ad0e1bf82d45d22bd31ed7c8fc54fe257cd74a8b6bd258175407b839`
on a single visible GPU. Model weights live in `/tmp/tau-saasr-model-20261028`
because shared-home quota prevented storing them there. This is temporary storage;
copy verified model files to a durable provisioned location for production runs.

The first real multi-turn audio check synthesized three new foreground texts,
producing 17.8 seconds of continuous audio. Foreground, background and noise tracks
reconstruct the pre-codec mixture within one PCM16 quantization step. The output
peak was −6.24 dBFS. Separate mixed and transmitted WAVs were saved. This check
makes no Agent or judge API calls and does not measure task accuracy.

The final audio check used the original Gaia2 request as its first foreground,
followed by two new replies. It produced 19.4 seconds of continuous audio, with
the same track-reconstruction check passing and a peak of −6.32 dBFS.

## One live Qwen call

Exactly one live attempt used Gaia2 task
`0029_5psdqgpwhjokawkbnio3cls6fxbm8du0`, seed 300, one concurrent call,
a 180-second audio budget and zero automatic retries.

The initial task request and two subsequent generated check-ins were synthesized
through SAASR and delivered. Recorded continuous scene playback was 160.2 seconds.
The tested Agent produced no observed speech or business tool calls; the trial
ended at `max_steps`, with reward 0 and evaluation skipped by the existing
premature-termination rule. This demonstrates live audio-path execution, not a
successful task outcome. The task was not rerun to seek a passing score.

## Local evidence

Evidence is under `data/simulations/saasr-validation-20261028/`:

- `validation-report.json`, `runtime.local.json`, `scenes.local.json`.
- `model-source.json`, `audio-check/`, and `audio-check-final/`.
- Regression logs and `gaia2-seal.log`.
- `pre-saasr-seal/` preserves the previous task/manifest seal files.

The live attempt is under `data/simulations/saasr-live-20261028-once/`.
Per-trial `audio/saasr/` contains foregrounds, background sources, sent audio and
playback metadata. These local evidence and model files are not committed.

The [user guide](saasr-user-audio.md) describes installation and the renderer switch.
