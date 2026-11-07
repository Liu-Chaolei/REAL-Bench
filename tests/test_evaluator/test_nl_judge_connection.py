"""Explicit NL-judge routing keeps the original assertion and reward logic."""

import json
import os
import subprocess
import sys


def test_nl_judge_connection_is_resolved_before_both_evaluators_import():
    script = """
import json
from types import SimpleNamespace
from tau2.evaluator import evaluator_nl_assertions as n
from tau2.data_model.tasks import Task, EvaluationCriteria, UserScenario
calls = []
def generate(**kwargs):
    calls.append(kwargs)
    return SimpleNamespace(content=json.dumps({'results': [
        {'expectedOutcome': 'fixture', 'reasoning': 'fixture unmet', 'metExpectation': False}
    ]}))
n.generate = generate
t = Task(id='fixture', user_scenario=UserScenario(instructions='fixture'),
         evaluation_criteria=EvaluationCriteria(nl_assertions=['fixture']))
for evaluator in [n.NLAssertionsEvaluator, n.FullDuplexNLAssertionsEvaluator]:
    result = evaluator.calculate_reward(t, [])
    assert result.reward == 0.0
    assert result.nl_assertions[0].met is False
    assert result.info['judge']['model'] == 'openai/responses/configured-judge'
assert len(calls) == 2
for call in calls:
    assert call['model'] == 'openai/responses/configured-judge'
    assert call['reasoning_effort'] == 'low'
    assert call['call_name'] == 'nl_assertions_eval'
print(json.dumps({'model': n.DEFAULT_LLM_NL_ASSERTIONS, 'calls': len(calls)}))
"""
    env = {
        **os.environ,
        "TAU2_NL_ASSERTIONS_MODEL": "openai/responses/configured-judge",
        "TAU2_NL_ASSERTIONS_REASONING_EFFORT": "low",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    output = json.loads(completed.stdout.strip().splitlines()[-1])
    assert output == {"model": "openai/responses/configured-judge", "calls": 2}
