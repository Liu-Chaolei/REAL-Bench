# Adapted from Meta ARE 1.2.0; see ../upstream/LICENSE and SOURCE.json.
from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from typing import Any

from .constants import CheckerType, SoftCheckerType
from .llm_utils import build_llm_checkers, build_subtask_extractor
from .misc import extract_text_between_tags, normalize_arg
from .support import ToolJudge, injected_traceable


class SoftToolJudge(ToolJudge):
    """
    A soft judge that compares some action args of an agent and oracle event representing a tool call with an llm.
    """

    def __init__(self, config: Any):
        super().__init__(config, "soft")
        self.config = config
        # Engine
        self.engine = self.config.engine
        # No arg to check
        self.selected_action_args = [
            name
            for name, check_type in self.config.arg_to_checker_type.items()
            if check_type == CheckerType.llm_checker
        ]
        self.no_arg_to_check = len(self.selected_action_args) == 0

        # Subtask extractor
        self.subtask_extractor = build_subtask_extractor(
            engine=self.config.engine, tool_name=self.tool_name
        )
        # LLM checkers
        self.llm_checkers = build_llm_checkers(engine=self.config.engine)
        # Soft checkers
        self.soft_checkers = {
            SoftCheckerType.content_checker.value: self.content_checker,
            SoftCheckerType.sanity_checker.value: self.sanity_checker,
            SoftCheckerType.signature_checker.value: self.signature_checker,
            SoftCheckerType.placeholder_checker.value: self.placeholder_checker,
            SoftCheckerType.cab_checker.value: self.cab_checker,
            SoftCheckerType.email_checker.value: self.email_checker,
            SoftCheckerType.message_checker.value: self.message_checker,
            SoftCheckerType.user_message_checker.value: self.user_message_checker,
            SoftCheckerType.event_checker.value: self.event_checker,
            SoftCheckerType.tone_checker.value: self.tone_checker,
        }
        # Checker to args names
        self.checker_to_args_names = {
            checker_name: list(inspect.signature(checker_fn).parameters.keys())
            for checker_name, checker_fn in self.soft_checkers.items()
        }
        # Flag if we need to extract subtask
        self.need_subtask = any(c.need_subtask for c in self.config.soft_checker_types)

    def describe_action_args(self, args: dict[str, Any]) -> str:
        return_string = ""
        for k, v in args.items():
            return_string += f"{k}: {v} \n"
        return return_string.strip()

    @injected_traceable(trace_type="equality_checker", tags=["judge"])
    def equality_checker(
        self,
        agent_args: dict[str, Any],
        oracle_args: dict[str, Any],
        **kwargs,
    ) -> bool:
        # Check args
        for arg_name in oracle_args.keys():
            if normalize_arg(agent_args[arg_name]) != normalize_arg(
                oracle_args[arg_name]
            ):
                return False
        return True

    @injected_traceable(trace_type="placeholder_checker", tags=["judge"])
    def placeholder_checker(
        self,
        agent_args: dict[str, Any],
        **kwargs,
    ) -> bool:
        agent_args_str = " ".join([v for v in agent_args.values()])
        placeholders = [
            "[User's Name]",
            "[User Name]",
            "[User]",
            "[Your Name]",
            "[My Name]",
            "Best regards,\nYour Name",
            "Best,\nYour Name",
        ]
        if any(p.lower() in agent_args_str.lower() for p in placeholders):
            return False
        return True

    @injected_traceable(trace_type="extract_subtask", tags=["judge"])
    def extract_subtask(self, oracle_action_call: str, task: str) -> str:
        task = task.strip()
        if len(task) == 0:
            return ""
        # Extract subtask
        subtask = self.subtask_extractor(
            user_prompt_args={
                "tool_name": self.tool_name,
                "oracle_action_call": oracle_action_call,
                "task": task,
            }
        )
        # Extract subtask
        if subtask is None:
            return ""
        subtask = extract_text_between_tags(subtask, "subtask")
        return subtask[0].strip() if len(subtask) > 0 else ""

    @injected_traceable(trace_type="content_checker", tags=["judge"])
    def content_checker(
        self,
        agent_args: dict[str, Any],
        oracle_args: dict[str, Any],
        today_date: str,
        user_address: str,
        subtask: str,
        **kwargs,
    ) -> bool | None:
        # Call the soft checker
        return self.llm_checkers[SoftCheckerType.content_checker.value](
            user_prompt_args={
                "agent_action_call": self.describe_action_args(agent_args),
                "oracle_action_call": self.describe_action_args(oracle_args),
                "task": subtask,
                "tool_name": self.tool_name,
                "today_date": today_date,
                "user_address": user_address,
            }
        )

    @injected_traceable(trace_type="signature_checker", tags=["judge"])
    def signature_checker(
        self,
        agent_args: dict[str, Any],
        user_name: str,
        **kwargs,
    ) -> bool | None:
        # Call the soft checker
        return self.llm_checkers[SoftCheckerType.signature_checker.value](
            user_prompt_args={
                "agent_action_call": self.describe_action_args(agent_args),
                "user_name": user_name,
            }
        )

    @injected_traceable(trace_type="sanity_checker", tags=["judge"])
    def sanity_checker(
        self,
        agent_args: dict[str, Any],
        task: str = "",
        previous_task: str = "",
        **kwargs,
    ) -> bool | None:
        # Check for numerical values first
        agent_action_call = self.describe_action_args(agent_args)

        def is_valid_numerical_values_format(s):
            # Define the regular expression pattern
            pattern = r"^content: \d+(\.\d+)?$"

            # Use re.match to check if the string matches the pattern
            return re.match(pattern, s) is not None

        if is_valid_numerical_values_format(agent_action_call):
            return True
        # Call the soft checker
        return self.llm_checkers[SoftCheckerType.sanity_checker.value](
            user_prompt_args={
                "agent_action_call": agent_action_call,
                "task": "\n".join([previous_task, task]),
            }
        )

    @injected_traceable(trace_type="cab_checker", tags=["judge"])
    def cab_checker(
        self,
        agent_args: dict[str, Any],
        oracle_args: dict[str, Any],
        user_address: str,
        **kwargs,
    ) -> bool | None:
        # Call the soft checker
        return self.llm_checkers[SoftCheckerType.cab_checker.value](
            user_prompt_args={
                "agent_action_call": self.describe_action_args(agent_args),
                "oracle_action_call": self.describe_action_args(oracle_args),
                "user_address": user_address,
            }
        )

    @injected_traceable(trace_type="email_checker", tags=["judge"])
    def email_checker(
        self,
        agent_args: dict[str, Any],
        oracle_args: dict[str, Any],
        today_date: str,
        **kwargs,
    ) -> bool | None:
        # Call the soft checker
        return self.llm_checkers[SoftCheckerType.email_checker.value](
            user_prompt_args={
                "agent_action_call": self.describe_action_args(agent_args),
                "oracle_action_call": self.describe_action_args(oracle_args),
                "today_date": today_date,
            }
        )

    @injected_traceable(trace_type="message_checker", tags=["judge"])
    def message_checker(
        self,
        agent_args: dict[str, Any],
        oracle_args: dict[str, Any],
        today_date: str,
        **kwargs,
    ) -> bool | None:
        # Call the soft checker
        return self.llm_checkers[SoftCheckerType.message_checker.value](
            user_prompt_args={
                "agent_action_call": self.describe_action_args(agent_args),
                "oracle_action_call": self.describe_action_args(oracle_args),
                "today_date": today_date,
            }
        )

    @injected_traceable(trace_type="event_checker", tags=["judge"])
    def event_checker(
        self,
        agent_args: dict[str, Any],
        oracle_args: dict[str, Any],
        user_address: str,
        subtask: str,
        **kwargs,
    ) -> bool | None:
        # Call the soft checker
        return self.llm_checkers[SoftCheckerType.event_checker.value](
            user_prompt_args={
                "agent_action_call": self.describe_action_args(agent_args),
                "oracle_action_call": self.describe_action_args(oracle_args),
                "user_address": user_address,
                "task": subtask,
            }
        )

    @injected_traceable(trace_type="user_message_checker", tags=["judge"])
    def user_message_checker(
        self,
        agent_args: dict[str, Any],
        oracle_args: dict[str, Any],
        subtask: str,
        **kwargs,
    ) -> bool | None:
        # Call the soft checker
        return self.llm_checkers[SoftCheckerType.user_message_checker.value](
            user_prompt_args={
                "agent_action_call": self.describe_action_args(agent_args),
                "oracle_action_call": self.describe_action_args(oracle_args),
                "task": subtask,
            }
        )

    @injected_traceable(trace_type="tone_checker", tags=["judge"])
    def tone_checker(
        self,
        agent_args: dict[str, Any],
        **kwargs,
    ) -> bool | None:
        # Call the soft checker
        return self.llm_checkers[SoftCheckerType.tone_checker.value](
            user_prompt_args={
                "agent_action_call": self.describe_action_args(agent_args),
            }
        )

    def get_checker_kwargs(
        self,
        kwargs: dict[str, Any],
        oracle_event: Any,
        oracle_args: dict[str, Any],
    ) -> dict[str, Any]:
        # Get the task
        tasks = kwargs.get("tasks", [""])
        # Soft checker kwargs
        today_date = ""
        if oracle_event.event_time is not None:
            today_date = datetime.fromtimestamp(
                oracle_event.event_time, tz=timezone.utc
            ).strftime("%Y-%m-%d %A")
        # Subtask
        subtask = (
            self.extract_subtask(
                oracle_action_call=self.describe_action_args(oracle_args),
                task="\n".join(tasks),
            )
            if self.need_subtask
            else ""
        )
        # User name
        user_details = kwargs.get("user_details")
        user_name = (
            f"{user_details.first_name} {user_details.last_name}"
            if user_details
            else ""
        )
        # User address
        user_address = f"{user_details.address}" if user_details else ""

        # Tasks
        return {
            "task": tasks[-1],
            "previous_task": "/n".join(tasks[:-1]) if len(tasks) > 1 else "",
            "user_name": user_name,
            "user_address": user_address,
            "today_date": today_date,
            "oracle_args": oracle_args,
            "subtask": subtask,
        }

    def compare(
        self,
        agent_event: Any,
        oracle_event: Any,
        **kwargs,
    ) -> bool | None:
        # If no args to check, then return True
        if self.no_arg_to_check:
            return True
        # Get the args
        oracle_args = oracle_event.get_args()
        agent_args = agent_event.get_args()
        selected_action_args = [
            arg for arg in self.selected_action_args if bool(oracle_args[arg])
        ]
        oracle_args = {
            k: v for k, v in oracle_args.items() if k in selected_action_args
        }
        agent_args = {k: v for k, v in agent_args.items() if k in selected_action_args}
        # Check equality
        if self.equality_checker(
            agent_args=agent_args,
            oracle_args=oracle_args,
        ):
            return True
        # Get checker kwargs
        checker_kwargs = self.get_checker_kwargs(
            kwargs=kwargs,
            oracle_event=oracle_event,
            oracle_args=oracle_args,
        )
        # Apply soft checkers
        for checker in self.config.soft_checker_types:
            checker_fn = self.soft_checkers[checker.value]
            # This is only for logging purposes
            _checker_kwargs = {
                k: v
                for k, v in checker_kwargs.items()
                if k in self.checker_to_args_names[checker.value]
            }
            # Call the checker
            verdict = checker_fn(agent_args=agent_args, **_checker_kwargs)
            if verdict is None:
                return None
            if not verdict:
                return False
        return True
