// Embeds the Gateway using its native ACP backend, clock injection and GCP SDK.
import { createServer } from 'node:http'
import { once } from 'node:events'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { randomUUID, createHash, timingSafeEqual } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { pathToFileURL } from 'node:url'
import { WebSocketServer, WebSocket } from 'ws'
import { ToolBroker, composePolicy, gatewayMedia, gatewayFailure, serveMcp, observeBackend } from './core.mjs'
import { startSpeechToSpeech } from './speech-runtime.mjs'
import { observePersistentTasks } from './persistent-observer.mjs'
import { createScenarioTimeProvider, SCENARIO_TIME_TOOL, SCENARIO_TIME_POLICY } from './scenario-time.mjs'

const port = Number(process.env.TAU_BRIDGE_PORT || 8787)
const root = process.env.TAU_QWEN_ROOT || '/opt/qwen-audio-agent'
const trial = process.env.TAU_TRIAL_ROOT
if (!trial) throw new Error('Start through the confined launcher')
const workspace = `${trial}/workspace`
const qwen = '/opt/tau-bridge/qwen-wrapper'
const token = process.env.TAU_BRIDGE_TOKEN
if (!token) throw new Error('TAU_BRIDGE_TOKEN is required')
const mcpPath = `/mcp/${randomUUID()}`
let controller, broker, application, gateway, client, initialized = false, closing = false
let speech
let stopPersistentObserver

function send(event) {
  if (controller?.readyState !== WebSocket.OPEN) throw new Error('Evaluator disconnected')
  controller.send(JSON.stringify(event))
}
function fail(error) {
  try { send({ type: 'error', message: error.message }) } catch { /* socket already closed */ }
}
async function close() {
  if (closing) return
  closing = true
  stopPersistentObserver?.()
  broker?.close()
  client?.stop()
  await application?.close()
  await application?.services.agent.close()
  gateway?.close()
  await speech?.stop()
}
async function initialize(message) {
  if (initialized || message.protocol !== 1) throw new Error('Invalid bridge initialization')
  initialized = true
  if (!message.policy || !Array.isArray(message.tools)) throw new Error('Policy and tools are required')
  const scenarioTimeEnabled = message.tools.some(tool => tool.name === SCENARIO_TIME_TOOL)
  const policy = message.policy + (scenarioTimeEnabled ? `\n\n${SCENARIO_TIME_POLICY}\n` : '')
  broker = new ToolBroker({ tools: message.tools, policy, send,
    timeoutMs: message.tool_timeout_ms, onFailure: fail })
  mkdirSync(`${trial}/prompts`, { recursive: true })
  mkdirSync(`${trial}/config`, { recursive: true })
  // Extend the immutable Qwen catalog with the DashScope model selected by tau.
  // The gateway merges this file with its built-in profiles at startup.
  writeFileSync(`${trial}/config/frontend-models.json`, JSON.stringify({
    providers: {
      dashscope: {
        models: {
          'qwen-audio-3.1-realtime-flash': {
            api: 'audio',
            label: 'Qwen Audio 3.1 Realtime Flash',
            voice: 'longanqian_v3.1',
          },
        },
      },
    },
  }))
  mkdirSync(`${workspace}/.qwen`, { recursive: true })
  const original = readFileSync(`${root}/config/frontend-agent/PROMPT.md`, 'utf8')
  const combined = composePolicy(original, policy)
  writeFileSync(`${trial}/prompts/PROMPT.md`, combined)
  writeFileSync(`${trial}/prompts/ASSISTANT.md`, readFileSync(`${root}/config/frontend-agent/ASSISTANT.md`))
  writeFileSync(`${workspace}/QWEN.md`, policy)
  const settings = {
    security: { auth: { selectedType: 'openai' } },
    model: { name: process.env.OPENAI_MODEL,
      ...(process.env.QWEN_CODE_REASONING_EFFORT ? { generationConfig: {
        reasoning: { effort: process.env.QWEN_CODE_REASONING_EFFORT },
        extra_body: { reasoning_effort: process.env.QWEN_CODE_REASONING_EFFORT },
      } } : {}),
    },
    mcpServers: { tau: { httpUrl: `http://127.0.0.1:${port}${mcpPath}` } },
  }
  // Install the business environment as normal user-scoped native MCP config.
  // Project-scoped servers require a separate configuration-approval ceremony;
  // that is deployment setup, not a business decision for the simulated caller.
  const userQwen = `${trial}/qwen`
  mkdirSync(userQwen, { recursive: true })
  writeFileSync(`${userQwen}/settings.json`, JSON.stringify(settings), { mode: 0o600 })
  Object.assign(process.env, {
    QWAUDIO_CONFIG_DIR: `${trial}/config`, QWAUDIO_DATA_DIR: `${trial}/data`,
    QWAUDIO_STATE_DIR: `${trial}/state`, QWAUDIO_CACHE_DIR: `${trial}/cache`,
    QWEN_CODE_WORKSPACE: workspace, AGENT_PROTOCOL: 'qwen', QWEN_CODE_BIN: qwen,
    QWEN_AUDIO_AGENT_BACKEND_PERMISSION_MODE: 'native',
    QWEN_AUDIO_AGENT_FRONTEND_PROMPT_DIR: `${trial}/prompts`,
  })
  // Native discovery runs after confinement, without touching shared home.
  await promisify(execFile)(qwen, ['mcp', 'list'], { cwd: workspace, timeout: 30000 })
  if (!broker.connected && message.tools.length) throw new Error('Qwen Code could not connect to the business MCP server')
  speech = await startSpeechToSpeech({ onFailure: fail,
    timeoutMs: Number(process.env.TAU_S2S_BOOT_TIMEOUT_MS || 120000) })
  // Set all deployment configuration BEFORE importing Qwen's config singleton.
  const { createGatewayApplication, supportsCurrentTimeSnapshotProvider } = await import(pathToFileURL(`${root}/server/src/app/gateway-application.mjs`))
  let getCurrentTimeSnapshot
  if (scenarioTimeEnabled) {
    if (supportsCurrentTimeSnapshotProvider !== true) {
      throw new Error('This Qwen image does not support scenario time injection; rebuild with the clock provider interface')
    }
    const { currentTimeSnapshot } = await import(pathToFileURL(`${root}/server/src/conversation/frontend-agent-context.mjs`))
    getCurrentTimeSnapshot = createScenarioTimeProvider(broker, currentTimeSnapshot)
  }
  const { GatewayClient } = await import(pathToFileURL(`${root}/shared/gateway/client-sdk.mjs`))
  const { config } = await import(pathToFileURL(`${root}/server/src/core/config.mjs`))
  const { AcpBackendAdapter } = await import(pathToFileURL(`${root}/server/src/backend/adapters/acp/backend-adapter.mjs`))
  const { builtinMcpServers } = await import(pathToFileURL(`${root}/server/src/backend/adapters/acp/builtin-mcp.mjs`))
  const { createBackendAgentHost } = await import(pathToFileURL(`${root}/server/src/backend/backend-adapter-sdk.mjs`))
  // ACP creates explicit per-session tool environments. Use the native
  // constructor's composition parameter, retaining all original baseline tools,
  // so the business environment also reaches delegated native Sessions.
  const backend = new AcpBackendAdapter({
    protocol: 'qwen', root: config.root, ownership: config.backendOwnership,
    permissionMode: config.backendPermissionMode, timeoutMs: config.agentTimeoutMs,
    ...config.backends.qwen, sessionStatePath: config.backendSessionStatePath,
    builtinMcp: [...builtinMcpServers(), {
      name: 'tau', type: 'http', url: `http://127.0.0.1:${port}${mcpPath}`, headers: [],
    }],
  })
  if (message.diagnostics_enabled) observeBackend(backend, send)
  application = createGatewayApplication({ autoStart: false, agent: createBackendAgentHost(backend),
    ...(scenarioTimeEnabled ? { getCurrentTimeSnapshot } : {}),
  })
  if (message.persistent_tasks_enabled) {
    const { AgentTaskRuntime } = await import(pathToFileURL(`${root}/server/src/frontend/tools/agent-task-runtime.mjs`))
    stopPersistentObserver = observePersistentTasks({ taskManager: application.services.taskManager, AgentTaskRuntime, emit: send })
  }
  await application.services.agent.start()
  gateway = application.start({ host: '127.0.0.1', port: 0 })
  await once(gateway, 'listening')
  const metadata = JSON.parse(readFileSync('/opt/provenance.json', 'utf8'))
  Object.assign(metadata, {
    qwen_code_version: execFileSync(qwen, ['--version'], { encoding: 'utf8' }).trim(),
    frontend_model: speech ? process.env.TAU_S2S_LLM_MODEL : process.env.QWEN_AUDIO_REALTIME_MODEL || 'qwen-audio-3.1-realtime-plus',
    frontend_provider: process.env.QWEN_AUDIO_REALTIME_PROVIDER || 'dashscope',
    frontend_time_source: scenarioTimeEnabled ? 'scenario' : 'host',
    ...(scenarioTimeEnabled ? { frontend_time_tool: SCENARIO_TIME_TOOL } : {}),
    ...(speech ? { speech_to_speech: speech.metadata } : {}),
    backend_protocol: 'qwen', backend_permission_mode: 'native',
    backend_model: process.env.OPENAI_MODEL || null,
    backend_reasoning_effort: process.env.QWEN_CODE_REASONING_EFFORT || null,
    sandbox: JSON.parse(readFileSync(`${trial}/sandbox-proof.json`, 'utf8')),
    policy_sha256: createHash('sha256').update(policy).digest('hex'),
    original_prompt_sha256: createHash('sha256').update(original).digest('hex'),
    effective_prompt_sha256: createHash('sha256').update(combined).digest('hex'),
    tool_names: message.tools.map(tool => tool.name),
  })
  client = new GatewayClient({
    url: `ws://127.0.0.1:${gateway.address().port}/api/realtime?sessionId=${encodeURIComponent(message.session_id)}`,
    createSocket: url => new WebSocket(url), clientType: 'tau-voice', reconnect: false,
    locale: 'en-US', timeZone: 'UTC',
    capabilities: ['input.audio', 'playback.receipts', 'permissions.respond', 'tasks.input.respond', 'session.heartbeat'],
    configure: { voiceEnabled: true, inputEnabled: true, outputEnabled: true, textOnly: false,
      workingDirectory: workspace },
    onStatus(status) {
      if (['unavailable', 'disconnected', 'occupied', 'replaced', 'revoked'].includes(status.state) && !closing) {
        fail(new Error(`Gateway connection ${status.state}`))
      }
    },
    onEvent(event) {
      const failure = gatewayFailure(event)
      if (failure) { fail(failure); return }
      if (event.type === 'voice.ready') {
        send({ type: 'ready', protocol: 1, input_sample_rate: event.inputSampleRate, metadata })
      } else send(gatewayMedia(event))
    },
  })
  client.start()
}

const server = createServer((request, response) => {
  if (request.url === mcpPath && broker) {
    serveMcp(request, response, broker).catch(fail)
  } else { response.writeHead(404); response.end() }
})
const sockets = new WebSocketServer({ noServer: true, maxPayload: 16 * 1024 * 1024 })
server.on('upgrade', (request, socket, head) => {
  const supplied = Buffer.from(request.headers.authorization || '')
  const expected = Buffer.from(`Bearer ${token}`)
  if (request.url !== '/control' || controller || supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
    socket.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return
  }
  sockets.handleUpgrade(request, socket, head, ws => sockets.emit('connection', ws))
})
sockets.on('connection', ws => {
  controller = ws
  ws.on('error', () => { close().finally(() => process.exit(1)) })
  ws.on('close', () => { close().finally(() => process.exit(0)) })
  ws.on('message', raw => {
    try {
      const message = JSON.parse(raw)
      if (message.type === 'initialize') {
        initialize(message).catch(fail)
      } else if (!initialized || !client) {
        throw new Error('Bridge is not initialized')
      } else if (message.type === 'input_audio') {
        if (!client.send({ type: 'input_audio_buffer.append', audio: message.audio })) throw new Error('Audio input rejected')
      } else if (message.type === 'playback') {
        if (!['playback.started', 'playback.ended', 'playback.cancelled'].includes(message.event)) throw new Error('Invalid playback event')
        if (!client.send({ type: message.event, responseId: message.response_id })) throw new Error('Playback receipt rejected')
      } else if (message.type === 'tool_result') {
        broker.complete(message)
      } else throw new Error('Unknown bridge message')
    } catch (error) { fail(error) }
  })
})
server.listen(port, '0.0.0.0')
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => close().finally(() => process.exit(0)))
