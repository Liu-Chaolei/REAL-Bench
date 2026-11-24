import assert from 'node:assert/strict'
import { readFileSync, writeFileSync, symlinkSync, unlinkSync, mkdirSync, renameSync,
  linkSync, statSync, readdirSync, readlinkSync } from 'node:fs'
import { execFileSync, spawnSync } from 'node:child_process'
import { join } from 'node:path'
const root = process.env.TAU_TRIAL_ROOT
const denied = process.env.TAU_DENIED_MARKER
const paths = [denied, ...JSON.parse(process.env.TAU_SANDBOX_TEST_PATHS || '[]')]
for (const path of paths) {
  assert.throws(() => readFileSync(path), { code: 'ENOENT' })
  assert.throws(() => readFileSync(`/proc/1/root${path}`), { code: 'ENOENT' })
  const link = join(root, 'workspace', 'escape')
  symlinkSync(path, link)
  assert.throws(() => readFileSync(link), { code: 'ENOENT' })
  unlinkSync(link)
}
for (const path of ['/aistor', '/mnt']) {
  if (path) assert.throws(() => readdirSync(path), { code: 'ENOENT' })
}
// The original HOME spelling points at a fresh directory inside this trial.
assert.deepEqual(readdirSync(process.env.HOME), [])
writeFileSync(join(process.env.HOME, 'home-probe'), 'private')
const homeInode = statSync(join(process.env.HOME, 'home-probe')).ino
renameSync(join(process.env.HOME, 'home-probe'), join(root, 'workspace', 'home-probe'))
assert.equal(statSync(join(root, 'workspace', 'home-probe')).ino, homeInode)
assert.throws(() => writeFileSync('/tmp/tau-outside-probe', 'no'), { code: 'EROFS' })
assert.throws(() => writeFileSync('/opt/tau-outside-probe', 'no'), { code: 'EROFS' })
assert.throws(() => execFileSync('/bin/cat', [denied], { stdio: 'pipe' }))
const first = join(root, 'workspace', 'a'), second = join(root, 'workspace', 'b')
mkdirSync(first); mkdirSync(second)
writeFileSync(join(first, 'file'), 'data')
const inode = statSync(join(first, 'file')).ino
renameSync(join(first, 'file'), join(second, 'file'))
assert.equal(statSync(join(second, 'file')).ino, inode, 'Real rename must preserve inode')
linkSync(join(second, 'file'), join(first, 'hardlink'))
assert.equal(statSync(join(first, 'hardlink')).ino, inode)
writeFileSync(join(first, 'replacement'), 'replacement')
renameSync(join(first, 'replacement'), join(second, 'file'))
assert.equal(readFileSync(join(second, 'file'), 'utf8'), 'replacement')
// Valid relative paths, directories and internal absolute symlinks keep native semantics.
symlinkSync(second, join(first, 'inside'))
renameSync(join(first, 'hardlink'), join(first, 'inside', 'relative-file'))
mkdirSync(join(first, 'directory'))
renameSync(join(first, 'directory'), join(second, 'directory'))
execFileSync('/bin/mv', [join(second, 'relative-file'), join(first, 'shell-moved')])
assert.equal(statSync(join(first, 'shell-moved')).ino, inode)
// Child tools cannot rebuild the filesystem view or enter another namespace.
const attempt = spawnSync('/usr/bin/unshare', ['--user', '--map-root-user', 'true'], { encoding: 'utf8' })
assert.notEqual(attempt.status, 0)
assert.match(attempt.stderr, /Operation not permitted/)
const namespace = readlinkSync('/proc/self/ns/mnt')
const childNamespace = execFileSync(process.execPath, ['-e', "process.stdout.write(require('fs').readlinkSync('/proc/self/ns/mnt'))"], { encoding: 'utf8' })
assert.equal(childNamespace, namespace)
console.log('cross_directory_rename=true')
console.log('cross_directory_hardlink=true')
console.log('namespace_escape=denied')
console.log(readFileSync(join(root, 'sandbox-proof.json'), 'utf8'))
