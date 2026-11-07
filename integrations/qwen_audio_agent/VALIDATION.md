# HPC filesystem-view validation — 2026-10-24

The default integration now uses private user, mount and PID namespaces instead
of the old Landlock ABI 1 rules. Normal cross-directory rename and hard-link
operations within the trial directory are restored on the current 5.15 kernel.
The historical 0.1.0 result is retained in [VALIDATION-landlock.md](VALIDATION-landlock.md).

- Image: `docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.2.0`.
- Image ID: `sha256:08683514d85f02ee57876952569b2b1aec046ad492d0828b1737e3d50311d52e`.
- Unmodified Qwen source: `d2a8dc45b9b49d560c876eb7ed449eb9b90df1b9`.
- Qwen Code `0.26.0`; selected real config remains DashScope
  `qwen-audio-3.0-realtime-plus`, backend `MiniMax-M3`, reasoning effort `low`.
- Actual platform UID/GID remain `30215:20001`; 16 automatic mounts are supported.
- Independent CPython `3.12.12` venv, user site disabled, unchanged frozen lock,
  pinned Ruff `0.14.5`.

## Completed checks

| Check | Result |
| --- | --- |
| Python adapter, streaming agent, runner and launch-failure regressions | 61 passed |
| Complete image contracts under actual platform UID and setup options | 11 passed |
| Native cross-directory file/directory rename, overwrite and shell `mv` | Passed; inode preserved |
| Cross-directory hard links and valid internal symlink paths | Passed |
| Moves between the unchanged HOME spelling and the workspace | Passed; one underlying private directory tree |
| Actual shared marker visible before isolation, absent afterwards | Passed |
| Absolute/relative traversal, symlink, child process and `/proc/1/root` attempts | Hidden files remain inaccessible |
| Image programs/system trees read-only; private workspace writable | Passed |
| New namespaces, zero capabilities, no-new-privileges and inherited view lock | Passed |
| Child attempt to create another namespace | Rejected |
| Real config parsing and DockerRuntime startup, without model sessions | Passed |
| Five-task English retail control preview | Passed; no scored simulation executed |
| Standard live provider suite | 132 skipped; 0 failures |
| Ruff on changed Python files, whitespace checks | Passed |

The 11 image contracts run the real Gateway, ACP, Qwen Code and MCP. Only remote
model endpoints are deterministic local stubs. They verify delegation, native
permission handling, a business result returning as audio, and the actual model
request's selected model and reasoning-effort parameter. They are not model
accuracy results.

## Isolation behavior and deployment requirements

The launcher constructs a small filesystem containing image programs, runtime
system directories and this trial's generated root. Real shared home/storage
contents are omitted. HOME retains its environment value and becomes a symlink
to the trial's fresh `home/`, preserving native file moves without exposing host
settings. No per-reference-file deny list or Agent source modification is used.
Original qwaudio configuration and shared Qwen settings were verified unchanged.

Setup requires Docker options `seccomp=unconfined`, `apparmor=unconfined` and
`systempaths=unconfined`, accepted by the actual platform wrapper. No privileged
container, root user override or added capability is requested. Before any Agent
code runs, the trusted helper drops all capabilities, closes external handles,
locks namespace/mount changes with inherited seccomp and uses a private proc
filesystem. Missing setup support or failed access/rename checks abort startup;
there is no fallback to the old restriction or an unconfined Agent.

The boundary controls local files. Network access remains unchanged. Other
platforms must support these setup options and the required namespace/mount
operations; updating the Docker image alone cannot supply missing host support.

Real five-task voice scores are still pending ElevenLabs, Deepgram, caller voice
configuration and normal evaluator simulator/judge settings. No paid model
sessions were started for this validation. The real backend service's acceptance
of its reasoning parameter remains untested here.

See [README.md](README.md) for launch and reproduction commands.
