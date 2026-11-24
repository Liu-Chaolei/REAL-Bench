// Verify the new filesystem view before importing any original Agent code.
import { readFileSync, writeFileSync, symlinkSync, unlinkSync, renameSync, mkdirSync,
  linkSync, statSync, readlinkSync, rmdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { join, relative } from 'node:path'
const root = process.env.TAU_TRIAL_ROOT
const denied = process.env.TAU_DENIED_MARKER
if (!root || !denied || process.env.TAU_FILESYSTEM_BOUNDARY !== 'namespace-v1') {
  throw new Error('The private filesystem view is required')
}
const namespaces = Object.fromEntries(['mnt', 'pid', 'user'].map(kind => [kind, readlinkSync(`/proc/self/ns/${kind}`)]))
for (const [kind, variable] of [['mnt', 'MOUNT'], ['pid', 'PID'], ['user', 'USER']]) {
  if (namespaces[kind] === process.env[`TAU_PARENT_${variable}_NS`]) throw new Error(`Missing private ${kind} namespace`)
}
const status = readFileSync('/proc/self/status', 'utf8')
if (!/^CapEff:\s+0+$/m.test(status) || !/^CapPrm:\s+0+$/m.test(status)
  || !/^CapBnd:\s+0+$/m.test(status) || !/^NoNewPrivs:\s+1$/m.test(status)
  || !/^Seccomp:\s+2$/m.test(status)) throw new Error('Agent privileges were not dropped or view is not locked')
if (String(process.getuid()) !== process.env.TAU_EXPECTED_UID
  || String(process.getgid()) !== process.env.TAU_EXPECTED_GID) throw new Error('Agent identity changed')
if (statSync(process.env.HOME).ino !== statSync(join(root, 'home')).ino) throw new Error('Private home alias is missing')
function hidden(path) {
  try { readFileSync(path); throw new Error('A hidden file was visible') }
  catch (error) { if (error.code !== 'ENOENT') throw error }
}
const first = join(root, 'workspace', '.boundary-a')
const second = join(root, 'workspace', '.boundary-b')
mkdirSync(first); mkdirSync(second)
const probe = join(first, 'file'), moved = join(second, 'file'), linked = join(first, 'link')
writeFileSync(probe, 'allowed')
const inode = statSync(probe).ino
renameSync(probe, moved)
if (readFileSync(moved, 'utf8') !== 'allowed' || statSync(moved).ino !== inode) throw new Error('Native cross-directory rename failed')
linkSync(moved, linked)
if (statSync(linked).ino !== inode) throw new Error('Native cross-directory hard link failed')
unlinkSync(linked); unlinkSync(moved); rmdirSync(first); rmdirSync(second)
hidden(denied); hidden(relative(process.cwd(), denied))
hidden(`/proc/1/root${denied}`)
const link = join(root, 'workspace', '.denied-link')
symlinkSync(denied, link); hidden(link); unlinkSync(link)
execFileSync(process.execPath, ['-e', `
  try { require('node:fs').readFileSync(process.argv[1]); process.exit(1) }
  catch (error) { process.exit(error.code === 'ENOENT' ? 0 : 2) }
`, denied])
const proof = { mechanism: 'mount_namespace', version: 1, namespaces,
  cross_directory_rename: true, cross_directory_hardlink: true, capabilities: 'none',
  scope: 'local_filesystem', uid: process.getuid(), gid: process.getgid(), trial_root: root,
  home_alias: { path: process.env.HOME, target: join(root, 'home') },
  checks: ['workspace_read_write', 'workspace_cross_directory_rename', 'workspace_cross_directory_hardlink',
    'absolute_denied', 'relative_denied', 'symlink_denied', 'child_denied', 'proc_root_denied',
    'private_namespaces', 'no_capabilities', 'view_locked'],
  readonly_roots: ['/', '/opt', '/usr', '/bin', '/sbin', '/lib', '/lib64', '/etc', '/proc', '/sys'],
  writable_roots: [root, '/dev'],
}
writeFileSync(join(root, 'sandbox-proof.json'), JSON.stringify(proof))
if (process.argv[2] === '--sandbox-test') await import('./test/sandbox-probe.mjs')
else await import('./bridge.mjs')
