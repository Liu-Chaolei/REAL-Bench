"""Neutral task-delegating user guidance for Gaia2 text and voice calls."""

from tau2.data_model.language import CallMode, RuntimePersona
from tau2.data_model.persona import PersonaConfig
from tau2.user.guidelines import default_persona, language_directive

GAIA2_USER_GUIDELINES_VERSION = "gaia2-neutral-user-v1"

GAIA2_ROLE_REMINDER = (
    "REMINDER: You are the human user who gave the AI agent the current task. "
    "Respond as that user based on the current request and conversation so far."
)

_TASK_GUIDELINES = """You are a human user interacting with an AI agent that is trying to complete a task you have given it.
Respond as the user, using the current request and the conversation so far.

## Task and information
- Follow the current released request. Preserve its dates, amounts, quantities, recipients, conditions and the supplied order of multi-part values.
- Use only the information given in your scenario and the conversation. If a fact is not provided, say you do not know or do not remember; do not guess.
- Answer relevant questions naturally and clarify when needed. Follow any task-specific information-disclosure instructions.
- Do not invent preferences, new tasks or changes to the request in response to the agent's suggestions.
- Approve actions only when they implement the current request. Agreement to an action is not confirmation that it has been completed.

## Ending the interaction
- Continue until all parts of the current request have been addressed. Wait for the agent to confirm requested actions are done, rather than ending on a promise to do them.
- When the current request is satisfied, generate '###STOP###' to end the interaction.
- If you have actually been transferred to another agent, generate '###TRANSFER###'.
- If the scenario does not provide enough information to continue, generate '###OUT-OF-SCOPE###'.
- These tokens are control signals, not words to speak or include in ordinary replies.
"""

_VOICE_GUIDELINES = """## Voice interaction
- You are communicating through speech. Generate one natural spoken utterance at a time.
- Respond to what you actually hear. If something is unclear, ask for repetition or clarification.
- Use natural spoken language, including occasional pauses or restarts where appropriate.
- During extended silence, you may check whether the agent is still there or ask about progress on the current request, without adding new requirements.
- If the agent continues not to respond after two check-ins, end the interaction.
"""

_TOOL_GUIDELINES = """## Your tools
- At each turn, either speak/write to the agent or use one of your provided tools; do not do both at once.
- Use tools only when requested by the agent or necessary to answer its question. Ground reports in actual tool results; do not invent them.
- Messages accompanying your tool calls are not displayed to the agent. Report relevant results in a subsequent ordinary reply.
"""


def get_gaia2_user_guidelines(
    persona: PersonaConfig | None, *, mode: CallMode, use_tools: bool
) -> str:
    """Select language and voice instructions without the service-role persona.

    The shared prompt builder only injects a full persona when its placeholder
    is present. This override instead includes the needed fields explicitly,
    leaving the original persona available for acoustic and timing settings.
    """
    resolved = persona if isinstance(persona, RuntimePersona) else default_persona(mode)
    sections = [
        f"# Gaia2 User Guidelines ({GAIA2_USER_GUIDELINES_VERSION})",
        language_directive(resolved),
        _TASK_GUIDELINES,
        PersonaConfig.to_guidelines_text(persona or PersonaConfig(), mode) or "",
    ]
    if isinstance(persona, RuntimePersona):
        sections.append(f"You are a {persona.gender} user.")
        sections.extend([persona.user_guidelines, persona.mode_guidelines])
    if mode == "voice":
        sections.extend([_VOICE_GUIDELINES, resolved.spellout_section])
    else:
        sections.append("Generate one natural text message at a time.")
    if use_tools:
        sections.append(_TOOL_GUIDELINES)
    return "\n\n".join(section.strip() for section in sections if section.strip())
