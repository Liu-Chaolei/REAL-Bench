import test from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'

test('confinement enforces file access and is inherited by native children', {
  skip: !process.env.TAU_QWEN_SYSTEM_TEST && 'Run in the built image',
}, () => {
  const output = execFileSync(process.execPath, [new URL('../bootstrap.mjs', import.meta.url).pathname, '--sandbox-test'], {
    env: process.env, encoding: 'utf8', timeout: 15000,
  })
  assert.match(output, /child_denied/)
  assert.match(output, /cross_directory_rename=true/)
  assert.match(output, /cross_directory_hardlink=true/)
  assert.match(output, /mount_namespace/)
})
