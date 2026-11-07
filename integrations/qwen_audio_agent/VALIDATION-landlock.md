# HPC validation — 2026-10-24

The integration evaluates the complete Qwen Audio Agent through its original
Gateway, ACP adapter and Qwen Code. The Qwen checkout remains clean; the existing
qwaudio configuration and shared Qwen settings were verified unchanged.

- Image: `docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.1.0`.
- Image ID: `sha256:936542d867a08d434214b01c9b457c6b29e2f3f21fbb45c59815c60eb81e8e47`.
- Qwen source: `d2a8dc45b9b49d560c876eb7ed449eb9b90df1b9`.
- Qwen Code: `0.26.0`; evaluator: CPython `3.12.12`, independent `.venv`,
  `site.ENABLE_USER_SITE=False`, frozen lock; Ruff `0.14.5`.
- Real platform UID/GID: `30215:20001`, with 16 automatically injected mounts.
- Selected config: DashScope `qwen-audio-3.0-realtime-plus`, Qwen Code backend
  `MiniMax-M3`, reasoning effort `low`. No original config text/keys are recorded.

## Completed checks

| Check | Result |
| --- | --- |
| Python adapter, configuration, streaming agent and runner regressions | 60 passed |
| Image build contracts, including the complete native system | 11 passed |
| Same native contracts under the platform UID and automatic mounts | 11 passed |
| Real shared-volume marker: readable before restriction, denied afterwards | Passed |
| Absolute/relative/parent traversal, symlink and child-process read attempts | Denied |
| Private workspace read/write and same-directory rename | Passed |
| Native model request retains selected model and `reasoning_effort=low` | Passed with local model stubs |
| Private/redacted logs are saved before removing the owned container | Passed |
| Real config parsing and DockerRuntime startup without starting model sessions | Passed |
| Five-task English retail control preview | Passed; no scored simulation executed |
| Provider suite | 132 live cases skipped; 0 failures |
| Ruff on changed Python files and diff whitespace checks | Passed |

The complete native contract runs real Gateway, ACP, Qwen Code and MCP. Only the
remote realtime/backend model endpoints are local deterministic stubs; it is a
transport/orchestration test, not a model accuracy measurement. Business execution
still passes through the evaluator, while native delegation and permission
handling remain in the original system.

## Remaining live prerequisites and known limit

No real control/regular five-task voice scores were produced. This environment
lacks `ELEVENLABS_API_KEY`, `DEEPGRAM_API_KEY` and caller voice configuration;
the evaluator's simulator/judge must also be configured through normal tau
settings. The supplied Agent config is not automatically exported into those
separate services. The real remote backend endpoint's response to the reasoning
parameter has not been tested here.

The current kernel provides Landlock ABI 1. Local reference-file reading is
blocked across path spellings, but cross-directory rename/link is restricted
inside the private workspace as well. Each ready event records this limitation
alongside the sandbox proof, UID/GID and allowed roots. This boundary does not
restrict network access. File-manipulation benchmarks need this condition
considered before interpreting scores.

See [README.md](README.md) for build, verification and launch commands. The fixed
host launcher is `integrations/qwen_audio_agent/run_hpc.sh`.
