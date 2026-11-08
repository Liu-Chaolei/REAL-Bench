# Effective interaction diagnostics

`interaction` labels the usefulness of frontend communication in a saved run.
One LLM judge request covers an entire task trial, including all requests of a
continuous task. It reports useful, unhelpful and uncertain communication acts
with their source utterance IDs, text, types and short reasons. It is an optional
diagnostic and does not change the task reward.

```bash
tau2 metrics run RUN_DIR --config examples/effective-interaction.yaml --only interaction --dry-run
tau2 metrics run RUN_DIR --config examples/effective-interaction.yaml --only interaction
tau2 metrics summarize RUN_DIR
tau2 metrics export RUN_DIR --output interaction-report.json
```

Configure the judge using the existing `model` or `models.interaction` profile
and `api_key_env`. The example selects only interaction, runs one trial at a time
and allows one request per trial. Successful cached judgments require no new
requests. Invalid output, excessive input or provider errors are recorded without
automatic repair, splitting or retries; rerun explicitly to retry failures.

The judge receives the full delivered dialogue and short previews of recorded
backend messages and tool events. Large fields are truncated and marked. Hidden
task scripts, reference answers, task reward, audio and database snapshots are
not supplied. Historical runs without diagnostic evidence use saved dialogue
alone, marked `conversation_only`; select only interaction for this fallback.
Missing execution evidence leaves unverifiable status claims uncertain.

Version `tau-interaction-effectiveness/2` defaults to one complete response per
utterance. The judge splits questions requiring separate user responses, or clear
switches to independent topics or tasks. Introductory acknowledgements stay with
the main act: "Received. Who is the recipient? What model is your computer?" has
two acts, not three. Results, explanations and progress for one coherent response
stay together even when their types differ. Splits must preserve conditions,
negation, tense and referents; "I'll notify you when it's sent" remains a future
plan. A separate confirmation question at the end of a report is still split.
Source utterance IDs are not merged. Initial acknowledgements and reasonable waiting updates can be useful;
repeated questions, off-task requests and unsupported claims require context.
The judge is instructed to evaluate each act as of its occurrence. Since it sees
the whole call, this batch mode cannot guarantee isolation from future context.
Independent event clocks are marked and must not be compared as if synchronized.

The program computes:

- **Effective interaction rate:** useful / (useful + unhelpful).
- **Judgment coverage:** (useful + unhelpful) / all acts, including uncertain.

Zero denominators produce `null`. Failed grading is not an agent score of zero.
Across trials, counts are pooled before calculating rates; infrastructure and
user-simulator error calls retain diagnostic detail but are excluded from the
headline counts. Different diagnostic profiles are reported separately.

`tau2 view` displays rates and counts. Full labels and reasons are saved in
`artifacts/task_<id>/sim_<id>/diagnostics/evaluations/<profile>/interaction.json`
and included in export. Model requests, usage and cost use the existing request
logs in the same profile directory. Summarize, export and dry-run make no model
calls.

## Live smoke check

On 2026-11-07, the existing DeepSeek judge profile completed one request for a
saved three-stage call (22 frontend utterances, 24 acts). A separate email example
correctly distinguished acknowledgement, missing-recipient clarification,
unrelated and repeated questions, and an unverifiable sending claim in one
request. The interaction and diagnostic integration selection passed 32 tests
with the local audio library path configured.

The original v1 runs also exposed hindsight in reasons and a split that detached
the condition in a future plan. A suspected contact-answer error was subsequently
traced to conflicting reference and tool data, so it is not a confirmed judge
miss. Inputs, original scores and review notes are under
`data/simulations/effective-interaction-live-20261107/`. The v2 splitting policy
changes act counts; keep its results separate from v1 scores.

The same two real calls were regraded once each under v2, using identical call
data and the same judge profile. Coherent result/progress responses were merged,
and an independent redundant confirmation at the end of a long report was split
out. The noisy future-plan sentence was kept intact but still misread as a
completion claim, so the change does not establish that semantic issue is fixed.
The original and revised outputs and review are saved under
`data/simulations/effective-interaction-split-v2-20261107/`.
