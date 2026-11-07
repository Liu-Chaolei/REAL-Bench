// The platform wrapper may itself exit zero after a failing container command.
import { spawnSync } from 'node:child_process'
const result = spawnSync(process.execPath, ['--test', '--test-concurrency=1',
  '/opt/tau-bridge/test/contracts.test.mjs', '/opt/tau-bridge/test/core.test.mjs',
  '/opt/tau-bridge/test/sandbox.test.mjs', '/opt/tau-bridge/test/system.test.mjs',
], { stdio: 'inherit', env: process.env })
if (result.status !== 0) process.exit(result.status || 1)
console.log('TAU_IMAGE_VERIFIED')
