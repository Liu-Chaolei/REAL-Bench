// Real Gateway + real ACP + real Qwen Code; only remote model endpoints are fake.
import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { WebSocketServer, WebSocket } from 'ws'

for (const mode of ['realtime', 'mimo', 'qwen']) test(`complete ${mode === 'realtime' ? 'realtime' : `stock HF cascaded (${mode} ASR)`} system delegates over native ACP and returns an MCP result as audio`, {
  skip: !process.env.TAU_QWEN_SYSTEM_TEST && 'Run in the isolated build with model stubs',
  timeout: 120000,
}, async () => {
  const cascade = mode !== 'realtime'
  let businessExecuted = false, consentReply = false, delegated = false
  let asrRequests = 0, ttsRequests = 0, frontendRequests = 0, primaryRequests = 0
  let completions = 0, logs = ''
  const messages = [], calls = []
  const modelRequests = []
  const fake = createServer(async (req, res) => {
    if (req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ object: 'list', data: [{ id: 'qwen-test', object: 'model' }] }))
      return
    }
    const chunks = []
    try {
      for await (const chunk of req) chunks.push(chunk)
    } catch (error) {
      // Barge-in and test shutdown close in-flight native/proxy HTTP uploads.
      if (req.aborted && error.code === 'ECONNRESET') return
      throw error
    }
    const body = JSON.parse(Buffer.concat(chunks).toString())
    if (req.url === '/qwen/api/v1/services/aigc/multimodal-generation/generation') {
      assert.equal(req.headers.authorization, 'Bearer fake-qwen-asr')
      assert.equal(req.headers['api-key'], undefined)
      assert.equal(body.model, 'qwen-audio-3.1-asr-flash')
      assert.equal(body.parameters.sample_rate, '16000')
      assert.ok(body.input.messages[0].content[0].input_audio.data.startsWith('data:audio/wav;base64,'))
      asrRequests++
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ output: { text: consentReply ? 'Yes, go ahead.' : 'Read order 123.' }, usage: { input_tokens: 50, output_tokens: 5 } }))
      return
    }
    if (req.url === '/mimo/chat/completions') {
      assert.equal(req.headers['api-key'], 'fake-mimo')
      let message
      if (body.model === 'mimo-v2.5-asr') {
        asrRequests++
        message = { content: consentReply ? 'Yes, go ahead.' : 'Read order 123.' }
      } else {
        assert.equal(body.model, 'mimo-v2.5-tts')
        ttsRequests++
        const wav = Buffer.alloc(44 + 4800)
        wav.write('RIFF'); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8)
        wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22)
        wav.writeUInt32LE(24000, 24); wav.writeUInt32LE(48000, 28)
        wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34)
        wav.write('data', 36); wav.writeUInt32LE(4800, 40); wav.fill(16, 44)
        message = { audio: { data: wav.toString('base64') } }
      }
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ choices: [{ message }] }))
      return
    }
    if (req.url === '/primary/v1/responses') {
      primaryRequests++
      assert.equal(req.headers.authorization, 'Bearer fake-primary')
      if (!body.stream) {
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ error: { code: 'insufficient_quota', message: 'plan quota exhausted (2056)' } }))
        return
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream' })
      res.write(`data: ${JSON.stringify({ type: 'response.created', response: { id: 'failed-primary', status: 'in_progress', output: [] } })}\n\n`)
      res.end(`data: ${JSON.stringify({ type: 'response.failed', response: { status: 'failed', error: { code: 'insufficient_quota', message: 'plan quota exhausted (2056)' } } })}\n\n`)
      return
    }
    if (req.url === '/v1/responses') {
      assert.equal(req.headers.authorization, 'Bearer fake-frontend')
      frontendRequests++
      if (body.reasoning) assert.equal(body.reasoning.effort, 'low')
      const tools = body.tools || [], can = name => body.tool_choice !== 'none' && tools.some(t => t.name === name)
      let invocation = null
      if (asrRequests && !delegated && can('spawn_thinking')) {
        delegated = true
        invocation = { name: 'spawn_thinking', arguments: { objective: 'Read order 123 using the business tool.' } }
      } else if (asrRequests > 1 && consentReply && can('respond_permission')) {
        consentReply = false
        invocation = { name: 'respond_permission', arguments: { decision: 'task' } }
      }
      const id = `resp_${frontendRequests}`, itemId = `msg_${frontendRequests}`
      const text = businessExecuted ? 'Your order is shipped.' : 'Ready.'
      const item = invocation
        ? { type: 'function_call', id: `fc_${frontendRequests}`, call_id: `call_${frontendRequests}`,
            name: invocation.name, arguments: JSON.stringify(invocation.arguments), status: 'completed' }
        : { type: 'message', id: itemId, role: 'assistant', status: 'completed',
            content: [{ type: 'output_text', text, annotations: [] }] }
      const response = { id, object: 'response', created_at: 1, model: body.model,
        status: 'completed', output: [item], error: null, incomplete_details: null,
        usage: { input_tokens: 10, output_tokens: 10, total_tokens: 20 } }
      if (body.stream) {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' })
        const send = event => res.write(`data: ${JSON.stringify(event)}\n\n`)
        send({ type: 'response.created', response: { ...response, status: 'in_progress', output: [] } })
        send({ type: 'response.output_item.added', output_index: 0, item })
        if (invocation) send({ type: 'response.function_call_arguments.done', item_id: item.id, output_index: 0, arguments: item.arguments })
        else send({ type: 'response.output_text.delta', item_id: itemId, output_index: 0, content_index: 0, delta: text })
        send({ type: 'response.output_item.done', output_index: 0, item })
        send({ type: 'response.completed', response })
        res.end()
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify(response))
      }
      return
    }
    const textOf = value => typeof value === 'string' ? value : Array.isArray(value)
      ? value.map(part => part.text || JSON.stringify(part)).join('\n') : JSON.stringify(value)
    const toolHistory = (body.messages || []).filter(m => m.role === 'tool').map(m => textOf(m.content))
    modelRequests.push({ model: body.model, reasoning_effort: body.reasoning_effort, tools: (body.tools || []).map(t => t.function?.name), lastTool: toolHistory.at(-1) })
    if (modelRequests.length > 5) modelRequests.shift()
    completions++
    const tool = (body.tools || []).find(t => t.function?.name.includes('read_order'))
    const performed = toolHistory.some(text => text.includes('shipped'))
    // Qwen Code keeps MCP declarations deferred. The model stub follows its
    // original search/invoke contract instead of disabling that mechanism.
    const history = toolHistory.join('\n')
    const discovered = [...history.matchAll(/"name"\s*:\s*"([^"]*read_order)"/g)].at(-1)?.[1]
    const has = name => (body.tools || []).some(t => t.function?.name === name)
    const invocation = tool ? { name: tool.function.name, arguments: { order_id: '123' } }
      : discovered && has('tool_call') ? { name: 'tool_call', arguments: { name: discovered, arguments: { order_id: '123' } } }
      : has('tool_search') ? { name: 'tool_search', arguments: { query: 'read_order', max_results: 1 } } : null
    const output = invocation && !performed
      ? { role: 'assistant', content: null, tool_calls: [{ id: `native-${completions}`, type: 'function',
        function: { name: invocation.name, arguments: JSON.stringify(invocation.arguments) } }] }
      : { role: 'assistant', content: 'Your order is shipped.' }
    if (body.stream) {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' })
      const delta = { ...output }
      if (delta.tool_calls) delta.tool_calls = delta.tool_calls.map((call, index) => ({ index, ...call }))
      res.write(`data: ${JSON.stringify({ id: `completion-${completions}`, object: 'chat.completion.chunk', model: 'qwen-test',
        choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`)
      res.write(`data: ${JSON.stringify({ id: `completion-${completions}`, object: 'chat.completion.chunk', model: 'qwen-test',
        choices: [{ index: 0, delta: {}, finish_reason: output.tool_calls ? 'tool_calls' : 'stop' }] })}\n\n`)
      res.end('data: [DONE]\n\n')
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ id: `completion-${completions}`, object: 'chat.completion', model: 'qwen-test',
        choices: [{ index: 0, message: output, finish_reason: output.tool_calls ? 'tool_calls' : 'stop' }],
        usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 } }))
    }
  })
  const realtime = new WebSocketServer({ server: fake })
  let responseCount = 0
  function reply(ws, { name, args, speak = false }) {
    const id = `r${++responseCount}`, callId = `f${responseCount}`
    const send = event => ws.send(JSON.stringify(event))
    send({ type: 'response.created', response: { id, status: 'in_progress', output: [] } })
    if (speak) {
      send({ type: 'response.audio_transcript.delta', response_id: id, item_id: id, delta: 'Your order is shipped.' })
      send({ type: 'response.audio.delta', response_id: id, item_id: id,
        delta: Buffer.alloc(960, 16).toString('base64') })
      send({ type: 'response.audio.done', response_id: id, item_id: id })
      send({ type: 'response.audio_transcript.done', response_id: id, item_id: id, transcript: 'Your order is shipped.' })
      send({ type: 'response.done', response: { id, status: 'completed', output: [] } })
    } else {
      const item = { id: callId, type: 'function_call', call_id: callId, name, arguments: JSON.stringify(args) }
      send({ type: 'response.output_item.added', response_id: id, item })
      send({ ...item, type: 'response.function_call_arguments.done', response_id: id })
      send({ type: 'response.output_item.done', response_id: id, item })
      send({ type: 'response.done', response: { id, status: 'completed', output: [item] } })
    }
  }
  realtime.on('connection', ws => {
    ws.send(JSON.stringify({ type: 'session.created', session: { id: 'fake-frontend' } }))
    ws.on('message', raw => {
      const message = JSON.parse(raw)
      if (message.type === 'session.update') ws.send(JSON.stringify({ type: 'session.updated', session: message.session }))
      if (message.type === 'conversation.item.create') ws.send(JSON.stringify({ type: 'conversation.item.created', item: message.item }))
      if (message.type === 'input_audio_buffer.append' && (!delegated || consentReply)) {
        const consent = consentReply
        delegated = true; consentReply = false
        ws.send(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: `u${responseCount}` }))
        ws.send(JSON.stringify({ type: 'input_audio_buffer.speech_stopped', item_id: `u${responseCount}` }))
        ws.send(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed',
          item_id: `u${responseCount}`, transcript: consent ? 'Yes, go ahead.' : 'Read order 123.' }))
        reply(ws, consent ? { name: 'respond_permission', args: { decision: 'task' } }
          : { name: 'spawn_thinking', args: { objective: 'Read order 123 using the business tool.' } })
      }
      if (message.type === 'response.create') {
        if (businessExecuted) reply(ws, { speak: true })
        else {
          const id = `r${++responseCount}`
          ws.send(JSON.stringify({ type: 'response.created', response: { id, status: 'in_progress', output: [] } }))
          ws.send(JSON.stringify({ type: 'response.done', response: { id, status: 'completed', output: [] } }))
        }
      }
    })
  })
  fake.listen(0, '127.0.0.1')
  await once(fake, 'listening')
  const endpoint = `127.0.0.1:${fake.address().port}`
  const controlPort = 18787
  const child = spawn(process.execPath, ['bootstrap.mjs'], {
    cwd: '/opt/tau-bridge',
    env: { ...process.env, TAU_BRIDGE_PORT: String(controlPort), TAU_BRIDGE_TOKEN: 'contract-token',
      QWEN_AUDIO_REALTIME_API_KEY: 'fake-key', QWEN_AUDIO_REALTIME_MODEL: 'qwen-audio-3.1-realtime-plus',
      QWEN_AUDIO_REALTIME_URL: `ws://${endpoint}/realtime`,
      OPENAI_API_KEY: 'fake-key', OPENAI_BASE_URL: `http://${endpoint}/v1`, OPENAI_MODEL: 'qwen-test', QWEN_CODE_REASONING_EFFORT: 'low',
      ...(cascade ? { QWEN_AUDIO_REALTIME_PROVIDER: 'speech-to-speech',
        QWEN_AUDIO_REALTIME_URL: 'ws://127.0.0.1:8765/v1/realtime', QWEN_AUDIO_REALTIME_MODEL: '', QWEN_AUDIO_REALTIME_API_KEY: '',
        TAU_S2S_CONFIG: JSON.stringify({ llm_api: 'responses-api', reasoning_effort: 'low', mimo_base_url: `http://${endpoint}/mimo`,
          asr_model: mode === 'qwen' ? 'qwen-audio-3.1-asr-flash' : 'mimo-v2.5-asr',
          tts_model: 'mimo-v2.5-tts', voice: 'Dean', language: 'en', request_timeout_seconds: 10 }),
        TAU_S2S_LLM_API_KEY: 'fake-primary', TAU_S2S_LLM_BASE_URL: `http://${endpoint}/primary/v1`,
        TAU_S2S_LLM_FALLBACK_API_KEY: 'fake-frontend', TAU_S2S_LLM_FALLBACK_BASE_URL: `http://${endpoint}/v1`,
        TAU_S2S_LLM_MODEL: 'frontend-test', TAU_S2S_MIMO_API_KEY: 'fake-mimo',
        ...(mode === 'qwen' ? { TAU_S2S_ASR_API_KEY: 'fake-qwen-asr',
          TAU_S2S_ASR_BASE_URL: `http://${endpoint}/qwen/api-ws/v1/realtime` } : {}),
      } : {}),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.stdout.on('data', data => { logs += data })
  child.stderr.on('data', data => { logs += data })
  const until = async predicate => {
    const limit = Date.now() + 60000
    while (!predicate()) {
      if (child.exitCode !== null || Date.now() > limit) throw new Error(`System contract failed: Models=${JSON.stringify(modelRequests)} Tasks=${JSON.stringify(messages.filter(m => m.event?.type.startsWith('task.')))} ${logs.slice(0,3000)} ${logs.slice(-3000)}`)
      await new Promise(resolve => setTimeout(resolve, 20))
    }
  }
  let control, interval, source, offset = 0
  const consentAudio = cascade ? readFileSync('/opt/tau-bridge/test/fixtures/consent.pcm') : null
  if (cascade) source = readFileSync('/opt/tau-bridge/test/fixtures/order.pcm')
  try {
    // Wait for the child to bind without requiring a second application session.
    await new Promise((resolve, reject) => {
      const deadline = Date.now() + 10000
      const connect = () => {
        const ws = new WebSocket(`ws://127.0.0.1:${controlPort}/control`, { headers: { Authorization: 'Bearer contract-token' } })
        ws.once('error', error => Date.now() < deadline && child.exitCode === null ? setTimeout(connect, 50) : reject(new Error(`${error.message}: ${logs}`)))
        ws.once('open', () => { control = ws; resolve() })
      }
      connect()
    })
    control.on('message', raw => {
      const message = JSON.parse(raw)
      messages.push(message)
      if (message.type === 'tool_call') {
        calls.push(message)
        businessExecuted = true
        control.send(JSON.stringify({ type: 'tool_result', call_id: message.call_id,
          content: '{"status":"shipped"}', is_error: false }))
      }
      if (message.type === 'gateway_event' && message.event.type === 'task.permission.requested') {
        consentReply = true
        if (cascade) { source = consentAudio; offset = 0 }
      }
    })
    control.send(JSON.stringify({ type: 'initialize', protocol: 1, session_id: 'full-system-test', diagnostics_enabled: true,
      policy: 'For an order status request, use read_order and report the returned status. Never invent it.',
      tool_timeout_ms: 30000,
      tools: [{ name: 'read_order', description: 'Read the order status', parameters: {
        type: 'object', properties: { order_id: { type: 'string' } }, required: ['order_id'],
      } }],
    }))
    await until(() => messages.some(m => m.type === 'ready' || m.type === 'error'))
    assert.ok(messages.some(m => m.type === 'ready'), JSON.stringify(messages))
    interval = setInterval(() => {
      const pcm = Buffer.alloc(640)
      if (cascade && offset < source.length) {
        source.copy(pcm, 0, offset, offset + 640); offset += 640
      }
      control.send(JSON.stringify({ type: 'input_audio', audio: pcm.toString('base64') }))
    }, 20)
    await until(() => businessExecuted || messages.some(m => m.type === 'error'))
    assert.ok(businessExecuted, `No business operation: ${JSON.stringify(messages)} Models: ${JSON.stringify(modelRequests)} ${logs.slice(-8000)}`)
    await until(() => messages.some(m => m.type === 'audio'))
    assert.ok(messages.some(m => m.type === 'diagnostic_backend_ready'))
    assert.ok(messages.some(m => m.type === 'diagnostic_backend' && m.role === 'prompt' && !m.internal && m.content))
    assert.equal(calls.length, 1)
    assert.equal(calls[0].name, 'read_order')
    assert.deepEqual(calls[0].arguments, { order_id: '123' })
    assert.equal(messages.find(m => m.type === 'ready').metadata.sandbox.mechanism, 'mount_namespace')
    assert.ok(modelRequests.some(m => m.reasoning_effort === 'low'), 'Native request must retain reasoning effort')
    assert.ok(modelRequests.every(m => m.model === 'qwen-test'))
    assert.ok(completions > 0, 'The native Qwen Code backend must call its model')
    if (cascade) {
      assert.ok(asrRequests > 1 && ttsRequests > 1 && frontendRequests > 1)
      assert.equal(primaryRequests, 1, 'The call must stay on the backup after primary quota exhaustion')
      assert.equal(messages.find(m => m.type === 'ready').metadata.speech_to_speech.llm_fallback_enabled, true)
      assert.equal(messages.find(m => m.type === 'ready').metadata.speech_to_speech.version, '1.1.0')
      assert.equal(messages.find(m => m.type === 'ready').metadata.speech_to_speech.asr_provider, mode === 'qwen' ? 'dashscope' : 'mimo')
      assert.equal(messages.find(m => m.type === 'ready').metadata.frontend_model, 'frontend-test')
    }
  } finally {
    clearInterval(interval)
    control?.close()
    child.kill('SIGTERM')
    const kill = setTimeout(() => child.kill('SIGKILL'), 3000)
    if (child.exitCode === null && child.signalCode === null) await once(child, 'exit').catch(() => {})
    clearTimeout(kill)
    for (const ws of realtime.clients) ws.terminate()
    await new Promise(resolve => realtime.close(resolve))
    await new Promise(resolve => fake.close(resolve))
  }
})
