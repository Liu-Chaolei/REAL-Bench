// The bridge exposes a standard, stateless Streamable HTTP MCP server.
// It transports requests; only tau's orchestrator executes business operations.
import { randomUUID } from 'node:crypto'

export class ToolBroker {
  constructor({ tools, policy, send, timeoutMs, onFailure = () => {} }) {
    this.tools = new Map(tools.map(tool => [tool.name, tool]))
    if (this.tools.size !== tools.length) throw new Error('Duplicate tool names')
    this.policy = policy
    this.send = send
    this.timeoutMs = timeoutMs
    this.onFailure = onFailure
    this.pending = new Map()
    this.closed = false
    this.discovered = false
    this.connected = false
  }

  async rpc(message) {
    if (this.closed) throw new Error('Business environment is closed')
    switch (message.method) {
      case 'initialize':
        this.connected = true
        return {
          protocolVersion: '2025-03-26',
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: 'tau', version: '1.0.0' },
          instructions: this.policy,
        }
      case 'ping': return {}
      case 'tools/list':
        this.discovered = true
        return { tools: [...this.tools.values()].map(tool => ({
          name: tool.name, description: tool.description, inputSchema: tool.parameters,
        })) }
      case 'tools/call': return this.call(message.params)
      default: throw new Error(`Unsupported MCP method: ${message.method}`)
    }
  }

  call({ name, arguments: args = {} } = {}) {
    if (this.closed) return Promise.reject(new Error('Business environment is closed'))
    if (!this.tools.has(name)) return Promise.reject(new Error('Unknown business tool'))
    const callId = randomUUID()
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(callId)
        const error = new Error('Business tool timed out; its outcome must not be retried')
        reject(error)
        this.onFailure(error)
      }, this.timeoutMs)
      this.pending.set(callId, { resolve, reject, timer })
      try { this.send({ type: 'tool_call', call_id: callId, name, arguments: args }) }
      catch (error) { clearTimeout(timer); this.pending.delete(callId); reject(error) }
    })
  }

  complete({ call_id: callId, content, is_error: isError }) {
    const pending = this.pending.get(callId)
    if (!pending) throw new Error('Unknown or already completed tool request')
    clearTimeout(pending.timer)
    this.pending.delete(callId)
    pending.resolve({ content: [{ type: 'text', text: String(content) }], isError: Boolean(isError) })
  }

  close() {
    this.closed = true
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer)
      reject(new Error('Business environment closed'))
    }
    this.pending.clear()
  }
}

export function gatewayMedia(event) {
  const responseId = event.responseId || event.response_id
  switch (event.type) {
    case 'audio.delta':
    case 'response.output_audio.delta':
      return { type: 'audio', response_id: responseId,
        sample_rate: event.sampleRate || event.sample_rate, audio: event.audio || event.delta }
    case 'audio.done':
    case 'response.output_audio.done':
      return { type: 'audio_done', response_id: responseId }
    case 'playback.clear':
    case 'response.interrupted':
      return { type: 'clear', ...(responseId ? { response_id: responseId } : {}) }
    default: return { type: 'gateway_event', event }
  }
}

export function composePolicy(originalPrompt, policy) {
  // Keep Qwen's original routing, task, permission and voice rules byte-for-byte.
  const combined = `${originalPrompt}\n\n# Evaluation business environment\n\n`
    + 'Your existing Qwen Code backend has an additional MCP server named tau exposing the business tools referenced by this policy. '
    + 'Serve the caller in English. The following policy governs the simulated application environment. '
    + 'Its scenario dates and authoritative clock take precedence over the host computer date.\n\n'
    + policy
  if ([...combined].length > 16000) throw new Error('Full policy exceeds Qwen prompt limit')
  return combined
}

export async function serveMcp(request, response, broker) {
  if (request.method !== 'POST') { response.writeHead(405); response.end(); return }
  let message
  try {
    const chunks = []
    let size = 0
    for await (const chunk of request) {
      size += chunk.length
      if (size > 16 * 1024 * 1024) throw new Error('MCP request too large')
      chunks.push(chunk)
    }
    message = JSON.parse(Buffer.concat(chunks).toString())
    if (message.jsonrpc !== '2.0' || typeof message.method !== 'string') throw new Error('Invalid JSON-RPC')
    if (message.id === undefined) { response.writeHead(202); response.end(); return }
    const result = await broker.rpc(message)
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }))
  } catch (error) {
    if (response.destroyed) return
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ jsonrpc: '2.0', id: message?.id ?? null,
      error: { code: -32603, message: error.message } }))
  }
}

export function gatewayFailure(event) {
  if (event.type !== 'voice.connection' || !['unavailable', 'blocked'].includes(event.state)) return null
  // The original Agent may recover a temporary voice interruption itself.
  // Explicitly blocked failures carry a message; no adapter replay is added.
  if (event.state === 'unavailable' && !event.message) return null
  const code = String(event.message || '').match(/(?:HTTP|response:)\s*(\d{3})/i)?.[1]
  return new Error(`Agent realtime voice connection failed${code ? ` (HTTP ${code})` : ''}; inspect the private system trace`)
}

/** Passive evidence at native BackendPort and ACP request boundaries. */
export function observeBackend(backend, emit) {
  const pending = new Map()
  const safe = event => { try { emit(event) } catch { /* observer never changes work */ } }
  const coordinator = sessionId => [...backend.coordinatorSessions.values()]
    .some(session => session.sessionId === sessionId) || backend.client.sessions?.get(sessionId)?.role === 'utility'
  for (const method of ['submit', 'update']) {
    const original = backend[method]?.bind(backend)
    if (!original) continue
    backend[method] = (...args) => {
      safe({ type: 'diagnostic_backend', id: randomUUID(), role: method,
        content: args[0], time: performance.now() / 1000, accepted: null,
        task_id: method === 'submit' ? args[0]?.id : args[0] })
      return original(...args)
    }
  }
  const request = backend.client.request.bind(backend.client)
  backend.client.request = (method, params, options) => {
    if (!['session/prompt', '_session/steering'].includes(method)) return request(method, params, options)
    const id = randomUUID()
    const event = { type: 'diagnostic_backend', id, role: method === 'session/prompt' ? 'prompt' : 'steer',
      session_id: params.sessionId, content: params.prompt, time: performance.now() / 1000,
      internal: !coordinator(params.sessionId), accepted: null }
    pending.set(params.sessionId, event)
    safe(event)
    const promise = request(method, params, options)
    promise.then(() => {
      safe({ ...event, accepted: true })
      if (pending.get(params.sessionId) === event) pending.delete(params.sessionId)
    }, () => {
      safe({ ...event, accepted: event.accepted === true ? true : false })
      if (pending.get(params.sessionId) === event) pending.delete(params.sessionId)
    })
    return promise
  }
  const update = backend.client.handleUpdate.bind(backend.client)
  backend.client.handleUpdate = notification => {
    const event = pending.get(notification?.sessionId)
    if (event && event.accepted !== true) {
      event.accepted = true
      safe({ ...event })
    }
    return update(notification)
  }
  const drain = backend.client.drainMidTurnQueue.bind(backend.client)
  backend.client.drainMidTurnQueue = params => {
    const result = drain(params)
    for (const item of result?.items || []) safe({ type: 'diagnostic_backend',
      id: randomUUID(), role: 'steer', session_id: params.sessionId,
      content: item.content, time: performance.now() / 1000,
      accepted: true, internal: !coordinator(params.sessionId) })
    return result
  }
  safe({ type: 'diagnostic_backend_ready', version: 1 })
}
