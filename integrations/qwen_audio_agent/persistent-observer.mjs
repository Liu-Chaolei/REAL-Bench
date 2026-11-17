// Passive instrumentation of the pinned native TaskManager and frontend tools.
import { randomUUID } from 'node:crypto'

export const PERSISTENT_CAPABILITIES = Object.freeze({
  version: 1,
  source: 'native_task_runtime',
  statuses: ['queued', 'running', 'delegated', 'finalizing', 'cancelling', 'completed', 'failed', 'cancelled'],
  operations: ['query', 'result', 'modify', 'cancel'],
  tools: { query: 'get_agent_task_status', result: 'get_agent_task_status', modify: 'update_agent_task', cancel: 'cancel_agent_task' },
  waiting: 'unsupported: no dedicated waiting lifecycle state in pinned runtime',
})

export function observePersistentTasks({ taskManager, AgentTaskRuntime, emit }) {
  let sequence = 0
  const safe = event => {
    try { emit({ ...event, sequence: ++sequence, event_id: randomUUID(), provider_timestamp: performance.now() / 1000, provider_clock: 'bridge_monotonic' }) }
    catch { /* Native execution must never depend on an observer. */ }
  }
  const unsubscribe = taskManager.subscribe(event => {
    const task = event.task
    if (!task || task.scope !== 'user' || task.kind !== 'work') return
    safe({ type: 'backend_task_state', kind: event.type === 'task.accepted' ? 'task_created' : 'task_state_changed',
      task_id: task.id, session_id: task.sessionId, parent_task_id: task.parentTaskId,
      state: task.status, result: task.result,
      result_available: task.status === 'completed' && Object.hasOwn(task, 'result'),
      evidence_source: 'native_task_runtime', native_event: event.type, task })
  })
  const hosts = new Map()
  const originals = new Map()
  const methods = {
    executeStatusToolCall: ['query', 'get_agent_task_status'],
    executeUpdateToolCall: ['modify', 'update_agent_task'],
    executeCancelToolCall: ['cancel', 'cancel_agent_task'],
  }
  for (const [method, [operation, tool]] of Object.entries(methods)) {
    const original = AgentTaskRuntime.prototype[method]
    if (typeof original !== 'function') throw new Error(`Unsupported native task method ${method}`)
    originals.set(method, original)
    AgentTaskRuntime.prototype[method] = function (input) {
      const host = this.host
      if (!hosts.has(host)) {
        const sendOutput = host.sendOutput
        const calls = new Map()
        hosts.set(host, { sendOutput, calls })
        host.sendOutput = function (callId, output, turnId, taskId, ...rest) {
          const call = calls.get(callId)
          if (call) {
            calls.delete(callId)
            safe({ ...call, type: 'backend_native_operation', kind: 'native_operation_completed',
              task_id: output?.task_id || taskId || call.task_id,
              success: output?.error !== true && !output?.isError && !['not_found', 'not_active', 'duplicate', 'partial'].includes(output?.status),
              result: output, result_available: true })
          }
          return sendOutput.call(this, callId, output, turnId, taskId, ...rest)
        }
      }
      const call = { operation, tool_name: tool, call_id: input.callId,
        session_id: host.sessionId, task_id: input.args?.task_id || null,
        arguments: input.args || {}, turn_id: input.turnId }
      hosts.get(host).calls.set(input.callId, call)
      safe({ ...call, type: 'backend_native_operation', kind: 'native_operation_started' })
      // Preserve the original return object/promise and synchronous exceptions.
      return original.call(this, input)
    }
  }
  safe({ type: 'backend_observer_ready', capabilities: PERSISTENT_CAPABILITIES })
  return () => {
    unsubscribe()
    for (const [method, original] of originals) AgentTaskRuntime.prototype[method] = original
    for (const [host, { sendOutput }] of hosts) host.sendOutput = sendOutput
    hosts.clear()
  }
}
