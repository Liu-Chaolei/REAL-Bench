# State based interaction evaluation plan

> Implemented as `tau-state-interaction/2`. See [implementation record](state-interaction-implementation-plan.md) for prefix-only semantics, component visibility, configuration and local test scope. This document retains the original design rationale.

> Revised design note
>
> The state manager and metric boundaries in this note supersede the earlier
> split into new_fact_incorporation, state_revision, action_fit and
> claim_fidelity. The implementation should keep one generic state projector
> and report only Progressivity and Grounding.

## Revised design direction

The implementation is based on the Hidden Information State and agenda based
ideas from Steve Young's dialogue system work. The state combines the user goal,
the latest dialogue act and dialogue history. Uncertainty is represented by
candidate values and evidence instead of an early forced choice. Agenda items
represent pending dialogue or task work. See
[Young et al., 2010](https://s3.amazonaws.com/mairesse/research/papers/ygkm09.pdf),
[Young et al., 2013](https://mi.eng.cam.ac.uk/~sjy/papers/ygtw13.pdf) and
[Schatzmann et al., 2007](https://aclanthology.org/N07-2038.pdf).

The core state is generic:

~~~text
goal
agenda
facts and candidates
constraints
operations and results
dialogue history
visibility
uncertainty
~~~

The domain adapter supplies operation schemas, state effects and completion
conditions. It does not implement the projector. A reference action list can
describe capabilities and possible effects, but it does not define the only
valid Agent trajectory.

Each state item is an object with an id, kind, value, status, source references,
visibility time and last update time. The main lifecycles are:

~~~text
facts:
    unknown -> mentioned -> grounded -> denied / stale / conflicted

agenda items:
    open -> in_progress -> satisfied / blocked / cancelled

operations:
    not_started -> requested -> accepted -> running
    running -> succeeded / failed / cancelled
~~~

The projector stores event facts and authoritative tool or backend facts in
separate ledgers. An Agent claim can be contradicted by a tool result, but it
cannot overwrite that result.

## Window and transition rules

An Agent response remains the window anchor. A window contains the user events
since the preceding Agent response, the Agent's tool calls, their results, and
the Agent response that closes the window.

Text runs must use the complete message order and tool call identifiers. They
must not compare message indices with runtime seconds. Voice runs use the
communication boundary and preserve media timing.

The projector keeps snapshots at the points that matter:

~~~text
state_before_agent
state_before_tool_action
state_before_agent_reply
state_after_window
~~~

Later evidence cannot be used to justify an earlier Agent claim.

The initial agenda contains a one time establish_contact item. A necessary
first greeting can satisfy that item and count as one interaction advancement.
Later greetings do not create another advancement.

The Delta field satisfied_items contains only state edges:

~~~text
before.status != "satisfied"
and after.status == "satisfied"
~~~

An operation creates a satisfied item only when its domain adapter says that
the operation result meets the item's completion condition. A successful query
does not automatically satisfy every reservation or account item.

## Revised metrics

### Progressivity

Progressivity is the overall result of a window. It asks whether the Agent
advanced the task or completed necessary interaction work.

The trace records:

~~~text
forward
stagnated
regressed
externally_blocked
~~~

Reports may break forward into two explanatory counts:

~~~text
goal_progress
    an agenda item, goal constraint or operation dependency moved forward

interaction_progress
    a necessary greeting, answer, clarification, explanation, result
    delivery or next step was completed
~~~

These are breakdowns of Progressivity, not additional top level metrics.
Repeated satisfied items do not count again. External waiting is reported
separately from Agent stagnation.

### Grounding

Grounding is one metric with one primary question:

> Did the Agent's reply or action correctly use the state that was visible at
> that time?

The state-use assessment records the Agent event, the relevant state references,
the behavior mode and evidence references:

~~~text
behavior_mode:
    reply / question / tool_action / commitment

status:
    consistent / inconsistent / unsupported / uncertain / not_applicable
~~~

Grounding covers both dialogue-state use and evidence use. It does not create
separate headline metrics for Task alignment, Action fit or Claim fidelity.
Those concepts become reason codes:

~~~text
stale_state_use
ignored_constraint
unsupported_claim
contradicted_claim
irrelevant_action
invalid_precondition
wrong_entity
missing_required_state
~~~

The denominator is an actual state-use opportunity. An Agent that is discussing
attachments has no recipient-state opportunity. It is not scored as having
forgotten the recipient.

The state manager writing a fact is not evidence that the Agent understood it.
Grounding is scored from the Agent's later reply, question, tool arguments or
commitment against the state snapshot and visible evidence at that point.

## Implementation consequences

The original implementation required the following changes; the v2 implementation
addresses these mechanisms (semantic accuracy still depends on the configured model):

1. Keep the full message order and tool call ids when building windows.
2. Add the generic agenda and explicit completion conditions.
3. Remove new_fact_incorporation and state_revision from Agent scoring.
4. Replace token overlap in action fit with state-use opportunities.
5. Treat claim fidelity as a Grounding reason code.
6. Ignore repeated satisfied statuses when calculating a Delta.
7. Keep greeting as the one-time establish_contact progress event.

The automatic run path remains the same:

~~~text
EvidenceRecorder
-> event normalization
-> state projection
-> Progressivity
-> Grounding
-> state_trace.json and state_interaction.json
~~~

## Lightweight checks

The checks should verify the mechanism rather than enumerate every possible
conversation:

- structured instructions initialize goal and known facts;
- tool results attach to the correct Agent window;
- satisfied items are emitted only on the unsatisfied-to-satisfied edge;
- a first greeting satisfies establish_contact once;
- a reply using a current fact receives a consistent Grounding result;
- a stale or contradicted tool argument receives an inconsistent result;
- a claim unsupported by authoritative evidence remains unsupported;
- external waiting is not counted as Agent failure.

The saved DeepSeek traces remain useful as regression fixtures, but no large
live acceptance campaign is required for this mechanism.
