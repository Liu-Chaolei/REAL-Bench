// Built-image contracts against the archived, unmodified Qwen revision.
import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { ToolBroker, serveMcp, gatewayMedia } from '../core.mjs'

const root = process.env.TAU_QWEN_TEST_ROOT
const options = { skip: !root && 'Set TAU_QWEN_TEST_ROOT to an installed, unmodified Qwen checkout' }

test('native Qwen Code connects to user-configured business MCP tools', {
  skip: !process.env.TAU_QWEN_CODE_TEST && 'Run inside the built image with native Qwen Code',
}, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tau-native-mcp-'))
  const broker = new ToolBroker({ tools: [{ name: 'read_order', description: 'Read',
    parameters: { type: 'object', properties: {} } }], policy: 'Public policy', send() {}, timeoutMs: 1000 })
  const server = createServer((request, response) => serveMcp(request, response, broker))
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const qwenHome = join(directory, 'qwen')
  const settingsPath = join(qwenHome, 'settings.json')
  try {
    await mkdir(qwenHome)
    await writeFile(settingsPath, JSON.stringify({
      mcpServers: { tau: { httpUrl: `http://127.0.0.1:${server.address().port}/mcp` } },
    }))
    const output = await promisify(execFile)('qwen', ['mcp', 'list'], { cwd: directory, timeout: 30000, env: { ...process.env, QWEN_HOME: qwenHome, QWEN_RUNTIME_DIR: join(directory, 'runtime') } })
    assert.equal(broker.connected, true, `Native Qwen Code must connect to MCP: ${output.stdout} ${output.stderr}`)
  } finally {
    broker.close()
    await new Promise(resolve => server.close(resolve))
    await rm(directory, { recursive: true, force: true })
  }
})

test('official MCP client discovers and calls the bridge', options, async () => {
  const require = createRequire(`${root}/package.json`)
  const { Client } = await import(require.resolve('@modelcontextprotocol/sdk/client/index.js'))
  const { StreamableHTTPClientTransport } = await import(require.resolve('@modelcontextprotocol/sdk/client/streamableHttp.js'))
  const calls = []
  let broker
  broker = new ToolBroker({
    tools: [{ name: 'read_order', description: 'Read', parameters: {
      type: 'object', properties: { order_id: { type: 'string' } }, required: ['order_id'],
    } }],
    policy: 'Test public policy', timeoutMs: 1000,
    send(event) {
      calls.push(event)
      setImmediate(() => broker.complete({ call_id: event.call_id, content: 'Shipped', is_error: false }))
    },
  })
  const server = createServer((request, response) => serveMcp(request, response, broker))
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const client = new Client({ name: 'tau-contract', version: '1.0.0' })
  try {
    await client.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${server.address().port}/mcp`)))
    assert.equal((await client.listTools()).tools[0].name, 'read_order')
    const result = await client.callTool({ name: 'read_order', arguments: { order_id: '1' } })
    assert.equal(result.content[0].text, 'Shipped')
    assert.equal(calls.length, 1)
    assert.equal(broker.discovered, true)
  } finally {
    await client.close()
    broker.close()
    await new Promise(resolve => server.close(resolve))
  }
})

test('native Qwen GCP SDK and parser accept bridge audio and playback', options, async () => {
  const require = createRequire(`${root}/package.json`)
  const { WebSocketServer, WebSocket } = require('ws')
  const { GatewayClient } = await import(pathToFileURL(`${root}/shared/gateway/client-sdk.mjs`))
  const { GatewayClientProtocolSession } = await import(pathToFileURL(`${root}/server/src/transport/gateway-client-protocol-session.mjs`))
  const server = createServer()
  const wss = new WebSocketServer({ server })
  const received = [], media = []
  const ready = Promise.withResolvers(), played = Promise.withResolvers()
  wss.on('connection', ws => {
    const protocol = new GatewayClientProtocolSession({ sessionId: 'contract' })
    ws.on('message', raw => {
      const outcome = protocol.receive(JSON.parse(raw))
      if (outcome.reply) ws.send(JSON.stringify(outcome.reply))
      const event = outcome.event
      if (!event) return
      received.push(event)
      if (event.type === 'connect') ws.send(JSON.stringify(protocol.encode({
        type: 'voice.ready', inputSampleRate: 16000, provider: 'test',
      })))
      if (event.type === 'audio.append') ws.send(JSON.stringify(protocol.encode({
        type: 'audio.delta', responseId: 'r', sampleRate: 24000, audio: 'EBAQEA==',
      })))
      if (event.type === 'playback.ended') played.resolve()
    })
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const client = new GatewayClient({
    url: `ws://127.0.0.1:${server.address().port}`, createSocket: url => new WebSocket(url),
    clientType: 'tau-voice', reconnect: false,
    capabilities: ['input.audio', 'playback.receipts', 'permissions.respond', 'tasks.input.respond', 'session.heartbeat'],
    configure: { voiceEnabled: true, inputEnabled: true, outputEnabled: true, textOnly: false },
    onEvent(event) {
      if (event.type === 'voice.ready') ready.resolve()
      else if (event.type === 'audio.delta') {
        media.push(gatewayMedia(event))
        client.send({ type: 'playback.started', responseId: 'r' })
        client.send({ type: 'playback.ended', responseId: 'r' })
      }
    },
  })
  const timers = []
  const within = promise => Promise.race([promise, new Promise((_, reject) => {
    timers.push(setTimeout(() => reject(new Error('GCP contract timed out')), 2000))
  })])
  try {
    client.start()
    await within(ready.promise)
    assert.equal(client.send({ type: 'input_audio_buffer.append', audio: 'AAA=' }), true)
    await within(played.promise)
    assert.equal(media[0].response_id, 'r')
    assert.equal(received[0].textOnly, false)
    assert.equal(received.filter(e => e.type === 'audio.append').length, 1)
  } finally {
    timers.forEach(clearTimeout)
    client.stop()
    for (const ws of wss.clients) ws.terminate()
    await new Promise(resolve => wss.close(resolve))
    await new Promise(resolve => server.close(resolve))
  }
})
