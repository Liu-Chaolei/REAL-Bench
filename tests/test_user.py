import pytest

from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.user.user_simulator import DummyUser, UserSimulator
from tau2.user.user_simulator_base import UserError


@pytest.fixture
def user_instructions() -> str:
    return (
        "You are Mia Li. You want to fly from New York to Seattle on May 20 (one way)."
    )


@pytest.fixture
def bad_user_instructions() -> str:
    return "You are Mia Li. You want to fly from Chicago to San Francisco on May 19 (round trip)."


@pytest.fixture
def first_agent_message() -> AssistantMessage:
    return AssistantMessage(
        content="Hello, how can I help you today?", role="assistant"
    )


@pytest.fixture
def user_simulator(user_instructions: str) -> UserSimulator:
    return UserSimulator(llm="gpt-4o-mini", instructions=user_instructions)


@pytest.mark.live_api
def test_user_simulator(
    user_simulator: UserSimulator, first_agent_message: AssistantMessage
):
    user_state = user_simulator.get_init_state()
    assert user_state is not None
    user_msg, user_state = user_simulator.generate_next_message(
        first_agent_message, user_state
    )
    # Check the response is a user message
    assert isinstance(user_msg, UserMessage)
    # Check the state is updated
    assert user_state is not None
    # Check the messages are of the correct type
    assert isinstance(user_state.messages[0], AssistantMessage)
    assert user_state.messages[0].content == first_agent_message.content
    assert isinstance(user_state.messages[1], UserMessage)


def test_user_simulator_set_state(
    user_simulator: UserSimulator,
):
    user_simulator.get_init_state(
        message_history=[
            UserMessage(content="Hello, can you help me find a flight?", role="user"),
            AssistantMessage(
                content="Hello, I can help you find a flight.", role="assistant"
            ),
        ]
    )


def test_dummy_user_no_args():
    """DummyUser must be instantiable without arguments (solo-mode gym path)."""
    dummy = DummyUser()
    state = dummy.get_init_state()
    assert state.messages == []


@pytest.mark.parametrize(
    "content",
    ["###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###", "Thanks. ###STOP###"],
)
def test_control_signals_bypass_grounding(monkeypatch, first_agent_message, content):
    """A caller's existing termination signals must reach the normal stop path."""
    user = UserSimulator(llm="unused", instructions="Ask for help.")
    user.scenario_constraints = "Preserve the original request."
    monkeypatch.setattr(
        user,
        "_generate_next_message",
        lambda *args: UserMessage(role="user", content=content, cost=0.25),
    )
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: pytest.fail("Control signals must not be sent to grounding"),
    )

    message, state = user.generate_next_message(
        first_agent_message, user.get_init_state()
    )

    assert message.content == content
    assert message.cost == 0.25
    assert user.is_stop(message)
    assert state.messages[-1] == message


def test_ordinary_user_reply_still_requires_grounding(monkeypatch, first_agent_message):
    """Control handling must not let an ordinary change to the goal bypass checks."""
    user = UserSimulator(llm="unused", instructions="Book October 21.")
    user.scenario_constraints = "Book October 21."
    monkeypatch.setattr(
        user,
        "_generate_next_message",
        lambda *args: UserMessage(role="user", content="Book October 22 instead."),
    )
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: AssistantMessage(
            role="assistant", content='{"valid":false,"reason":"Changed the date"}'
        ),
    )

    with pytest.raises(UserError, match="Changed the date"):
        user.generate_next_message(first_agent_message, user.get_init_state())
