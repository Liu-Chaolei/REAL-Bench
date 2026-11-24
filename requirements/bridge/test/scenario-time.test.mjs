import test from 'node:test'
import assert from 'node:assert/strict'
import { ToolBroker } from '../core.mjs'
import { createScenarioTimeProvider, SCENARIO_TIME_TOOL } from '../scenario-time.mjs'

test('frontend queries the broker clock afresh, without blocking other work', async () => {
  const sent = []
  const broker = new ToolBroker({ tools: [{ name: SCENARIO_TIME_TOOL }],
    send: event => sent.push(event), timeoutMs: 1000 })
  const contexts = []
  const read = createScenarioTimeProvider(broker, context => {
    contexts.push(context)
    return { iso_utc: context.now.toISOString(), time_zone: context.timeZone }
  })
  try {
    assert.equal(sent.length, 0, 'construction never queries before the evaluator is ready')
    for (const seconds of [1728975600, '1728975660.125']) {
      const pending = read({ timeZone: 'UTC', locale: 'en-US' })
      const event = sent.at(-1)
      assert.equal(event.name, SCENARIO_TIME_TOOL)
      assert.deepEqual(event.arguments, {})
      broker.complete({ call_id: event.call_id,
        content: JSON.stringify({ current_timestamp: seconds }), is_error: false })
      assert.equal((await pending).iso_utc, new Date(seconds * 1000).toISOString())
    }
    assert.equal(sent.length, 2)
    assert.equal(contexts[1].locale, 'en-US')
    for (const content of ['not json', '{}', '{"current_timestamp":"not-time"}', '{"current_timestamp":null}', '{"current_timestamp":1e30}']) {
      const pending = read({ timeZone: 'UTC' })
      broker.complete({ call_id: sent.at(-1).call_id, content, is_error: false })
      await assert.rejects(pending)
    }
    const pending = read({})
    broker.complete({ call_id: sent.at(-1).call_id, content: '{}', is_error: true })
    await assert.rejects(pending, /query failed/)
    assert.equal(contexts.length, 2, 'errors never reach the host formatter as a fallback')
  } finally { broker.close() }
})
