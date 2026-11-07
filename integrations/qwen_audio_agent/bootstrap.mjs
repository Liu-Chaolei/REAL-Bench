// Trusted launcher: only this supervisor remains outside the Agent boundary.
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readlinkSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

process.umask(0o077)
const trial = mkdtempSync('/tmp/tau-qwen-')
const denied = mkdtempSync('/tmp/tau-qwen-denied-')
const marker = join(denied, 'marker')
writeFileSync(marker, 'must remain unreadable to the Agent')
for (const name of ['workspace', 'config', 'tmp', 'state', 'data', 'cache', 'prompts', 'qwen', 'qwen-runtime', 'home']) {
  mkdirSync(join(trial, name))
}
const env = { ...process.env, TAU_TRIAL_ROOT: trial, TAU_DENIED_MARKER: marker,
  TMPDIR: join(trial, 'tmp'), TMP: join(trial, 'tmp'), TEMP: join(trial, 'tmp'),
  XDG_CONFIG_HOME: join(trial, 'config'), XDG_CACHE_HOME: join(trial, 'cache'),
  XDG_DATA_HOME: join(trial, 'data'), XDG_STATE_HOME: join(trial, 'state'),
  QWEN_CODE_TAU_ROOT: trial,
  TAU_PARENT_MOUNT_NS: readlinkSync('/proc/self/ns/mnt'),
  TAU_PARENT_PID_NS: readlinkSync('/proc/self/ns/pid'),
  TAU_PARENT_USER_NS: readlinkSync('/proc/self/ns/user'),
  TAU_EXPECTED_UID: String(process.getuid()), TAU_EXPECTED_GID: String(process.getgid()),
}
const launcher = fileURLToPath(new URL('./tau-isolate', import.meta.url))
const entry = fileURLToPath(new URL('./confined.mjs', import.meta.url))
const child = spawn(launcher, [trial, marker, process.execPath, entry, ...process.argv.slice(2)], {
  cwd: join(trial, 'workspace'), env, stdio: ['ignore', 'inherit', 'inherit'],
})
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal))
let cleaned = false
function cleanup() {
  if (cleaned) return
  cleaned = true
  rmSync(trial, { recursive: true, force: true })
  rmSync(denied, { recursive: true, force: true })
}
child.on('error', () => { cleanup(); process.exit(125) })
child.on('exit', (code, signal) => { cleanup(); process.exit(code ?? (signal === 'SIGTERM' ? 0 : 125)) })
