"""Compatibility exports for the evaluator-owned persistent grader."""

from tau2.evaluator.evaluator_communicate import CommunicateEvaluator
from tau2.evaluator.persistent_tasks.grader import GRADING_VERSION, grade

__all__ = ["GRADING_VERSION", "CommunicateEvaluator", "grade"]
