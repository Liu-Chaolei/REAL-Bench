"""Contextual rendering of immutable later-stage scenario goals."""

from __future__ import annotations

import json
import re

from tau2.data_model.dynamic_followup import (
    DynamicFollowupCandidate,
    DynamicFollowupConfig,
    DynamicFollowupContext,
    DynamicFollowupVerification,
)
from tau2.data_model.message import SystemMessage, UserMessage
from tau2.utils.llm_utils import extract_json_from_llm_response, generate


class DynamicFollowupError(ValueError):
    """The model did not produce a safe contextual rendering."""


_PROTECTED_VALUE = re.compile(
    r"(?:\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\+?\d[\d() .:/-]{2,}\d|"
    r"[\"“‘]([^\"”’]+)[\"”’])"
)


def infer_status_affect(messages, released_facts: list[str]) -> tuple[str, str]:
    """Infer a small, grounded context label without another model call.

    The labels describe the preceding interaction only. They are intentionally
    coarse: task correctness remains determined by the source request and
    tool evidence, while these labels only guide conversational realization.
    """
    # Scripted requests in released_facts describe goals, not outcomes. Use
    # only the tail of the live dialogue so old prohibitions and negations do
    # not contaminate the current stage's status.
    user_turns = [
        str(getattr(message, "content", "") or "")
        for message in messages
        if getattr(message, "role", None) == "user"
    ]
    recent_messages = list(messages)[-12:]
    recent_text = "\n".join(
        str(getattr(message, "content", "") or "") for message in recent_messages
    ).casefold()
    recent_users = "\n".join(user_turns[-3:]).casefold()
    last_user = user_turns[-1].casefold() if user_turns else ""

    if re.search(r"\b(?:goodbye|try another way|i'm done|i am done)\b", last_user):
        status = "abandoned"
    elif re.search(
        r"\b(?:everything(?:'s| is) done|all(?: of that)? went through|"
        r"it(?:'s| is) out|has been sent|that's everything|all set)\b",
        last_user,
    ):
        status = "completed"
    elif re.search(
        r"\b(?:hold off|leave .* unsent|still unsent|not yet|still waiting|nothing|"
        r"don't guess|do not guess|unconfirmed)\b",
        last_user,
    ):
        status = "partial"
    elif re.search(r"\b(?:timed out|timeout|time limit exceeded)\b", recent_text):
        status = "timed_out"
    elif re.search(
        r"\b(?:failed|failure|rejected|unable|couldn't|cannot|can't|no luck)\b",
        recent_text,
    ):
        status = "failed"
    elif re.search(
        r"\b(?:still running|not yet|pending|waiting|unconfirmed|almost there)\b",
        recent_text,
    ):
        status = "incomplete"
    elif re.search(
        r"\b(?:sent|created|saved|completed|done|went through|all set|"
        r"successfully)\b",
        recent_text,
    ):
        status = "completed"
    else:
        status = "unknown"

    if re.search(
        r"\b(?:great|thanks|thank you|appreciate|all sounds good|everything)\b",
        last_user,
    ) and not re.search(r"\b(?:wrong|don't|do not|unsure|hold off)\b", last_user):
        affect = "relieved/positive"
    elif re.search(
        r"\b(?:ridiculous|forever|taking too long|still waiting|hurry|"
        r"what(?:'s| is) going on|just get)\b",
        recent_users,
    ):
        affect = "frustrated/impatient"
    elif re.search(
        r"\b(?:make sure|don't want|do not want|wrong|confirm|hold off|"
        r"safest|careful|double-check|don't guess|do not guess)\b",
        recent_users,
    ):
        affect = "cautious"
    else:
        affect = "neutral"
    return status, affect


def _dialogue_context(messages, limit: int) -> str:
    """Serialize only user/agent dialogue for the caller-belief classifier."""
    dialogue = [
        message
        for message in messages
        if getattr(message, "role", None) in {"user", "assistant"}
        and getattr(message, "content", None)
    ]
    return _compact_context(dialogue, limit)


def classify_status_affect(
    *,
    model: str,
    model_args: dict,
    conversation: list,
    max_context_characters: int = 24000,
):
    """Classify the caller's perceived prior-stage outcome and emotional stance.

    This deliberately receives only the user/agent transcript. It must infer
    what the caller thinks happened, rather than determine objective tool or
    environment correctness.
    """
    dialogue = _dialogue_context(conversation, max_context_characters)
    system = """
You classify the caller's belief about the preceding task in a voice
conversation. Use only the USER and AGENT dialogue below. Do not use tool GT,
environment state, hidden facts, or the benchmark source task. The question is
what the caller appears to believe, not whether the operation actually
succeeded.

Choose exactly one status:
- completed: the caller explicitly accepts the result or says the work is done;
- partial: the caller accepts some work but says another part is unresolved;
- incomplete: the caller is still waiting or has no result yet;
- failed: the caller believes the attempt failed, was rejected, or produced a
  wrong result;
- timed_out: the caller explicitly treats the attempt as timed out or beyond a
  stated time limit;
- abandoned: the caller gives up, withdraws the request, or ends because the
  result was not obtained;
- unknown: the dialogue does not establish the caller's belief.

Resolve conflicts using the latest clear caller statement. A goodbye after a
confirmed successful result is completed; a goodbye while still waiting is
abandoned. Do not infer completion only from an agent claim if the caller asks
whether it really happened or says it is still unresolved. Do not treat an
agent's emotional language as the caller's affect.

Describe the caller's affect in a short phrase of at most a few words. Do not
choose from a closed list. Examples include "relieved but still cautious",
"frustrated and impatient", "neutral and matter-of-fact", and "excited and
optimistic"; these are examples, not an exhaustive vocabulary. Infer the
phrase from the caller's wording, with the latest caller turns strongest. Do
not add a fact or diagnose a mental state. Return JSON only with status, affect,
rationale, and evidence. Evidence must be short paraphrases or short
transcript snippets, not new facts.
""".strip()
    user = f"""
USER/AGENT DIALOGUE:
{dialogue or "(no dialogue)"}
""".strip()
    args = dict(model_args)
    # Classification should be reproducible even when the rendering call uses
    # a higher temperature to vary the caller's wording.
    args["temperature"] = 0
    reply = generate(
        model=model,
        messages=[
            SystemMessage(role="system", content=system),
            UserMessage(role="user", content=user),
        ],
        call_name="dynamic_followup_context",
        response_model=DynamicFollowupContext,
        response_validation_retries=1,
        **args,
    )
    context = DynamicFollowupContext.model_validate_json(
        extract_json_from_llm_response(reply.content or "")
    )
    return context, reply


def _compact_context(messages, limit: int) -> str:
    rows = []
    total = 0
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if not content:
            continue
        row = f"{message.role}: {content}"
        if total + len(row) > limit:
            break
        rows.append(row)
        total += len(row)
    return "\n".join(reversed(rows))


def _protected_values(text: str) -> list[str]:
    values = []
    for match in _PROTECTED_VALUE.finditer(text):
        value = match.group(1) or match.group(0)
        value = " ".join(value.split())
        if len(value) >= 3 and value not in values:
            values.append(value)
    return values


def _contains_protected_value(utterance: str, value: str) -> bool:
    compact_utterance = " ".join(utterance.split()).casefold()
    compact_value = " ".join(value.split()).casefold()
    if compact_value in compact_utterance:
        return True
    # Speech-friendly spacing and punctuation may differ, but the requested
    # alphanumeric value must remain present.
    tokens = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
    spoken = re.findall(r"[^\W_]+", utterance, flags=re.UNICODE)
    return bool(tokens) and all(
        token.casefold() in [x.casefold() for x in spoken] for token in tokens
    )


def generate_dynamic_followup(
    *,
    model: str,
    model_args: dict,
    stage: int,
    source_request: str,
    conversation: list,
    released_facts: list[str],
    config: DynamicFollowupConfig,
    tool_call_gt: list[dict] | None = None,
    stage_status: str | None = None,
    affect: str | None = None,
):
    """Render one fixed stage goal using the preceding live conversation."""
    context = _compact_context(conversation, config.max_context_characters)
    facts = "\n".join(released_facts[-8:])
    inferred_status, inferred_affect = infer_status_affect(conversation, released_facts)
    stage_status = stage_status or inferred_status
    affect = affect or inferred_affect
    tool_gt = json.dumps(
        tool_call_gt or [], ensure_ascii=False, indent=2, sort_keys=True
    )
    system = """
You render a later request in a benchmark conversation. The SOURCE REQUEST is
the immutable business goal and is authoritative. Write one natural user
utterance that asks for every requirement in that source request, while using
the preceding conversation and the supplied status and affect to avoid
repetition, create a natural transition, and refer naturally to objects already
created or discussed.

Rules:
- Preserve every business requirement, exact recipient, title, filename, date,
  time, quantity, amount and prohibition from SOURCE REQUEST.
- Do not invent a new business goal, preference, entity, deadline or tool task.
- Do not claim an operation succeeded unless the conversation or tool result
  shows it succeeded.
- Adjust the utterance's tone and transition wording using the supplied status
  and affect, while always focusing on the CURRENT SOURCE REQUEST.
- Use PREVIOUS STAGE STATUS and CALLER AFFECT to choose the opening, emotional
  stance, politeness, urgency, and transitions. When the current request is a
  natural follow-up, include one brief context-aware transition instead of
  presenting it as an isolated command. If earlier work is partial, incomplete,
  failed, timed out, or abandoned, acknowledge that only briefly when useful, then move
  directly to the CURRENT SOURCE REQUEST; do not reopen it, retry it, or repair
  the earlier task. Treat partial and incomplete work the same way for this
  purpose. If status is unknown, use a neutral transition.
- When status is partial, incomplete, failed, timed_out, or abandoned, do not
  say that an unresolved earlier item is now fixed merely because the CURRENT
  SOURCE REQUEST contains a new parameter or exact value. Use that value for
  the current request, but do not narrate it as a prior-stage success unless
  the dialogue explicitly confirms that success.
- Affect changes expression only. Do not invent a new fact, preference,
  outcome, relationship, deadline, or task to make the utterance more emotional.
- Do not copy the SOURCE REQUEST sentence structure. Vary the opening, clause
  grouping, transitions, and sentence length while preserving the requirements.
- If SOURCE REQUEST uses a pronoun or deictic reference to an earlier-stage
  object or action, preserve that cross-stage reference and its antecedent.
  Examples include "that same chat", "the renamed group", "this contact",
  "those listings", and "the event". Do not silently turn such a reference
  into an unrelated standalone object or drop the relationship to the earlier
  stage. You may make the antecedent clear with a natural phrase, but the
  generated request must still refer to the same earlier object or action.
- Tool results and earlier agent messages may guide whether an operation
  succeeded, but they are not additional caller facts. Do not reveal an answer,
  internal ID, database field, contact detail, tool argument, or derived value
  that is absent from SOURCE REQUEST. Keep such information as a natural
  reference when needed (for example, "that chat" or "the address you found")
  instead of spelling out the hidden value.
- "covered_requirements" must list the substantive requirements represented in
  the utterance. "new_requirements" must be empty.
- Return JSON only with utterance, covered_requirements, referenced_entities,
  and new_requirements.
""".strip()
    user = f"""
STAGE: {stage}
PREVIOUS STAGE STATUS: {stage_status}
CALLER AFFECT: {affect}
SOURCE REQUEST (immutable goal):
{source_request}

RELEASED FACTS FROM EARLIER STAGES:
{facts or "(none)"}

ACTUAL CONVERSATION AND TOOL OBSERVATIONS:
{context or "(none)"}
""".strip()
    verification_system = """
You are an independent semantic checker for a generated later-stage caller
request. Compare the SOURCE REQUEST, the GENERATED REQUEST, and the EXPECTED
TOOL CALL GT below.

Rules:
- The SOURCE REQUEST is the authoritative business goal. Every substantive
  requirement must remain in the GENERATED REQUEST.
- The GENERATED REQUEST must not add a new business goal, preference, entity,
  deadline, quantity, amount, prohibition, or tool task.
- The GENERATED REQUEST must not expose any answer, internal ID, database
  field, contact detail, tool argument, or derived value that appears only in
  the conversation or EXPECTED TOOL CALL GT and is absent from SOURCE REQUEST.
  A generic reference such as "that chat" or "the address you found" is fine;
  spelling out the hidden value is not.
- Use EXPECTED TOOL CALL GT to identify the backend operations and argument
  semantics required by the source goal. It is evaluation evidence, not text
  that the caller must recite.
- Internal IDs and implementation details in the GT do not need to appear in
  the caller's wording unless the SOURCE REQUEST explicitly requires them.
- Natural references to objects already created or discussed are valid when
  supported by the conversation. Do not reject differences in case,
  punctuation, spacing, or conversational phrasing alone.
- When the SOURCE REQUEST contains a cross-stage pronoun or deictic reference,
  require the GENERATED REQUEST to preserve the same antecedent relationship;
  a paraphrase that loses "same", "that", "this", or the relevant prior
  object is a missing requirement even if the standalone action sounds valid.
- Do not reject a change in tone or transition wording that accurately reflects
  verified earlier-stage status, as long as the generated request stays focused
  on SOURCE REQUEST. In particular, a failed or incomplete earlier stage must
  not cause the generated request to reopen that stage or add repair work.
- If the context status is partial, incomplete, failed, timed_out, or abandoned,
  reject wording that claims an unresolved earlier item became fixed solely
  because its value appears in the current source request. The value may still
  be used as a current-stage parameter.
- Treat every generated claim about earlier-stage completion, failure,
  identity, sent status, created objects, resolved uncertainty, or prior
  outcomes as a factual claim. Check each one against the USER/AGENT HISTORY.
  Do not use a value that appears only in the current SOURCE REQUEST as
  evidence that the earlier stage succeeded. A natural transition is allowed
  only when its historical implication is supported by the history.
- Put every unsupported or contradicted historical claim in
  unsupported_historical_claims and set valid=false. Use an empty list when
  all historical claims are supported or the generated request makes none.
- Set valid=false if any source requirement is missing, any new requirement is
  introduced, a dependency or historical claim is unsupported, the requested
  backend operations materially disagree with the GT, or information is leaked
  from the GT/conversation. Otherwise set valid=true.
- Return JSON only with valid, reason, missing_requirements,
  invented_requirements, missing_tools, extra_tools, leaked_information, and
  unsupported_historical_claims.
""".strip()
    messages = [
        SystemMessage(role="system", content=system),
        UserMessage(role="user", content=user),
    ]
    last_error = None
    for attempt in range(config.max_retries + 1):
        try:
            reply = generate(
                model=model,
                messages=messages,
                call_name="dynamic_followup_request",
                response_model=DynamicFollowupCandidate,
                response_validation_retries=0,
                **model_args,
            )
            candidate = DynamicFollowupCandidate.model_validate_json(
                extract_json_from_llm_response(reply.content or "")
            )
            if not candidate.covered_requirements or candidate.new_requirements:
                raise DynamicFollowupError(
                    "dynamic request changed or omitted its source goal"
                )
            missing = [
                value
                for value in _protected_values(source_request)
                if not _contains_protected_value(candidate.utterance, value)
            ]
            if missing:
                raise DynamicFollowupError(
                    "dynamic request dropped protected source values: "
                    + ", ".join(missing)
                )
            # Historical claims need more context than generation. Keep a
            # larger transcript window for the checker so early evidence is not
            # silently dropped from the factuality check.
            verification_context = _compact_context(conversation, 100000)
            verification_user = f"""
SOURCE REQUEST (immutable goal):
{source_request}

GENERATED REQUEST:
{candidate.utterance}

RELEASED FACTS FROM EARLIER STAGES:
{facts or "(none)"}

USER/AGENT HISTORY AND TOOL OBSERVATIONS:
{verification_context or "(none)"}

EXPECTED TOOL CALL GT (reference operations; may contain internal IDs):
{tool_gt}
""".strip()
            verification_reply = generate(
                model=model,
                messages=[
                    SystemMessage(role="system", content=verification_system),
                    UserMessage(role="user", content=verification_user),
                ],
                call_name="dynamic_followup_check",
                response_model=DynamicFollowupVerification,
                response_validation_retries=1,
                **model_args,
            )
            verification = DynamicFollowupVerification.model_validate_json(
                extract_json_from_llm_response(verification_reply.content or "")
            )
            if (
                not verification.valid
                or verification.missing_requirements
                or verification.invented_requirements
                or verification.missing_tools
                or verification.extra_tools
                or verification.leaked_information
                or verification.unsupported_historical_claims
            ):
                detail = verification.reason or "semantic verification failed"
                raise DynamicFollowupError(
                    "dynamic request failed independent verification: " + detail
                )
            return candidate, reply, verification, verification_reply
        except Exception as error:
            last_error = error
            if attempt == config.max_retries:
                raise
            messages.append(
                SystemMessage(
                    role="system",
                    content=(
                        "The previous candidate was rejected. Regenerate it while "
                        "preserving every requirement from SOURCE REQUEST and adding "
                        "no new requirement. The validation error was: "
                        + type(error).__name__
                    ),
                )
            )
    raise DynamicFollowupError(str(last_error))
