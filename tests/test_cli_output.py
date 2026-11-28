"""Compact CLI output: tables by default, full JSON on request, clean --help."""

import subprocess
import sys

import pytest

from tau2.data_model.benchmark import BenchmarkSummary, CellSummary
from tau2.utils.table import format_fraction, format_table


def test_format_table_aligns_text_left_and_numbers_right():
    table = format_table(
        ["measure", "calls", "pass"],
        [["naturalness", 200, "91.0%"], ["tool_use", 3, "-"]],
    )
    assert table.splitlines() == [
        "measure      calls  pass",
        "-----------  -----  -----",
        "naturalness    200  91.0%",
        "tool_use         3  -",
    ]
    assert format_fraction(None) == "-"
    assert format_fraction(0.5) == "50.0%"


def _cell(language, domain, expected, scored, score):
    return CellSummary(
        language=language,
        domain=domain,
        expected_trials=expected,
        scored_trials=scored,
        error_trials=0,
        unscored_trials=0,
        missing_trials=expected - scored,
        complete=expected == scored,
        score=score if expected == scored else None,
        observed_score=score,
    )


def test_benchmark_summary_table_skips_cells_outside_the_requested_scope():
    from tau2.runner.report import render_summary

    summary = BenchmarkSummary(
        benchmark_version="voice-v2",
        frame_revision="fixture",
        condition_fingerprint=None,
        expected_trials=7,
        scored_trials=5,
        error_trials=0,
        unscored_trials=0,
        missing_trials=2,
        complete=False,
        score=None,
        observed_score=0.6,
        by_language={},
        by_domain={},
        cells=[
            _cell("en", "gaia2", 5, 5, 0.6),
            _cell("pt", "gaia2", 2, 0, None),
            _cell("fr", "gaia2", 0, 0, None),
        ],
    )
    assert render_summary(summary).splitlines() == [
        "language  domain  scored  errors  unscored  missing  score  observed  complete",
        "--------  ------  ------  ------  --------  -------  -----  --------  --------",
        "en        gaia2   5/5          0         0        0  60.0%  60.0%     yes",
        "pt        gaia2   0/2          0         0        2  -      -         no",
    ]


def test_benchmark_plan_table_lists_selected_cells(tmp_path):
    from tau2.data_model.run_config import AudioNativeConfig
    from tau2.data_model.simulation import VoiceRunConfig
    from tau2.runner.presets import BenchmarkRequest, resolve_benchmark
    from tau2.runner.report import render_plan

    plan = resolve_benchmark(
        BenchmarkRequest(
            output=tmp_path,
            template=VoiceRunConfig(audio_native_config=AudioNativeConfig()),
            languages=["en"],
            domains=["gaia2"],
        )
    )
    assert render_plan(plan).splitlines() == [
        "language  domain  tasks  trials  calls",
        "--------  ------  -----  ------  -----",
        "en        gaia2     800       1    800",
    ]


@pytest.mark.parametrize(
    "command",
    [["--help"], ["run", "--help"]],
    ids=" ".join,
)
def test_help_is_only_help(command):
    """Importing the CLI must not log the registry, data directory or noise banks."""
    completed = subprocess.run(
        [sys.executable, "-c", "from tau2.cli import main; main()", *command],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    assert completed.stdout.startswith("usage: ")
    assert completed.stderr == ""
