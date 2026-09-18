"""Replay ARE exports that contain generated turn conditions without metadata.

Runs the frozen replay implementation unchanged except for import normalization.
The immutable original scenario proves that removed events were runtime additions.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import override


def normalize_export(data, source):
    normalized = copy.deepcopy(data)
    original_ids = {event["event_id"] for event in source["events"]}
    removed = []
    kept = []
    for event in normalized["events"]:
        if event.get("event_type") != "CONDITION" or event["event_id"] in original_ids:
            kept.append(event)
            continue
        action = event.get("action", {})
        if (
            event.get("class_name") != "ConditionCheckEvent"
            or not event["event_id"].startswith("condition_turn_")
            or action.get("app") is not None
            or not action.get("function", "").startswith("wrapped_condition__turn_idx_")
        ):
            raise ValueError("Unrecognized added condition; cannot normalize replay")
        removed.append(event["event_id"])
    # ARE also rewrites ENV dependencies to generated CHECK_n IDs. Restore the
    # immutable definition rather than guessing how to reverse those rewrites.
    # Completed events, timestamps and recorded judge replies are never changed.
    source_id = source.get("metadata", {}).get("definition", {}).get("scenario_id")
    export_id = data.get("metadata", {}).get("definition", {}).get("scenario_id")
    if source_id != export_id:
        raise ValueError("Original scenario identity differs from the export")
    if {event["event_id"] for event in kept} != original_ids:
        raise ValueError("Original scenario event identities differ from the export")
    normalized["events"] = copy.deepcopy(source["events"])
    if "apps" in source:
        normalized["apps"] = copy.deepcopy(source["apps"])
    return normalized, removed


def parse_replay_parameter(original, value, type_str):
    """Preserve only the exact empty-str case; keep all other ARE validation."""
    if value == "" and type_str == "str":
        return ""
    return original(value, type_str)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-scenario", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, os.environ.get("GAIA2_RUNNER_PATH", "/opt/runner"))
    import replay

    os.environ["GAIA2_REPLAY_SOURCE"] = str(args.source_scenario.resolve())

    # ARE accepts empty string tool arguments at runtime, but its importer
    # conflates them with None. Preserve the original str value during replay.
    from are.simulation.scenarios.scenario import Scenario

    original_parser = Scenario._parse_parameter_value
    empty_strings = []

    def parse_parameter(value, type_str):
        if value == "" and type_str == "str":
            empty_strings.append({"value": value, "value_type": type_str})
        return parse_replay_parameter(original_parser, value, type_str)

    Scenario._parse_parameter_value = staticmethod(parse_parameter)
    original_importer = replay.JsonScenarioImporter
    source = json.loads(args.source_scenario.read_text())
    removed = []

    class ExportImporter(original_importer):
        @override
        def import_from_json_to_benchmark(
            self,
            json_str,
            apps_to_skip=None,
            apps_to_keep=None,
            load_completed_events=True,
        ):
            normalized, identities = normalize_export(json.loads(json_str), source)
            removed.extend(identities)
            return super().import_from_json_to_benchmark(
                json.dumps(normalized),
                apps_to_skip,
                apps_to_keep,
                load_completed_events,
            )

    replay.__dict__["JsonScenarioImporter"] = ExportImporter
    sys.argv = [sys.argv[0], str(args.results), "--output", str(args.output)]
    try:
        replay.main()
    finally:
        Scenario._parse_parameter_value = staticmethod(original_parser)
        replay.__dict__["JsonScenarioImporter"] = original_importer
    report = json.loads(args.output.read_text())
    report.update(
        removed_runtime_conditions=removed,
        preserved_empty_string_parameters=empty_strings,
        source_scenario_sha256=hashlib.sha256(
            args.source_scenario.read_bytes()
        ).hexdigest(),
        normalization="restore immutable scenario definition and regenerate runtime turn conditions; preserve all completed events",
    )
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
