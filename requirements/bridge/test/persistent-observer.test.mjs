import test from 'node:test'
import assert from 'node:assert/strict'
import { observePersistentTasks } from '../persistent-observer.mjs'

test('native observer keeps promise identity, real results and lifecycle separate', async () => {
  const events = []
  let listener
  const manager = { subscribe(fn) { listener = fn; return () => { listener = null } } }
  let originalPromise
  class Runtime {
    constructor(host) { this.host = host }
    executeStatusToolCall(input) {
      originalPromise = this.host.sendOutput(input.callId, { status: 'ok', task_id: 'real', task_status: 'failed', error: 'native failure' }, input.turnId, 'real')
      return originalPromise
    }
    executeUpdateToolCall() { throw new Error('native error') }
    executeCancelToolCall() {}
  }
  const output = async () => 'native-output'
  const host = { sessionId: 's', sendOutput: output }
  const restore = observePersistentTasks({ taskManager: manager, AgentTaskRuntime: Runtime, emit: e => events.push(e) })
  try {
    listener({ type: 'task.running', task: { id: 'real', scope: 'user', kind: 'work', status: 'running' } })
    const runtime = new Runtime(host)
    const returned = runtime.executeStatusToolCall({ callId: 'c', args: {}, turnId: 't' })
    assert.equal(returned, originalPromise)
    assert.equal(await returned, 'native-output')
    const completed = events.find(e => e.kind === 'native_operation_completed')
    assert.equal(completed.task_id, 'real')
    assert.equal(completed.success, true) // Failed task, successful status query.
    assert.equal(completed.result.task_status, 'failed')
    assert.equal(events.find(e => e.type === 'backend_task_state').state, 'running')
    assert.throws(() => runtime.executeUpdateToolCall({ callId: 'x', args: {} }), /native error/)
  } finally { restore() }
  assert.equal(host.sendOutput, output)
  assert.equal(listener, null)
})

test('an observer failure cannot change native task execution', () => {
  class Runtime {
    executeStatusToolCall() { return 42 }
    executeUpdateToolCall() {}
    executeCancelToolCall() {}
  }
  const restore = observePersistentTasks({ taskManager: { subscribe() { return () => {} } },
    AgentTaskRuntime: Runtime, emit() { throw new Error('observer failure') } })
  try {
    const runtime = new Runtime()
    runtime.host = { sendOutput() {} }
    assert.equal(runtime.executeStatusToolCall({ callId: 'c', args: {} }), 42)
  } finally { restore() }
})
