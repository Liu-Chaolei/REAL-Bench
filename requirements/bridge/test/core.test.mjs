import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { ToolBroker, composePolicy, gatewayMedia, gatewayFailure, serveMcp } from '../core.mjs'

const tools = [{ name: 'cancel_order', description: 'Cancel', parameters: {
  type: 'object', properties: { order_id: { type: 'string' } }, required: ['order_id'],
} }]

test('MCP executes through the evaluator exactly once and preserves errors', async () => {
  const sent = []
  const broker = new ToolBroker({ tools, policy: 'official policy', send: e => sent.push(e), timeoutMs: 1000 })
  const pending = broker.rpc({ method: 'tools/call', params: { name: 'cancel_order', arguments: { order_id: '1' } } })
  assert.equal(sent.length, 1)
  assert.equal(broker.pending.size, 1)
  assert.deepEqual(sent[0].arguments, { order_id: '1' })
  broker.complete({ call_id: sent[0].call_id, content: 'not allowed', is_error: true })
  assert.deepEqual(await pending, { content: [{ type: 'text', text: 'not allowed' }], isError: true })
  assert.throws(() => broker.complete({ call_id: sent[0].call_id }), /already completed/)
  assert.equal(sent.length, 1)
  broker.close()
})

test('waiting tools do not prevent media processing; shutdown rejects pending work', async () => {
  const broker = new ToolBroker({ tools, policy: '', send() {}, timeoutMs: 1000 })
  const pending = broker.call({ name: 'cancel_order' })
  const rejection = assert.rejects(pending, /environment closed/)
  assert.deepEqual(gatewayMedia({ type: 'audio.delta', responseId: 'r', sampleRate: 24000, audio: 'AA==' }),
    { type: 'audio', response_id: 'r', sample_rate: 24000, audio: 'AA==' })
  broker.close()
  await rejection
  await assert.rejects(broker.call({ name: 'cancel_order' }), /closed/)
})

test('timeouts are infrastructure failures, never automatic retries', async () => {
  const sent = [], failures = []
  const broker = new ToolBroker({ tools, policy: '', send: e => sent.push(e), timeoutMs: 5,
    onFailure: e => failures.push(e) })
  await assert.rejects(broker.call({ name: 'cancel_order' }), /must not be retried/)
  assert.equal(sent.length, 1)
  assert.equal(failures.length, 1)
  broker.close()
})

test('public MCP discovery preserves the tool schema and policy', async () => {
  const broker = new ToolBroker({ tools, policy: 'business policy', send() {}, timeoutMs: 1000 })
  const server = createServer((req, res) => serveMcp(req, res, broker))
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  try {
    const call = async (method, id = 1) => {
      const response = await fetch(`http://127.0.0.1:${server.address().port}/mcp`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
        body: JSON.stringify({ jsonrpc: '2.0', id, method }),
      })
      return response.json()
    }
    assert.equal((await call('initialize')).result.instructions, 'business policy')
    assert.deepEqual((await call('tools/list')).result.tools[0].inputSchema, tools[0].parameters)
    assert.equal((await call('not-supported')).error.code, -32603)
  } finally { broker.close(); await new Promise(resolve => server.close(resolve)) }
})

test('the original routing prompt is retained; policies cannot be truncated', () => {
  const original = 'Original routing, permission and task handling.'
  const prompt = composePolicy(original, 'Complete retail policy.')
  assert.ok(prompt.startsWith(original))
  assert.ok(prompt.endsWith('Complete retail policy.'))
  assert.throws(() => composePolicy(original, 'a'.repeat(16000)), /limit/)
})

test('native task and transcript events remain diagnostics, not business calls', () => {
  const event = { type: 'task.completed', task: { content: 'internal answer' } }
  assert.deepEqual(gatewayMedia(event), { type: 'gateway_event', event })
  assert.deepEqual(gatewayMedia({ type: 'playback.clear' }), { type: 'clear' })
})

test('fatal realtime authentication fails boot promptly without copying provider messages', () => {
  const failure = gatewayFailure({ type: 'voice.connection', state: 'unavailable',
    message: 'Unexpected server response: 401 synthetic-secret' })
  assert.match(failure.message, /HTTP 401/)
  assert.ok(!failure.message.includes('synthetic-secret'))
  assert.equal(gatewayFailure({ type: 'voice.connection', state: 'connecting' }), null)
  assert.equal(gatewayFailure({ type: 'voice.connection', state: 'unavailable' }), null)
  const recoverable = { type: 'error', message: 'The Agent is recovering a response' }
  assert.equal(gatewayFailure(recoverable), null)
  assert.deepEqual(gatewayMedia(recoverable), { type: 'gateway_event', event: recoverable })
})

test('backend diagnostics preserve native promises, inputs and child isolation', async () => {
  const { observeBackend } = await import('../core.mjs')
  const events = []
  let resolve
  const promise = new Promise(r => { resolve = r })
  const calls = []
  const backend = {
    coordinatorSessions: new Map([['owner', { sessionId: 'coordinator' }]]),
    submit: (...args) => { calls.push(args); return promise },
    update: (...args) => { calls.push(args); return promise },
    client: {
      request: (...args) => { calls.push(args); return promise },
      handleUpdate: value => value,
      drainMidTurnQueue: () => ({ items: [{ content: 'later change' }] }),
    },
  }
  observeBackend(backend, e => events.push(structuredClone(e)))
  const params = { sessionId: 'coordinator', prompt: ['complete original input'] }
  assert.equal(backend.client.request('session/prompt', params, {}), promise)
  assert.equal(calls[0][1], params)
  backend.client.handleUpdate({ sessionId: 'coordinator' })
  assert.equal(events.at(-1).accepted, true)
  backend.client.request('session/prompt', { sessionId: 'child', prompt: ['subtask'] }, {})
  assert.equal(events.at(-1).internal, true)
  resolve('original result')
  assert.equal(await promise, 'original result')
  backend.client.drainMidTurnQueue({ sessionId: 'coordinator' })
  assert.equal(events.at(-1).content, 'later change')
  assert.equal(events.at(-1).accepted, true)
})
