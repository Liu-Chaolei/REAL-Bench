# Running Gaia2 async tasks

Tasks are part of the ordinary Gaia2 collection. Select the `async` split or
ordinary task IDs; the runner installs async observation from the embedded
private configuration automatically. No `--persistent-task-config` is needed.

The first migrated task is
`0052_snsq4rd73gytoo0wr75bgd2nyzra07z1__async_0001_v1`.

For the existing Qwen deployment:

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.persistent.example.json \
  --task-ids 0052_snsq4rd73gytoo0wr75bgd2nyzra07z1__async_0001_v1 \
  --max-seconds 180 --save-to gaia2-async-smoke-01
```

For another tested Agent, use its ordinary run configuration and implement the
public async evidence port described in [the architecture](persistent-tasks.md).
An unsupported Agent can still execute ordinary tasks; unsupported async cases
are skipped individually with null scores. Draft async data is also skipped,
with a distinct task-not-ready reason. Task definitions do not prescribe Qwen.

Existing results are preserved under `data/simulations/`. Use a new output prefix
for each attempt. Historical qpt results refer to the old definitions and are
shown as history, not as measurements of the new task IDs.

Summarize integrated task attempts with:

```bash
UV_FROZEN=1 .venv/bin/python scripts/report_gaia2_async.py
```

Reports are written to `data/simulations/gaia2-async-summary.*`. Mixed-batch
unsupported and unready cases are unscored; valid measurements and model behavior
are counted separately. Changing a task's data requires normal artifact pinning;
public runtime changes also require the ordinary final seal workflow.
