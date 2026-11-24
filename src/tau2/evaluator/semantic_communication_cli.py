"""CLI parameters for Gaia2 language and semantic communication scoring."""

import argparse
import json


def add_communication_run_args(parser: argparse.ArgumentParser) -> None:
    """Register language selection on the ordinary run command."""
    from tau2.data_model.semantic_communication import CommunicateScorer

    parser.add_argument(
        "--communicate-scorer",
        choices=[scorer.value for scorer in CommunicateScorer],
        default=None,
        help="Communicate-info scorer: the pinned semantic LLM judge (default) or "
        "the original substring matching (custom runs only).",
    )
    parser.add_argument(
        "--communicate-judge",
        type=json.loads,
        default=None,
        metavar="JSON",
        help='Semantic judge overrides, e.g. {"model": "...", "model_args": {...}}.',
    )
    parser.add_argument(
        "--language",
        choices=("en",),
        default=None,
        action="append",
        help="Gaia2 call language (English only).",
    )
    parser.set_defaults(localize_db=None)


def communicate_scoring_from_args(args: argparse.Namespace):
    """The explicit communicate-info scorer a run command requests, if any."""
    from tau2.data_model.semantic_communication import (
        CommunicateScorer,
        CommunicateScoring,
        SemanticCommunicateSettings,
    )

    if args.communicate_scorer is None and args.communicate_judge is None:
        return None
    scorer = CommunicateScorer(args.communicate_scorer or CommunicateScorer.SEMANTIC)
    if scorer == CommunicateScorer.SUBSTRING:
        if args.communicate_judge is not None:
            raise ValueError("--communicate-judge applies to the semantic scorer")
        return CommunicateScoring(scorer=scorer)
    return CommunicateScoring(
        judge=SemanticCommunicateSettings.model_validate(args.communicate_judge or {})
    )
