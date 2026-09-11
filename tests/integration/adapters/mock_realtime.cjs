// Deterministic frontend boundary for offline transport tests, not a benchmark model.
const { WebSocketServer } = require('/opt/qwen/node_modules/ws')
const { readFileSync } = require('node:fs')
const { createHash } = require('node:crypto')
const wav = readFileSync('/evaluation/input.wav')
let data
for (let offset = 12; offset + 8 <= wav.length;) {
  const size = wav.readUInt32LE(offset + 4)
  if (wav.toString('ascii', offset, offset + 4) === 'data') {
    data = wav.subarray(offset + 8, offset + 8 + size)
    break
  }
  offset += 8 + size + (size % 2)
}
if (!data?.length) throw new Error('Missing input PCM')
const expectedHash = createHash('sha256').update(data).digest('hex')
let inputVerified = false
let outageInjected = false
let connectionCount = 0
const wss = new WebSocketServer({ port: 8199, host: '127.0.0.1' })
wss.on('connection', ws => {
  const connectionNumber = ++connectionCount
  let pcm = Buffer.alloc(0)
  let answered = false
  const send = event => ws.send(JSON.stringify(event))
  send({ type: 'session.created', session: { id: 'mock-session' } })
  ws.on('message', raw => {
    const event = JSON.parse(String(raw))
    if (event.type === 'session.update') {
      send({ type: 'session.updated', session: { id: 'mock-session' } })
    }
    if (event.type === 'conversation.item.create') {
      send({ type: 'conversation.item.created', item: event.item })
      return
    }
    if (event.type === 'response.create' && connectionNumber > 1 && inputVerified) {
    send({ type: 'response.created', response: { id: 'reply-1', status: 'in_progress' } })
    send({ type: 'response.audio.delta', response_id: 'reply-1', delta: Buffer.alloc(9600).toString('base64') })
    send({ type: 'response.audio_transcript.done', response_id: 'reply-1', transcript: 'Audio transport verified.' })
    send({ type: 'response.audio.done', response_id: 'reply-1' })
    send({ type: 'response.done', response: { id: 'reply-1', status: 'completed', output: [] } })
      return
    }
    if (event.type !== 'input_audio_buffer.append' || answered) return
    if (!pcm.length) send({ type: 'input_audio_buffer.speech_started', item_id: 'input-1' })
    pcm = Buffer.concat([pcm, Buffer.from(event.audio, 'base64')])
    if (pcm.length < data.length + 32000) return // one second VAD tail
    answered = true
    inputVerified = true
    if (createHash('sha256').update(pcm.subarray(0, data.length)).digest('hex') !== expectedHash) {
      send({ type: 'error', error: { message: 'Input audio changed in transit' } })
      return
    }
    send({ type: 'input_audio_buffer.speech_stopped', item_id: 'input-1' })
    send({ type: 'input_audio_buffer.committed', item_id: 'input-1' })
    send({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'input-1', transcript: 'Mock recognition of the provided WAV.' })
    if (!outageInjected) {
      outageInjected = true
      setTimeout(() => {
        // Deliberately omit a WebSocket close frame: recovery must close it itself.
        send({ type: 'error', error: { code: 'user_idle_timeout', message: 'Your session was closed because no user input was received for 180 seconds.' } })
      }, 2500)
    }
  })
})
