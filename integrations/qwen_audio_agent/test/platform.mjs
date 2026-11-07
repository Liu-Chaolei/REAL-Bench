// Run manually via verify_platform.py, never with real credentials.
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
const paths = JSON.parse(process.env.TAU_SANDBOX_TEST_PATHS).filter(path => existsSync(path))
assert.ok(paths.length, 'Shared marker must be visible before confinement')
for (const path of paths) {
  assert.equal(readFileSync(path, 'utf8'), 'tau-platform-test-marker')
}
process.env.TAU_SANDBOX_TEST_PATHS = JSON.stringify(paths)
console.log(`Platform UID=${process.getuid()} GID=${process.getgid()}; shared markers visible before confinement`)
console.log(execFileSync(process.execPath, ['/opt/tau-bridge/bootstrap.mjs', '--sandbox-test'], {
  env: process.env, encoding: 'utf8', timeout: 15000,
}))
console.log('TAU_PLATFORM_VERIFIED')
