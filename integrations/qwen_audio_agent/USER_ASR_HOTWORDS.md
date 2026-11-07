# Simulated caller ASR with automatic hotwords

The optional listener profile uses MiMo ASR on the Agent frontend and
`qwen-audio-3.1-asr-flash` on the evaluation-side listener. The supplied retail
and Gaia2 configurations currently use MiMo on both sides with hotwords disabled.
When enabled, the listener reads
`QWEN_AUDIO_REALTIME_PROVIDER`, `QWEN_AUDIO_REALTIME_BASE_URL` and
`DASHSCOPE_API_KEY3` from the existing private dotenv file. Its provider must be
`dashscope`; the configured regional WebSocket host is mapped to the synchronous
HTTP recognition endpoint. With the MiMo frontend, the listener credential and
vocabulary stay outside the Agent container. The existing `0.5.2` image can be reused.

## Per-task preparation

Every enabled call prepares its own vocabulary before starting the conversation
clock. The shared builder reads the initial user request and known information.
Domains can additionally expose initial user-visible material through
`Environment.get_user_asr_materials(task)` and the corresponding toolkit hook.
Tasks without a domain hook still receive extraction from their user instructions.

Gaia2 supplies personal contact and participant names, calendar event/location
labels, shopping product names and apartment/city names. Internal contacts,
scoring data, reference operations and unreleased scenario messages are excluded.
Retail resolves the caller from stated identity and supplies that caller's name,
address and own order products; other users and forgotten email addresses are
excluded. Free-text entity extraction uses the configured user-simulation model,
with one structured-output correction permitted by the shared generation helper.
Every proposed phrase must be a literal quote from its named input source.

Structured phrases and model extractions are merged with Unicode-preserving
whitespace normalization and deduplication. Default weight is 4. At most 2,000
phrases are sent, ordered by explicit task mentions, caller/contact names, then
other background material. Stable sorting determines truncation, which is recorded.
Numeric-only strings, internal IDs, email addresses and phrases longer than
80 characters are discarded and included in the rejected-term count.
The resulting vocabulary is fixed throughout the call and its audio reviews.

The default cache is `data/.asr_hotword_cache/` (ignored by Git). A per-key lock
prevents simultaneous trials from generating conflicting entries. The key covers
all permitted material, language, extractor version, model/argument digest, weight
and size limit. Cache files contain terms and provenance, never API credentials.
Extraction failure stops preparation; it does not silently select another model
or omit the vocabulary.

## Configuration and observation

These settings belong at the top level of the Qwen Agent configuration, separate
from `speech_to_speech`, which configures the Agent frontend:

```json
{
  "asr_provider": "qwen",
  "asr_model": "qwen-audio-3.1-asr-flash",
  "asr_provider_env": "QWEN_AUDIO_REALTIME_PROVIDER",
  "asr_base_url_env": "QWEN_AUDIO_REALTIME_BASE_URL",
  "asr_api_key_env": "DASHSCOPE_API_KEY3",
  "asr_hotwords": {"enabled": true, "weight": 4, "max_terms": 2000}
}
```

Set `asr_hotwords.enabled` to false for a Qwen-without-hotwords comparison; no
extraction call is made. The supplied configurations set `asr_provider` to
`mimo`, `asr_model` to `mimo-v2.5-asr`, and `enabled` to false, so no Qwen
listener request or hotword extraction occurs in ordinary runs.

The listener processes only already-played audio, in asynchronous segments capped
at 4 seconds or 0.6 seconds of trailing silence. HTTP requests receive 16 kHz
PCM16 WAV and `parameters.vocabulary`. Timestamps use measured segment bounds,
not provider word alignment. Bounded audio reviews use the same vocabulary,
remain capped by their initiating snapshot, and preserve primary transcripts.

Simulation metadata records the full vocabulary, source references, vocabulary
hash, cache status, extraction usage/cost, recognition usage and release delay.
When audio artifacts are enabled, the prepared profile is also saved as
`observer_hotwords.json` next to the audio taps. Every call owns its profile;
it is never inserted into Agent policy or the user's conversational prompt.

## Validation

Evidence and the MiMo / Qwen without words / Qwen with words recording comparison
are in `data/simulations/user-asr-hotwords-validation-20261027/`. This is a small
connectivity and transcription comparison, not a full benchmark. In the sampled
audio, adding the full background vocabulary did not reliably recover the correct
accented surname. The comparison also records the additional input-token usage
and latency; vocabulary acceptance alone does not establish recognition gains.
See [USER_ASR_HOTWORDS_RESULTS.md](USER_ASR_HOTWORDS_RESULTS.md) for final checks.

The Gaia2 source has been reference/replay-verified and resealed. Old series
directories are preserved; their successor inventories are `v2-asr-hotwords`,
`v2-asr-hotwords-examples` and `v2-asr-hotwords-initial-attempt`. Active authored
drafts retain their content and have been rebound and reference/replay-verified.
Old model reviews are retained as history; the new binding needs fresh model
review before any release. The pilot launcher defaults to `v2-asr-hotwords`.
