# Outcome grading and actual-mail replies

> 当前 Gaia2 整题评分仅使用环境组件；旧 ACTION/NL/COMMUNICATE 不再计算。必要信息覆盖由 conciseness 的 required_coverage 单独评价，后台 reply_reward 保留。阶段交付只记日志，不因 missing_requests 直接清零。下文历史验证记录保留原口径；当前指标见 [metrics-inventory.md](metrics-inventory.md)。

`tau-outcome-grading-v2` applies to every τ domain in text and voice. A returned
`max_steps` or `timeout` trajectory receives the same ENV, COMMUNICATE, ACTION and
NL checks selected by the task's existing evaluation type and reward basis.
The ending remains recorded. Error endings retain their existing classification.
A successfully graded budget ending has `evaluation_status=completed`; a judge
or replay error has `evaluation_status=failed` and retains its original trace.

Continuous tasks collect component diagnostics before enforcing the delivery
requirement. If any request was not fully delivered, the whole-task reward is
zero and `RewardInfo.info.missing_requests` explains why. Component scores are
retained; there are no new per-stage total scores. `trajectory_seal` records the
interaction stop and the journal boundary after accepted work has drained.
Cleanup does not create additional turns. Any task-specific deadline is judged
against recorded execution time.

`gaia2-actual-mail-replies-v1` covers 164 scripted replies in 81 tasks. In online
mode, each correspondent binds to the earliest successful outgoing email in the
corresponding stage whose recipient and business content match. Existing semantic
matching is reused, asynchronously and cached per content pair. Group mail and
separate recipient mail are both supported. Thread replies additionally match the
actual parent email. No calendar or spoken completion anchor is required.

Each reply uses the actual sent email ID, the original reply text and delay,
measured from actual sending. A late match is delivered at the current time;
nothing is backdated. A `trigger` journal row preserves the binding and version.
Failures of the matcher remain visible. Offline reference mode retains the
original graph. Replay mode executes only recorded effects and never generates
new mail. Other environment events retain their current graph dependencies.

The standalone key-free verification command exercises both group/original and
individual recipient sends through real application tools and strict replay:

```bash
UV_FROZEN=1 uv run python scripts/verify_gaia2_actual_replies.py \
  --workers 12 --output data/simulations/gaia2-outcome-replies-v2-validation
UV_FROZEN=1 uv run tau2 gaia2 verify --reference --workers 16 --seal
```

`event-audit.json` inventories migrated and retained event dependencies. Fixed-ID
replies (17 events) and non-mail events are retained, not certified by this change.
Reference verification still validates 800 frozen reference executions and strict
replay; it does not prove all possible agent paths are supported.

Before resealing, archive the data using `tau2.domains.gaia2.recorded.archive_data`.
It copies task data, snapshots, verification records, extension metadata and the
English pack header, together with an integrity manifest. Resource bytes are
reused through their original verified manifest. Historical rescoring uses:

```bash
UV_FROZEN=1 uv run tau2 evaluate-trajs /path/to/original/results.json \
  --recorded-data-root data/archives/gaia2-before-outcome-replies-v2-20261107 \
  --output-dir /path/to/new-rescore
```

The archive must match the original embedded tasks and language artifact pins.
It cannot be combined with `--fresh-tasks`. Live runs still require the current
runtime seal. Old results remain untouched, and absent replies remain absent in
historical grading. Judge calls may be needed; agent/user/audio services are not
called during rescoring.

This change does not alter fixed continuation text, Qwen permissions, model
configuration, stage STOP semantics or the task's reference business targets.

Historical strict replay compares notification time strings by their exact numeric
value, since the application serializer stringifies scalars. For
`Messages__list_recent_conversations`, upstream participant IDs are a set; replay
compares exact participant membership while preserving conversation and message
order. No missing notification, changed participant or changed message passes.

The private judge profile can be applied without exposing credentials using
`scripts/rescore_gaia2_outcomes.py` with the same arguments.
