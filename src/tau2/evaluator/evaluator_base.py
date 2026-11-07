import traceback
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from tau2.data_model.simulation import RewardInfo
from tau2.data_model.tasks import Task

TrajectoryItemType = TypeVar("TrajectoryItemType")


def evaluation_error_info(error: Exception) -> dict:
    """Classify a scoring failure without replacing its completed conversation."""
    components = {
        "evaluator_env.py": "env",
        "evaluator_communicate.py": "communicate",
        "evaluator_nl_assertions.py": "nl_assertion",
        "evaluator_action.py": "action",
    }
    component = next(
        (
            name
            for frame in traceback.extract_tb(error.__traceback__)
            for filename, name in components.items()
            if frame.filename.endswith(filename)
        ),
        "unknown",
    )
    return {
        "error_type": type(error).__name__,
        "message": str(error),
        "component": component,
    }


class EvaluatorBase(ABC, Generic[TrajectoryItemType]):
    """
    Base class for all Evaluators.
    Evaluators are responsible for evaluating a simulation.
    """

    @classmethod
    @abstractmethod
    def calculate_reward(
        cls,
        task: Task,
        full_trajectory: list[TrajectoryItemType],
        **kwargs: Any,
    ) -> RewardInfo:
        """
        Calculate the reward for the simulation.
        """
        pass
