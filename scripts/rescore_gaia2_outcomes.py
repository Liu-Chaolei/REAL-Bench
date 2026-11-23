#!/usr/bin/env python3
"""Rescore selected historical trajectories with the existing private judge profile."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from tau2.utils.env_config import read_config_environment, resolve_env_reference


def main():
    """Configure only judge credentials; never call agent or audio services."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--recorded-data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--config-env",
        default=None,
    )
    parser.add_argument("--api-config-dir", type=Path)
    args = parser.parse_args()
    if args.config_env is None:
        from tau2.config import DEFAULT_API_CONFIG_DIR
        from tau2.utils.api_config import get_api_configuration

        get_api_configuration(str(args.api_config_dir or DEFAULT_API_CONFIG_DIR)).text(
            "REPLY_JUDGE"
        )
        env = dict(os.environ)
        env["TAU2_SHARED_API_CONFIG_DIR"] = str(
            args.api_config_dir or DEFAULT_API_CONFIG_DIR
        )
    else:
        values = read_config_environment(args.config_env)
        env = dict(os.environ)
        env["OPENAI_API_KEY"] = resolve_env_reference(values, "GAIA2_JUDGE_API_KEY")
        env["OPENAI_API_BASE"] = resolve_env_reference(values, "GAIA2_JUDGE_ENDPOINT")
        env["OPENAI_BASE_URL"] = env["OPENAI_API_BASE"]
        prefix = (
            "openai/responses/"
            if values.get("GAIA2_JUDGE_API") == "responses"
            else "openai/"
        )
        env["TAU2_NL_ASSERTIONS_MODEL"] = prefix + resolve_env_reference(
            values, "GAIA2_JUDGE_MODEL"
        )
        env["TAU2_NL_ASSERTIONS_REASONING_EFFORT"] = values.get(
            "GAIA2_JUDGE_REASONING_EFFORT", "low"
        )
    command = [
        sys.executable,
        "-m",
        "tau2.cli",
        "evaluate-trajs",
        *args.paths,
        "--recorded-data-root",
        str(args.recorded_data_root),
        "--output-dir",
        str(args.output_dir),
    ]
    if args.config_env is None:
        command += ["--api-config-dir", env["TAU2_SHARED_API_CONFIG_DIR"]]
    raise SystemExit(subprocess.call(command, env=env))


if __name__ == "__main__":
    main()
