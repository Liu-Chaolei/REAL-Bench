from typing import Generic, Literal, Optional, Tuple, TypeVar

from loguru import logger

from tau2.agent.base.llm_config import LLMConfigMixin
from tau2.data_model.hidden_intent import HiddenGoalGraph
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    MultiToolMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from tau2.data_model.persona import PersonaConfig
from tau2.environment.tool import Tool
from tau2.user.guidelines import finalize_system_prompt, simulation_guidelines
from tau2.user.hidden_intent import HIDDEN_CONTEXT_PREFIX, HiddenIntentRuntime
from tau2.user.user_simulator_base import (
    OUT_OF_SCOPE,
    STOP,
    TRANSFER,
    HalfDuplexUser,
    UserState,
    ValidUserInputMessage,
    is_valid_user_history_message,
)
from tau2.utils.llm_utils import generate

SYSTEM_PROMPT = """
{global_user_sim_guidelines_with_persona}

<scenario>
{instructions}
</scenario>
""".strip()


def build_user_system_prompt(
    instructions: Optional[str],
    *,
    persona_config: PersonaConfig,
    mode: Literal["text", "voice"],
    guidelines: str,
) -> str:
    """Assemble caller instructions identically for runtime and offline review."""
    persona_guidelines = persona_config.to_guidelines_text(mode=mode) or ""
    if persona_guidelines:
        persona_guidelines = f"\n\n{persona_guidelines}\n"
    prompt = SYSTEM_PROMPT.format(
        global_user_sim_guidelines_with_persona=guidelines.replace(
            "<PERSONA_GUIDELINES>", persona_guidelines
        ),
        instructions=instructions,
    )
    return finalize_system_prompt(persona_config, prompt)


UserStateType = TypeVar("UserStateType", bound="UserState")


class UserSimulator(
    LLMConfigMixin, HalfDuplexUser[UserStateType], Generic[UserStateType]
):
    """A half-duplex LLM-based user simulator for turn-based conversations.

    The runtime persona_config adds additional behavioral guidelines on top of the global
    and task-specific settings.
    Note: User behavior/persona is controlled in THREE places, and they need to be consistent / non-overlapping.
    1. Shared simulation scaffolds (tau2.user.guidelines) rendered with the run's language pack persona
    2. Task-specific persona (UserScenario.persona field) - Baked into task JSON at creation time
    3. Runtime persona config (persona_config parameter) - Configurable at simulation time
    """

    def __init__(
        self,
        llm: str,
        instructions: Optional[str] = None,
        tools: Optional[list[Tool]] = None,
        llm_args: Optional[dict] = None,
        persona_config: Optional[
            PersonaConfig
        ] = None,  # TODO: Should this be pushed to the base class?
        global_guidelines_override: Optional[str] = None,
        hidden_intent_graph: HiddenGoalGraph | None = None,
    ):
        super().__init__(
            instructions=instructions,
            tools=tools,
            llm=llm,
            llm_args=llm_args,
        )
        self.persona_config = persona_config or PersonaConfig()
        self.global_guidelines_override = global_guidelines_override
        self.hidden_intent_runtime = (
            HiddenIntentRuntime(hidden_intent_graph)
            if hidden_intent_graph is not None
            else None
        )
        self._hidden_intent_events: list[dict] = []
        self._hidden_intent_seen_questions: set[str] = set()
        self._scenario_inputs = []
        self.scenario_constraints = None
        self._released_scenario_facts = []
        self._candidate_recovery_events = []
        from tau2.config import DEFAULT_USER_CANDIDATE_RETRIES

        self.candidate_max_retries = DEFAULT_USER_CANDIDATE_RETRIES

    def queue_scenario_input(self, event_id: str, text: str) -> None:
        """Use the same task script contract in text and streaming voice runs."""
        self._scenario_inputs.append((event_id, text))

    def observe_agent_tool_calls(self, tool_calls) -> None:
        """Bind Agent tool calls to hidden-intent state for diagnostics."""
        if self.hidden_intent_runtime is None:
            return
        for call in tool_calls:
            self.hidden_intent_runtime.observe_tool_call(call.name, call.arguments)

    def get_hidden_intent_metrics(self) -> dict | None:
        """Return the single public hidden-intent metric for this run."""
        if self.hidden_intent_runtime is None:
            return None
        from tau2.metrics.hidden_intent import score_hidden_intent

        return score_hidden_intent(
            self.hidden_intent_runtime.graph, self.hidden_intent_runtime.state
        )

    def scenario_stage_context(self, event_id, text):
        """Mark the newly released request as the current conversational goal."""
        if event_id in (
            getattr(self, "scenario_stage_ids", set())
            | getattr(self, "scenario_goal_update_ids", set())
        ):
            return SystemMessage(
                role="system",
                content=(
                    "The previous interaction has ended. Your CURRENT request is: "
                    + text
                    + "\nEarlier requests and conversation are context. Apply the normal "
                    "completion/STOP guidelines to this current request."
                ),
            )
        return None

    @property
    def simulation_mode(self) -> Literal["text", "voice"]:
        """Communication mode shared by scaffold and persona rendering."""
        return "text"

    @property
    def global_simulation_guidelines(self) -> str:
        """
        The simulation guidelines for the user simulator.
        """
        return simulation_guidelines(
            self.persona_config,
            mode=self.simulation_mode,
            use_tools=self.tools is not None,
            override=self.global_guidelines_override,
        )

    @property
    def system_prompt(self) -> str:
        """The complete caller prompt, including persona and task instructions."""
        if self.instructions is None:
            logger.warning("No instructions provided for user simulator")
        return build_user_system_prompt(
            self.instructions,
            persona_config=self.persona_config,
            mode=self.simulation_mode,
            guidelines=self.global_simulation_guidelines,
        )

    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> UserStateType:
        """
        Get the initial state of the user simulator.
        """
        if message_history is None:
            message_history = []
        assert all(is_valid_user_history_message(m) for m in message_history), (
            "Invalid user message history. User messages must be of type UserMessage, AssistantMessage, or ToolMessage to User."
        )

        system_messages = [SystemMessage(role="system", content=self.system_prompt)]
        if self.hidden_intent_runtime is not None:
            system_messages.append(
                SystemMessage(
                    role="system", content=self.hidden_intent_runtime.user_context()
                )
            )
        user_state = UserState(
            system_messages=system_messages,
            messages=message_history,
        )
        return user_state

    def _sync_hidden_intent_context(self, state: UserStateType) -> None:
        """Refresh the private user-side state message after a transition."""
        if self.hidden_intent_runtime is None:
            return
        context = SystemMessage(
            role="system", content=self.hidden_intent_runtime.user_context()
        )
        for index, system_message in enumerate(state.system_messages):
            if system_message.content and system_message.content.startswith(
                HIDDEN_CONTEXT_PREFIX
            ):
                state.system_messages[index] = context
                return
        state.system_messages.append(context)

    def _observe_hidden_question(
        self, question: str, state: UserStateType
    ) -> dict | None:
        """Advance hidden state once for a newly observed Agent turn."""
        if self.hidden_intent_runtime is None or not question:
            return None
        key = " ".join(question.split())
        if key in self._hidden_intent_seen_questions:
            return None
        self._hidden_intent_seen_questions.add(key)
        result = self.hidden_intent_runtime.detect_and_apply(
            question, model=self.llm, model_args=self.llm_args
        )
        self._hidden_intent_events.append({"question": question, **result})
        if result.get("released"):
            self._sync_hidden_intent_context(state)
            self._sync_released_hidden_facts()
        return result

    def _sync_released_hidden_facts(self) -> None:
        """Expose released values to the existing candidate grounding context."""
        if self.hidden_intent_runtime is None:
            return
        for fact in self.hidden_intent_runtime.released_facts():
            if fact not in self._released_scenario_facts:
                self._released_scenario_facts.append(fact)

    @classmethod
    def is_stop(cls, message: UserMessage) -> bool:
        """
        Check if the message is a stop message.
        """
        if message.is_tool_call():
            return False
        # Audio-only messages (chunks) don't have text content
        if message.content is None:
            return False
        return (
            STOP in message.content
            or TRANSFER in message.content
            or OUT_OF_SCOPE in message.content
        )

    def generate_next_message(
        self, message: ValidUserInputMessage, state: UserStateType
    ) -> Tuple[UserMessage, UserStateType]:
        if isinstance(message, AssistantMessage) and message.content:
            self._observe_hidden_question(message.content, state)
        self._sync_hidden_intent_context(state)
        if self._scenario_inputs:
            key, text = self._scenario_inputs.pop(0)
            self._released_scenario_facts.append(text)
            reminder = self.scenario_stage_context(key, text)
            if reminder is not None:
                state.system_messages.append(reminder)
            user_message = UserMessage(
                role="user", content=text, cost=0.0, raw_data={"scenario_input_id": key}
            )
            if message is not None:
                state.messages.append(message)
        else:
            if self.scenario_constraints is None:
                user_message = self._generate_next_message(message, state)
            else:
                from tau2.user.grounding import generate_grounded_reply

                generated_state = None

                def generate_reply(feedback):
                    nonlocal generated_state
                    generated_state = state.model_copy(
                        update={
                            "messages": list(state.messages),
                            "system_messages": list(state.system_messages) + feedback,
                        }
                    )
                    return self._generate_next_message(message, generated_state)

                incoming = (
                    message.tool_messages
                    if isinstance(message, MultiToolMessage)
                    else [message]
                )
                conversation = "\n".join(
                    f"{m.role}: {m.content}"
                    for m in [*state.messages, *incoming]
                    if m is not None and m.content
                )
                user_message = generate_grounded_reply(
                    generate_reply=generate_reply,
                    allowed=self.scenario_constraints
                    + "\n"
                    + "\n".join(self._released_scenario_facts),
                    conversation=conversation,
                    model=self.llm,
                    model_args=self.llm_args,
                    is_stop=self.is_stop,
                    events=self._candidate_recovery_events,
                    max_retries=self.candidate_max_retries,
                    context_revision=len(state.messages),
                )
                state.messages = generated_state.messages
        # Updating state with response
        state.messages.append(user_message)
        return user_message, state

    def _generate_next_message(
        self, message: ValidUserInputMessage, state: UserStateType
    ) -> UserMessage:
        """Get the response from the user simulator.

        Args:
            message: The assistant or tool message.
            state: The user simulator's state.

        Returns:
            The user message.
        """
        if isinstance(message, AssistantMessage) and message.is_audio:
            raise ValueError(
                "Assistant message cannot be audio. Use VoiceUserSimulator instead."
            )
        logger.debug(f"User responds to message: {message}")
        # Updating state with new message
        # Skip empty messages (e.g., empty chunks from streaming mode)
        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        elif isinstance(message, ToolMessage):
            # ToolMessage always has content (tool response)
            state.messages.append(message)
        elif message.has_content() or message.is_tool_call():
            state.messages.append(message)
        messages = state.system_messages + state.flip_roles()

        # Generate response
        assistant_message = generate(
            model=self.llm,
            messages=messages,
            tools=self.tools,
            call_name="user_simulator_response",
            **self.llm_args,
        )

        user_response = assistant_message.content
        logger.debug(f"Response: {user_response}")

        user_message = UserMessage(
            role="user",
            content=user_response,
            generation_time_seconds=assistant_message.generation_time_seconds,
            cost=assistant_message.cost,
            usage=assistant_message.usage,
            raw_data=assistant_message.raw_data,
        )

        # flip the requestor of the tool calls
        if assistant_message.tool_calls is not None:
            user_message.tool_calls = []
            for tool_call in assistant_message.tool_calls:
                user_message.tool_calls.append(
                    ToolCall(
                        id=tool_call.id,
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                        requestor="user",
                    )
                )
        return user_message


class DummyUser(UserSimulator):
    """A dummy user to run a agent solo simulation."""

    def __init__(self):
        super().__init__(llm="dummy")

    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> UserState:
        return UserState(messages=[], system_messages=[])

    def is_stop(cls, message: UserMessage) -> bool:
        raise NotImplementedError("DummyUser does not support stop messages")

    def set_seed(self, seed: int):
        pass

    def generate_next_message(
        self, message: ValidUserInputMessage, state: UserState
    ) -> tuple[UserMessage, UserState]:
        raise NotImplementedError("DummyUser does not support generate_next_message")
