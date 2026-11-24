// Query the evaluator's clock on demand; never keep a second clock in Node.
export const SCENARIO_TIME_TOOL = 'System__get_current_time'

export const SCENARIO_TIME_POLICY = 'Frontend get_current_time and backend '
  + 'System__get_current_time read the same scenario clock in UTC. Interpret '
  + 'today, tomorrow and now against that clock. The scenario start date is '
  + 'not a permanently fixed current time; query the time tool when needed.'

export function createScenarioTimeProvider(broker, formatSnapshot) {
  return async context => {
    const result = await broker.call({ name: SCENARIO_TIME_TOOL, arguments: {} })
    if (result.isError) throw new Error('Scenario time query failed')
    const value = JSON.parse(result.content.map(item => item.text || '').join(''))
    // tau's tool serializer also represents numeric fields as JSON strings.
    const raw = value?.current_timestamp
    const timestamp = typeof raw === 'number' || (typeof raw === 'string' && raw.trim())
      ? Number(raw) : NaN
    const now = new Date(timestamp * 1000)
    if (!Number.isFinite(timestamp) || !Number.isFinite(now.getTime())) {
      throw new Error('Scenario time query returned an invalid timestamp')
    }
    return formatSnapshot({ ...context, now })
  }
}
