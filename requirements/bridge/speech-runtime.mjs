// Managed stock HF pipeline, inside the same private filesystem as the Agent.
import { spawn } from 'node:child_process'
import { randomBytes, createHash } from 'node:crypto'
import { once } from 'node:events'
import { readFileSync } from 'node:fs'

export async function startSpeechToSpeech({ onFailure, timeoutMs = 120000 } = {}) {
  if (!process.env.TAU_S2S_CONFIG) return null
  const settings = JSON.parse(process.env.TAU_S2S_CONFIG)
  const audioToken = randomBytes(32).toString('hex')
  const redact = text => [audioToken, process.env.TAU_S2S_LLM_API_KEY, process.env.TAU_S2S_LLM_FALLBACK_API_KEY, process.env.TAU_S2S_MIMO_API_KEY, process.env.TAU_S2S_ASR_API_KEY]
    .filter(Boolean).reduce((content, key) => content.replaceAll(key, '[REDACTED]'), text)
  const child = spawn('/opt/s2s/bin/python', ['/opt/tau-bridge/speech/server.py'], {
    env: { ...process.env, PYTHONNOUSERSITE: 'True', PYTHONUNBUFFERED: '1',
      PYTHONDONTWRITEBYTECODE: '1', OMP_NUM_THREADS: '1', OPENBLAS_NUM_THREADS: '1',
      NUMBA_CACHE_DIR: `${process.env.TAU_TRIAL_ROOT}/cache/numba`,
      TORCH_HOME: '/opt/s2s-assets/torch', NLTK_DATA: '/opt/s2s-assets/nltk',
      HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1',
      TAU_S2S_AUDIO_TOKEN: audioToken,
    }, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let stopping = false, ready = false
  const stopped = once(child, 'exit').catch(() => {})
  const stop = async () => {
    stopping = true
    if (child.exitCode !== null || child.signalCode !== null) return
    child.kill('SIGTERM')
    const kill = setTimeout(() => child.kill('SIGKILL'), 7000)
    try { await stopped } finally { clearTimeout(kill) }
  }
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Speech-to-Speech startup timed out')), timeoutMs)
      let output = ''
      child.stdout.on('data', data => {
        output = (output + data.toString()).slice(-8192)
        if (!ready && output.includes('TAU_S2S_READY')) {
          ready = true; clearTimeout(timer); resolve()
        }
      })
      child.stderr.on('data', data => process.stderr.write(redact(data.toString())))
      child.on('error', () => {
        clearTimeout(timer)
        if (!ready) reject(new Error('Speech-to-Speech could not start'))
        else if (!stopping) onFailure?.(new Error('Speech-to-Speech process failed'))
      })
      child.on('exit', () => {
        clearTimeout(timer)
        if (!ready) reject(new Error('Speech-to-Speech exited during startup; inspect private container log'))
        else if (!stopping) onFailure?.(new Error('Speech-to-Speech exited during the call'))
      })
    })
  } catch (error) { await stop(); throw error }
  return { stop, metadata: {
    implementation: 'huggingface/speech-to-speech', version: '1.1.0',
    llm_model: process.env.TAU_S2S_LLM_MODEL, llm_api: settings.llm_api,
    reasoning_effort: settings.reasoning_effort,
    asr_model: settings.asr_model, tts_model: settings.tts_model,
    asr_provider: settings.asr_model === 'qwen-audio-3.1-asr-flash' ? 'dashscope' : 'mimo',
    voice: settings.voice, language: settings.language,
    streaming_audio_input: false, tts_sentence_batch_size: 1,
    llm_fallback_enabled: Boolean(process.env.TAU_S2S_LLM_FALLBACK_API_KEY),
    asset_manifest_sha256: createHash('sha256').update(readFileSync('/opt/s2s-assets/manifest.json')).digest('hex'),
  } }
}
