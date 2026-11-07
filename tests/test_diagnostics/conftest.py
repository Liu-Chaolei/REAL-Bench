from copy import deepcopy

import pytest

pytest.importorskip(
    "jsonschema", reason="Install the diagnostics extra for schema tests"
)

from tau2.diagnostics.intent.config import MODEL_ENV_KEYS


@pytest.fixture
def duplicate_target_intent():
    selection = {
        "entity_type": "contact",
        "condition": {
            "kind": "comparison",
            "field": "age",
            "operator": "<=",
            "value": {"kind": "literal", "value": 24},
        },
        "quantifier": {"mode": "all"},
    }
    return {
        "entities": [
            {"id": "contacts", "entity_type": "contact", "selection": selection}
        ],
        "goals": [
            {
                "id": "update",
                "action": "update_contacts",
                "target": {"kind": "selection", "selection": deepcopy(selection)},
                "parameters": [
                    {"name": "age_increment", "value": {"kind": "literal", "value": 1}}
                ],
            }
        ],
    }


@pytest.fixture(autouse=True)
def isolate_model_environment(monkeypatch):
    """Keep local credentials and LiteLLM's implicit dotenv loader out of mocks."""
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    for key in MODEL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
