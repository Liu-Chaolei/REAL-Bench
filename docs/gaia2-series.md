# Authoring related Gaia2 task series

The completed local questions have been consolidated into the original
`data/tau2/domains/gaia2/tasks.json` (800 original IDs, three requests per task).
The separate authoring directories are recovery archives, not the active question
source. See [the current integration status](gaia2.md) before evaluation.

This offline pipeline extends each sealed Gaia2 task with two related requests.
The three stages share application state and scenario time. It produces native
`Task`, `ScenarioSpec`, `ScenarioAction` and `EvaluationCriteria` records, plus a
private stage index with material evidence, checkpoint outcomes and review records.
The canonical integrated tasks use the continuous runtime described in
[the Gaia2 guide](gaia2.md): intermediate caller STOP or out-of-scope signals release the next request,
with one final score after the call. Standalone authoring exports still require
the native integration and complete reference/replay verification before running.

## Material and task rules

### Active lightweight repair scope

The completed question audit defines a frozen partition of 401 passed tasks and
399 tasks requiring repair. The current command is `repair-light`; it does not
rerun the 675-task audit, schedule the 401 passed tasks or distinguish old/new
passes in the status ledger. Previous inputs/decisions remain in their source
directories for provenance. Their verification hashes are not rewritten.

```bash
python -m tau2.task_authoring.gaia2_series.cli repair-light \
  --dataset data/simulations/gaia2-question-revalidation-v2-20261102-162938/dataset \
  --output /path/to/new-light-repair \
  --config docs/gaia2-series.mimo.config.json
```

The new `task-status.json` starts with 401 `passed` and 399 `pending` records.
`new-policy-coverage.json` provides one unified passed count out of 800.
`scope.json` and `repair-scope-399.json` freeze the scope and its original routes.
401 passed tasks receive no new generation, audit, environment or replay calls.
`semantic_precheck` tasks retain their saved requests; other repair routes use a
small question lens and optional focused semantic check. A missing saved request
is blocked explicitly, never silently invented under the preservation route.

0149 and every other task use the same `repair_one` worker. There is no pilot
gate. Technical sessions start with empty model histories and no old candidate.
Only the current request, relevant tool names, up to three business records, and
the latest candidate/short error enter model input. Public reads are limited to
6000 characters; oversize reads require narrowing. Per stage: at most ten
content attempts, four material-read rounds and one format correction. Requests
use Key1/Key3, initially capped at one each with two task workers. Override with
`--concurrency 12 --per-key-concurrency 6` after measuring capacity. Settings are
saved in `runtime-settings.json` and reused on resume. Batches may overlap to fill
the workers; reports still contain ten tasks and commit in queue order. One
empty batch does not reduce the configured concurrency to a single batch.
Two empty batches stop new admission; any already admitted work finishes under
its existing budget and is reported separately. A task has a persisted
20-minute deadline; restart does not reset budgets. Two identical candidate/error
failures isolate a task; two batches of ten without a new pass stop the queue.

Both stages must execute, pass independent review and complete strict replay
to add a repaired task to `passed`. A failed first stage
never starts the second. Calls store request metrics and response hashes; the
current candidate and errors are saved in `sessions/`. Whole response histories
are not copied into the new work version or reintroduced into prompts. Run the
same command/output to resume unfinished work; terminal failures stay isolated.

The older revalidation workflow below documents how the fixed audit was produced;
it is not the entry point for the 399-task repair run.

The current lightweight protocol is `light-repair-v4-compact-verdict`. Model
submissions use `StageSolution` (events, evidence, checks, communicate_info,
nl_assertions); request text, event kind/channel and framework evaluation fields
are supplied by the host. An operation contains only id/tool/arguments/dependencies.
Reference replies use `AgentUserInterface__send_message_to_user` with
`arguments={"content": "<reply text>"}`; its schema is available from the material
reader. The host also normalizes the unambiguous `message` argument alias.
DeepSeek thinking mode uses `tool_choice=auto` on the final submission turn
because its endpoint rejects `required`; only the submission tool is exposed on
that turn and host-side schema validation remains mandatory. MiMo keeps the
original `required` behavior.
Framework actions and env_assertions are empty, the reward uses native scenario
and natural-language assertions, plus communication when required. The model
cannot submit DB rewards or action-reward trajectories through this interface.

Repairs use `SolutionRevision`: candidate SHA-256, at least one supplied complete
solution or answer/assertion block, and a reason. Events/evidence/checks,
communicate_info and nl_assertions can be revised. User request text remains
locked. There are no JSON patch addresses or array-index edits.
The host assembles a fresh `StageDraft`, validates it and checks actual business
outcomes. Block replacement is transactional and rejects stale hashes. Redundant
checks may be consolidated; coverage of the user goal is reviewed semantically,
not inferred from check counts. A revision alone never makes a task accepted.

Repairs start from the current candidate and focus on the reported error's path,
actual value and expected value. The prompt asks the author to confirm uncertain
tool signatures and return shapes, fix that cause first, and preserve unrelated
working content. Related events, evidence, checks and answers may change together;
there is no hard block whitelist. Only changed blocks are submitted; the host
retains omitted blocks. The revision reason briefly explains the cause and any
dependent edits. When resuming an older block-restricted session, its matching
business error is recovered from the local log rather than treating a removed
restriction as the repair goal. Schema, evidence, execution, replay and independent
review still determine acceptance; prompt guidance alone does not establish it.

Lightweight validation distinguishes prior facts from solution evidence. Facts
using current-stage states/results/public-read arguments are checked after the
stage operations complete in an isolated checkpoint copy. Unknown or future
stage references remain invalid. The old mechanical continuity marker is not
required in lightweight mode: the independent reviewer assesses the business
relationship. Strict mode remains available for older workflows.

During repair, execution and factual checks run with `replay=False`; completed
prefixes are cached inside the task session. Full replay and the native complete
business-state check run only after review accepts the final candidate. The
reviewer sees the actual delivered answers as well as the solution; rounding
and formatting instructions apply to each user request independently. In this
authoring workflow `communicate_info` supplies the proposed reference answer;
the reviewer verifies it directly against evidence without requiring an extra
reply event. Any supplied reply is checked alongside it. Review inputs group
each request with its own answer and solution; earlier goals are business
context only, and exact numeric values are retained unless that request asks
for rounding.

A rejected review returns its reasons to the affected stage and seeds a revision
of that stage's solution, answer and assertions. Good earlier stages are retained;
changing an earlier stage causes later solutions to be revised/rechecked. Each
lightweight review returns one `pass`/`fail` verdict, up to four concise reasons
and affected stages, rather than eight potentially contradictory quality flags.
Candidate answers and assertions are proposals to verify against the request.
Older multi-flag decisions remain unchanged and readable. Each
task allows at most two persisted review-correction rounds. Each grants one
additional attempt to the affected and dependent stages, beyond their initial
ten attempts, within the same 20-minute task deadline. Review attempts are not
reset by restarting. A previously saved, content-bound rejection is also fed back
when creating an explicitly authorized retry round (e.g. task 0016).

After an explicit protocol fix, create one separate round from a stopped run:

```bash
python -m tau2.task_authoring.gaia2_series.cli repair-light \
  --dataset data/simulations/gaia2-question-revalidation-v2-20261102-162938/dataset \
  --output /path/to/new-block-repair \
  --retry-from data/simulations/gaia2-light-repair-v2-20261102-234300/dataset \
  --config docs/gaia2-series.mimo.config.json
```

Previously blocked tasks go first (20 in the initial stopped run), followed by
the 379 untouched tasks, within the same 399-task scope. Their fixed requests are
retained; verified prefixes are rechecked locally once. A prefix that still
passes is reused, while the failing stage is freshly generated. Invalid old
criteria and failed candidate bodies are not migrated into new model inputs.
Old sessions, metrics and counters stay untouched; each new session points to
the previous file/hash/counters. This explicitly created round has its own
ten-initial-attempt-per-stage budget, bounded review-correction credits and
two-empty-batch stop counter. Restarting the
same output/arguments never seeds or resets it again. 401 passed tasks remain
passed and are never scheduled.

For a ten-task provider comparison, use the separate DeepSeek profile and a new
output directory. The profile reads `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` and
`DEEPSEEK_MODEL` from the configured environment file; credentials are never
written to the run records. `--task-limit 10` limits scheduling to the first ten
frozen queue entries and does not change the 800-task denominator.

Short feedback carries a code, exact item path, tool/read arguments and bounded
actual/expected values. Schema failures report at most three field errors to
the model. Complete structured diagnostics are stored once per failed attempt
in `diagnostics/`; previous feedback is never merged into the current error.

Fixed user requests are generated before their technical solutions. The
`fixed-user-requests-v2` policy allows references to earlier requested business
objects but forbids assuming a tested agent's clarification, recommendation,
answer wording or execution procedure. A follow-up must add a goal even when
the original request was completed directly. Courtesy words alone are not a
violation. Resolving the execution feasibility of the original goal alone is
not an added goal; calibration must get every positive/negative stage correct,
not merely reject a pair containing one obvious bad request. Question planning uses an initial-business-only reader; neither its
initial packet nor any retrieval API exposes the original reference dialogue,
answer text, execution results, author rationale or legacy review decisions.

`revalidate-series --dataset LEGACY --output NEW --config CONFIG` creates a
separate working version, freezes legacy-reviewed/unresolved IDs and hashes,
calibrates the semantic reviewer on clear positive and negative cases, then
audits all legacy-reviewed request pairs. The audit is blind to old rationale
and acceptance. Each stage gets a verdict, counterfactual checks and exact
offending quote; uncertain or inconsistent judgments get one independent
adjudication. Failed/uncertain/error cases join the original unresolved IDs in
the deduplicated `repair-queue.json`. Errors never count as passes. Run
`audit-questions --dataset NEW --config CONFIG` to resume only the full audit.

The workflow redoes task 0149 as a pilot. If it does not pass both new request
semantics, execution/replay and fresh quality review, batch repair stops while
the full audit remains available. Otherwise it processes batches of ten with
two repair workers using Key1/Key3, stopping after two batches without new acceptance.
Question audits use a separate measured capacity: 11 workers, with independent
request caps of 5 for Key1 and 6 for Key3. `RequestPolicy` accepts
`per_key_concurrency_overrides` for asymmetric caps; keys must belong to its pool.
Old passing text is retained but requires new policy approval and current
technical verification/review. Bad questions are regenerated separately from
technical patches; changing the first follow-up invalidates its successor.
Each affected stage has one persisted five-attempt session across planning
rejections and technical candidates. Historical counters are never cleared.

Outputs are `population.json`, `question-audit-report.json`, per-task
`question-audits/`, `question-audits.jsonl`, `repair-queue.json`, `repair-results/`
and `new-policy-coverage.json`. Legacy `reviewed` status alone never contributes
to the new acceptance count. Artifacts remain authoring-only and are not
published, registered, or automatically signed as human-approved data.

Each continuation needs a substantive related goal. At least one continuation must
use a previous result. Explicit, useful revisions are allowed. Empty repetition,
immediate pointless undo/redo, invented people/contact details/attachments, new
external replies, and new initial world data are rejected. User-specified control
parameters such as an email subject or a time adjustment are allowed.

Facts must belong to the current snapshot or a preceding result, and be reachable
through a public application read. Shared asset files are not automatically inputs
for every task. `Evidence` records both a JSON pointer to a source/checkpoint/result
and a public read whose selected output must equal the claimed fact. JSON pointers
follow RFC 6901 (`~0` for `~`, `~1` for `/`). Checkpoint 0 is the completed original;
1 and 2 are the completed continuations. Results use the original event IDs.

`StageDraft` contains the request, relationship, preconditions, evidence, native
reference events, native criteria, and deterministic outcome checks. Reference event
IDs start with `series_s1_` or `series_s2_`; the compiler inserts `series_sN_user`.
The original graph, including its existing clarification and environment events,
remains intact. Reference answers use `AgentUserInterface__send_message_to_user`
with `channel="agent"`. Generated scalar IDs use the native `{{event_id}}` syntax.
New environment events, private operations and new real-time waits are outside v1.

Reference format is supplied explicitly to both author and repair models as
`reference_format`, alongside the generated output schema. `{{event_id}}` is
expanded only inside event argument values and public-read argument values.
`ValueRef.event_id`, event IDs and dependencies are literal IDs. JSON pointers,
including evidence `result_pointer`, are static RFC 6901 paths and never expand
templates. Empty `pointer=""` selects an entire scalar result such as a created
object ID. To check that object's title, add a public getter using the returned
ID, then select `/title` from its actual object result (or `/0/title` if it returns
a list). A creation result containing only a string ID has no `/title` field.
Array indexes and object keys must come from observed material; escape `~` as
`~0` and `/` as `~1` inside pointer components. Invalid template placement is
rejected during parsing or compilation, before environment startup.

Checks support exact equality, membership, unordered collection equality, count,
sum, minimum and maximum. A second material reference can supply the expected
value. Every stage is checked before the next starts. A strict journal replay
must reproduce the whole final state. A separate model reviews whether the
reference actually solves the request and the evidence proves its claims.

## Commands

Use the existing `gaia2` and `dev` extras and `UV_FROZEN=1`. The same commands are
available as `python -m tau2.task_authoring.gaia2_series.cli` if the host's optional
voice libraries prevent the full `tau2` CLI from loading.

```bash
tau2 gaia2-series inventory --source data/tau2/domains/gaia2 \
  --output data/tau2/task_series/gaia2/v2-asr-hotwords
tau2 gaia2-series generate --dataset data/tau2/task_series/gaia2/v2-asr-hotwords \
  --config docs/gaia2-series.config.example.json --pilot-per-category 1
tau2 gaia2-series verify --dataset data/tau2/task_series/gaia2/v2-asr-hotwords \
  --pilot-per-category 1
tau2 gaia2-series review --dataset data/tau2/task_series/gaia2/v2-asr-hotwords \
  --config docs/gaia2-series.config.example.json --pilot-per-category 1
```

The current pilot is five tasks (one per category), all manually reviewed before
a larger run. `--pilot` retains the legacy 50-task sample for explicit use.
No scope filter selects all 800. Generation resumes checked stages and keeps failed
candidates/feedback. Each stage has three content attempts, including repairs;
provider failures are recorded separately and isolated to the current task.
Use `--stop-on-provider-error` only when whole-batch cancellation is desired.
Material retrieval has six rounds followed by a tools-disabled JSON response.
Unstructured commentary receives bounded structured-output correction. Calls are
persisted individually, including before a stage finishes. Rejected reviews feed
back into generation while preserving the original content-attempt budget.
Changed generation settings require a new output version. Review uses a separate
model call/context; it can use the same model family through a distinct profile.
Credentials remain environment references. API credentials and native raw API
request logs are not written into authoring provenance.

Empty/whitespace replies without tool calls are recorded as `empty_reply` but
never appended to conversation history. The authoring request receives at most
two additional JSON-only attempts using its last valid history, including on the
final structured-output turn. Repeated empty output is a candidate failure;
normal stage repair budgets still apply and it does not trigger provider failure
or concurrency reduction. Tool-only assistant messages remain valid.

The resumable pilot driver defaults to five tasks, removes proxy variables from
its own process, and runs generation, verification and independent review before
writing human review sheets. Completed tasks still reach review when another task
fails; the final exit status remains unsuccessful if any phase failed. The driver
never publishes data or signs human decisions. Its default runtime controls are
one worker, 600 seconds per request and at most two transient API attempts:

```bash
bash scripts/run_gaia2_series_pilot.sh
```

Runtime controls can also be supplied directly without changing the pinned
authoring configuration or discarding existing stages:

```bash
tau2 gaia2-series generate --dataset data/tau2/task_series/gaia2/v2-asr-hotwords \
  --config docs/gaia2-series.config.example.json --pilot \
  --concurrency 1 --request-timeout 600 --provider-attempts 2
```

Timeout, connection and transient server failures receive bounded retries;
authentication/configuration errors do not. The same controls apply to `review`.
For MiniMax profiles, optional `MINIMAX_API_KEY2` and `MINIMAX_BASE_URL2` in the
private config file provide a secondary route for explicit plan/quota exhaustion.
The model and request conversation are preserved. Switching routes does not spend
a transient retry, even when the configured retry count is one. A process remembers
exhausted routes across workers; if both routes exhaust quota, no new requests are
sent to them and the batch stops. Successful calls record the actual endpoint and
route in provenance; credentials are never stored there. Ordinary rate limiting
and connection failures do not trigger this quota-specific route switch.
Per-call records include UTC start time, elapsed seconds, timeout, retrieval turn,
provider attempt and request size. `SERIES_CONCURRENCY`, `SERIES_REQUEST_TIMEOUT`
and `SERIES_PROVIDER_ATTEMPTS` override the pilot driver's runtime defaults.

On this HPC host, the driver also supplies `.venv/lib/tau-native` in the library
path. Use the same library path when running other commands if PortAudio reports
missing native libraries. A dataset cannot be owned by two mutating commands at
the same time; status/read-only inspection remains available during generation.

All source bytes, resources and runtime pins are checked before commands. Outputs
must live outside the source tree. Authoring code is outside the runtime's pinned
Gaia2 directory, so adding this pipeline does not require resealing original data.

## Human review and export

`review-sheet` writes `review.md` and `human-review-template.jsonl`. Templates have
blank reviewer names and `PENDING` decisions; they cannot be imported as approvals.
Review all five current pilot series before explicitly starting a larger run.
The legacy release gate still requires its fixed 50-series and additional 75-series
samples, plus flagged items; changing that later release policy is separate work.
In a separate decision file, supply a reviewer identity and
`accepted` or `rejected`, keeping the exact `content_sha256`. Edits invalidate prior
signatures. A changed first continuation also requires checking the second again.
Exact repeated request/target/outcome combinations on the same snapshot are
reported in `duplicates.json` and require additional human review.

```bash
tau2 gaia2-series import-review --dataset data/tau2/task_series/gaia2/v2-asr-hotwords \
  --decisions /path/to/completed-human-decisions.jsonl
tau2 gaia2-series status --dataset data/tau2/task_series/gaia2/v2-asr-hotwords
tau2 gaia2-series export --dataset data/tau2/task_series/gaia2/v2-asr-hotwords
```

Full export independently re-executes the current candidates and requires 800
verified series, accepted independent model reviews and all required human
signatures. Missing tasks remain in `coverage.json` with denominator 800. For
development, `export --allow-partial` writes an explicitly incomplete
`draft-export/`; it never marks the dataset frozen. Full exports use `release/`
and freeze further authoring. Original source data remains untouched.

The release includes `tasks.json` (three stages), `tasks-2.json` (the exact two-stage
prefixes), `stages.jsonl`, category splits and hashes. Stage-scoped criteria in the
sidecar are authoritative for temporal attribution: native aggregate communication
checks alone do not establish which stage an answer satisfied. No live agent
accuracy is measured by authoring/reference verification.

## Repairing failed candidates

Authoring now submits stages, repairs and reviews through dedicated
`submit_stage`, `submit_repair` and `submit_review` tools. Repair submissions are
content-bound `RepairPatch` objects containing a candidate SHA-256, replacement
paths/values and a reason. Existing paths under `events`, `evidence`, `checks`,
`criteria`, `relation` and `preconditions` may be replaced together;
only user requests and verified prefixes are locked. Repairs can correct illegal
action/environment criteria and consolidate incorrect or redundant checks; a
smaller check count alone is not a rejection. Business coverage is still checked
by execution and independent review. The host assembles and validates the stage.
Plain text and echoed input
packets are not submissions. After at most six material retrieval rounds, one
format correction is available without restarting retrieval. Final submissions
request `tool_choice=required`; a provider explicitly rejecting that option may
fall back once to `auto` with only the submission tool, still validated locally.

For a dictionary keyed by a newly created ID, a state reference may specify
`key_from_result={"event_id":"series_s1_create","pointer":""}` and
`suffix_pointer="/participant_ids"`, with `pointer="/Chats/conversations"`.
The evaluator reads the actual string ID, selects that dictionary member and
then follows the suffix. Ordinary pointers never expand templates. Unknown,
future, non-string or absent keys fail explicitly; existing references retain
their serialization and hashes.

Use one isolated MiMo acceptance session before any batch restart:

```bash
python -m tau2.task_authoring.gaia2_series.cli repair-acceptance \
  --dataset data/tau2/task_series/gaia2/v1-full-mimo2 \
  --output /path/to/new-acceptance-output \
  --task-id TASK_ID \
  --config docs/gaia2-series.mimo.config.json
```

The command copies one unresolved record and its index/journal, retains old
counters and history, and records a separate session with a configurable
content-attempt limit per stage (`--content-attempts`, default 5, maximum 10).
Defaults are one worker, Key1/Key3, 180 seconds per API
request, at most two API attempts and a 15-minute session deadline. Requests
are capped by the remaining session time; no additional phase starts after
the deadline. Both continuations must verify/replay and pass independent review
for `scope_complete=true`. The original work-record hashes are checked before
and after. `dataset_complete` and `release` remain false. A failed acceptance
exits unsuccessfully and writes its exact stop reason and final diagnostics.

Feedback carries the latest submitted/assembled candidate, raw output and
candidate hash even when parsing fails. An invalid patch is never accepted as
a valid stage. The historical configuration identity is retained in provenance;
acceptance uses the new protocol identity only in its isolated copy.

Normal generation filters exhausted stage budgets before scheduling and checks
them again before reconstructing the reference environment. Exhausted tasks remain
in the repair queue with their original counters and candidate history; resuming
does not replay them just to discover that no normal attempt is available.

Evidence reads use an isolated reconstruction of the completed checkpoint, so
their observations cannot bind later reference operations or alter the main journal.
Validation failures return the failed candidate, failing pointer or operation,
expected/actual values, and bounded public-result excerpts to a repair prompt.
Repairs preserve intent and the verified prefix. Replacing an impossible goal
requires a reason and material/tool evidence. Verification-system errors stop the
task instead of causing the model to rewrite it.

```bash
tau2 gaia2-series repair --dataset /path/to/series --config /path/to/config.json \
  --task-ids TASK_ID_1 TASK_ID_2 --concurrency 1
```

Explicit IDs are required. The last failed candidate is revalidated first; if a
verifier fix resolves it, it is restored without model calls. Otherwise a failed
stage receives at most three additional repairs, tracked separately from its
original attempts. Restarting resets neither budget. Provider failures do not
spend content attempts. Independent and human review remain required.

Recovery also checks repair and recovery budgets before reconstructing the
environment. Exhausted tasks retain their drafts, counters and failure history,
with a `recovery_stop` reason for targeted intervention. Identical validation
errors for the same stage and completed prefix stop after `max_attempts`
occurrences (three by default), even if a larger recovery budget remains. API
failures do not contribute to this limit. An unchanged rejected review enters
repair rather than being treated as a successful or newly attempted review.

Within a recovery run, unchanged candidates or a fully blocked repair batch stop
further review cycles. The recovery supervisor stops after a full pass with no
new reviewed tasks or additional completed stages; it does not automatically
increase attempt budgets. `recovery-run.json` records `progress`, `made_progress`,
`blocked_tasks` and `unresolved_tasks`. A stopped run remains `needs_attention`
until every task has two stages and an accepted review; stopping is not completion.

`requests.md` contains concise requests and reference replies; `review.md` has
full candidates and review records. Neither file constitutes human approval.

## Full population authoring

The active four-key MiMo run uses fixed capacity: `SERIES_FIXED_CONCURRENCY=1`
passes `--fixed-concurrency`. With 24 task workers and four keys capped at six
requests each, ordinary 500/502 and upstream timeouts receive bounded retries
without reducing concurrency. Explicit ordinary 429 rate limits apply a shared
five-second cooldown to the affected credential only. Quota failover and terminal
authentication/access errors retain their existing stop behavior. Fixed mode is
stored as `adaptive_concurrency=false` in the operational request policy.

The full driver operates on a clean dataset. Import only the explicitly approved
pilot records; old partial runs are not copied. Approval files use the existing
content-bound `HumanDecision` JSONL format:

```bash
python scripts/prepare_gaia2_full.py --pilot data/tau2/task_series/gaia2/v1-repair \
  --output data/tau2/task_series/gaia2/v1-full \
  --decisions data/tau2/task_series/gaia2/v1-repair/pilot-human-decisions.jsonl
tau2 gaia2-series full --dataset data/tau2/task_series/gaia2/v1-full \
  --config docs/gaia2-series.config.example.json \
  --start-concurrency 8 --max-concurrency 16
```

`bash scripts/run_gaia2_series_full.sh` supplies direct connections, 600-second
request timeouts and two transient request attempts. Use the frozen runtime's
`PYTHONPATH` and `TAU2_DATA_DIR` when running an isolated snapshot.
`SERIES_START_CONCURRENCY` and `SERIES_MAX_CONCURRENCY` override the launcher's
8/16 defaults. The full runner supports up to 128 task workers and provisions a
synchronous HTTP pool with twice the configured maximum worker count. The active
run starts at 32 and caps recovery at 32. Health decisions after resume use only
new requests from that session, with persisted window state preventing recounts.

The 2026-10-28 secondary-endpoint short-request probe measured 31/32 successful
requests at 32 concurrency, 46/64 at 64, and 32/32 on a repeated 32-worker wave.
The 64-worker wave had connection timeouts and read errors, so the active run uses
32 workers and a 64-connection pool. These probes test API capacity; complete-task
generation, tool execution, repair and review throughput must be measured separately.
Measurements are retained as `concurrency-probe.json` and
`concurrency-probe-repeat.json`. The reusable probe is
`scripts/probe_gaia2_concurrency.py`; its requests only summarize source questions
and do not change benchmark tasks.

Tasks are shuffled reproducibly and interleaved across categories in batches of
50. Concurrency uses nonoverlapping windows of 100 completed requests on the
active endpoint. Two consecutive windows with more than 5% capacity errors halve
workers (32 to 16 to 8); two with at most 1% recover one step, up to the configured
maximum. Request health is checked every 30 seconds inside the active batch;
the scheduler polls at most every 15 seconds while waiting for a task to finish.
An increase immediately fills available task slots. A reduction drains existing
tasks before starting replacements, without cancelling model calls. Changes have
a 60-second cooldown; batching no longer delays recovery until all 50 tasks finish.
At eight workers persistent pressure produces a recorded warning. Latency, model
JSON errors, candidate rejection rates and local verification failures never
reduce concurrency. Quota errors use endpoint failover, resetting route windows.
Non-transient API/configuration failures and exhausted quota on both routes stop
the run; isolated environment/replay failures leave tasks flagged for inspection.
Adjustment history is stored in `concurrency-decisions.json` and `full-run.json`.

For MiMo accounts with independent per-key limits, the full command accepts
`--api-key-pool MIMO_API_KEY MIMO_API_KEY2 --per-key-concurrency 6`.
Use twelve task workers to allow both keys to run six requests simultaneously.
The launcher reads the same options from `SERIES_API_KEY_POOL` (space-separated
variable names) and `SERIES_PER_KEY_CONCURRENCY`. A single writer owns the dataset
and distributes conversations round-robin across the keys; each actual model
request acquires its key's independent semaphore. Generation, repair and review
share those caps. Keys must resolve to distinct values. Request provenance records
only `api_key_env`, never the credential. This operational change preserves task
content and existing attempt budgets.

Errors have only two operational classes: candidate failures return the saved
candidate (or raw reply), phase, location and relevant actual data to the model;
system failures from trusted resource/environment operations remain isolated or
stop a globally unusable run. A bad check expression, including dictionary object
membership, is a candidate failure without changing the check language's semantics.
The two classes retain existing task statuses for backward compatibility. Pending
candidates are saved before verification and retry budgets survive restarts.

Generation is followed by bounded content repair, provider recovery, full offline
verification, independent model review and bounded repair of rejected stages.
Provider recovery rounds are persisted and capped at three per task and role.
Content budgets remain three original attempts plus three extra repair attempts
per stage. Changing a predecessor invalidates later-stage candidates. Approved
pilot requests are never regenerated automatically.

`full-run.json`, `progress.json` and `batches/` retain run state, five-minute
progress reports and individual batch results. Restart the same command to resume.
Pinning checks reject a changed implementation or run policy. No two mutating
commands may own the dataset simultaneously.

The full dataset uses the approved `pilot_plus_anomalies` human policy instead of
the legacy 50+75 sampling rule. Provider retries, failed candidates, repairs,
review rejections, explicit material warnings, and exact or near-duplicate task
groups require human review even if the final automatic checks pass. Near-duplicate
screening compares tasks with the same snapshot and tool sequence using a request
similarity threshold of 0.9; similarity flags require inspection, not automatic rejection.
The driver writes `anomalies.json`, `anomaly-review-template.jsonl`, `coverage.json`
and all-task reading sheets. It stops at `awaiting_human_review` or `needs_attention`;
it never exports, freezes, registers, or runs an agent benchmark. Explicit export
still re-executes references and requires content-bound human approvals for all
required tasks.

## Listener ASR runtime binding update

The automatic listener-hotword update resealed Gaia2 after all 800 reference
executions and strict replays passed. The previous `v1`, `v1-examples` and
`v1-initial-attempt` directories are retained unchanged as historical versions.
Their successors are `v2-asr-hotwords`, `v2-asr-hotwords-examples` and
`v2-asr-hotwords-initial-attempt`. Eight existing drafts were rebound without
changing their authored content, then reference-executed and strictly replayed.
Prior model reviews remain in `previous_binding_review`; new bindings require
fresh review. No new authoring or review model calls were made during migration.
The pilot launcher defaults to the new `v2-asr-hotwords` inventory.
