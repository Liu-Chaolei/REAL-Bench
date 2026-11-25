"""One prefix-only interpretation per speech/action event, with existing cache."""

import json
from pathlib import Path

from tau2.diagnostics.intent.io import canonical
from tau2.diagnostics.state.models import Interpretation


class SemanticInterpreter:
    """Use the configured model; never provide future events or private answers."""

    def __init__(self, model, capabilities):
        self.model, self.capabilities = model, capabilities
        self.rubric = Path(__file__).with_name("semantic_prompt.md").read_text()

    def interpret(self, event, visible):
        payload = dict(
            event=event.model_dump(mode="json"),
            visible_state=visible,
            capabilities=self.capabilities,
        )
        raw = self.model.complete_sync(
            self.rubric + "\nRecorded prefix:\n" + canonical(payload),
            Interpretation.model_json_schema(),
        )
        value = json.loads(raw)
        # Some providers echo the requested JSON response format as a wrapper
        # field. It is transport metadata, not part of Interpretation. Keep the
        # state schema strict for every other unexpected field.
        if isinstance(value, dict) and value.get("type") == "json_object":
            value = {key: item for key, item in value.items() if key != "type"}
        return Interpretation.model_validate(value)
