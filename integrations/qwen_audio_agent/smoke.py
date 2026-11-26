"""Run English retail or Gaia2 calls with MiMo audio and the configured judge."""

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

# Qwen Gaia2 smoke calls use a 200 ms simulation tick.  The 6000-tick
# default leaves enough simulated time for backend retries and stage delivery;
# pass --max-seconds 0 explicitly when an unlimited run is wanted.
DEFAULT_SMOKE_MAX_SECONDS = 1200


def main():
    """Load evaluation credentials privately; keep model/key roles separate."""
    from tau2.adapters.qwen_audio_agent.config import QwenAgentConfig
    from tau2.config import DEFAULT_SEED
    from tau2.utils.env_config import read_config_environment, resolve_env_reference

    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=here / "config.gaia2.unified.json"
    )
    parser.add_argument("--api-config-dir", type=Path)
    parser.add_argument("--num-tasks", type=int, default=3)
    parser.add_argument("--domain", choices=["retail", "gaia2"], default="gaia2")
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--task-split-name", default="base")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument(
        "--realtime-generation",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Run SAASR caller generation without blocking simulation ticks. "
            "With --no-realtime-generation, the world waits only for first audio."
        ),
    )
    parser.add_argument(
        "--user-audio-renderer",
        choices=["saasr"],
        default="saasr",
        help="Qwen Audio Agent uses the SAASR caller renderer.",
    )
    parser.add_argument("--saasr-config", type=Path)
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=DEFAULT_SMOKE_MAX_SECONDS,
        help=(
            "Maximum conversation seconds; default is 1200 seconds "
            "(6000 ticks at 0.2s/tick), and 0 disables time limits."
        ),
    )
    parser.add_argument(
        "--save-to",
        default="qwen-mimo-smoke-" + datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--metrics-config", type=Path)
    parser.add_argument(
        "--judge-from-user-llm",
        action="store_true",
        help="Use caller LLM credentials and model for runtime judges.",
    )
    parser.add_argument("--active-metrics-config", type=Path)
    parser.add_argument("--user-simulation-config", type=Path)
    parser.add_argument("--gaia2-allow-stale-runtime", action="store_true")
    parser.add_argument(
        "--judge-api-key-env",
        default="GAIA2_JUDGE_API_KEY",
        help="Environment variable containing the communication/NL judge key.",
    )
    parser.add_argument(
        "--judge-endpoint-env",
        default="GAIA2_JUDGE_ENDPOINT",
        help="Environment variable containing the communication/NL judge endpoint.",
    )
    parser.add_argument(
        "--judge-model-env",
        default="GAIA2_JUDGE_MODEL",
        help="Environment variable containing the communication/NL judge model.",
    )
    parser.add_argument(
        "--judge-api",
        choices=["chat", "responses"],
        default=None,
        help="Judge API protocol; defaults to GAIA2_JUDGE_API for the legacy profile.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume saved results and run only unfinished task/trial pairs.",
    )
    args = parser.parse_args()
    if args.max_seconds < 0:
        parser.error("--max-seconds must be nonnegative; use 0 for no time limit")
    if args.max_concurrency < 1:
        parser.error("--max-concurrency must be positive")
    config_value = json.loads(args.config.read_text())
    if args.api_config_dir is not None:
        config_value["api_config_dir"] = str(args.api_config_dir)
    if config_value.get("api_config_dir"):
        config_value["api_configuration_resolved"] = False
    config = QwenAgentConfig.model_validate(config_value)
    if args.user_simulation_config:
        from tau2.data_model.user_simulation import (
            UserSimulationConfig,
            merge_user_settings,
        )

        caller = UserSimulationConfig.model_validate_json(
            args.user_simulation_config.read_text()
        )
        config = config.model_copy(
            update={"evaluation": merge_user_settings(caller, config.evaluation)}
        )
    if config.api_config_dir:
        return run_shared(args, config)
    values = read_config_environment(config.config_env_file)
    env = dict(os.environ)
    caller = config.user_llm_settings()
    if args.judge_from_user_llm:
        if caller is None:
            raise ValueError(
                "--judge-from-user-llm requires a complete user LLM configuration"
            )
        judge_model, user_llm_args = caller
        judge_api_key = user_llm_args["api_key"]
        judge_api_base = user_llm_args["api_base"]
        env["OPENAI_API_KEY"] = judge_api_key
        env["OPENAI_API_BASE"] = judge_api_base
        env["OPENAI_BASE_URL"] = judge_api_base
        judge_model_args = {
            key: value
            for key, value in user_llm_args.items()
            if key not in {"api_key", "api_base", "base_url"}
        }
    else:
        env["OPENAI_API_KEY"] = resolve_env_reference(values, args.judge_api_key_env)
        env["OPENAI_API_BASE"] = resolve_env_reference(values, args.judge_endpoint_env)
        env["OPENAI_BASE_URL"] = env["OPENAI_API_BASE"]
        judge_model = resolve_env_reference(values, args.judge_model_env)
        judge_model_args = {
            "reasoning_effort": values.get("GAIA2_JUDGE_REASONING_EFFORT", "low"),
            "num_retries": 0,
        }
    metrics_source = args.metrics_config
    if args.metrics_config:
        from tau2.diagnostics.config import MetricsConfig

        metrics = MetricsConfig.load(args.metrics_config)
        # Resolve only explicitly named diagnostic credentials from the same
        # private source; each role retains its own declared model and endpoint.
        if not args.judge_from_user_llm:
            for profile in [metrics.model, *metrics.models.values()]:
                if profile.api_key_env:
                    env[profile.api_key_env] = resolve_env_reference(
                        values, profile.api_key_env
                    )
    # Explicit backend references prevent evaluator credentials overriding Agent.
    # The names are configurable so a run can select a separate provider profile
    # (for example DEEPSEEK_*); only the complete three-variable contract matters.
    backend_refs = {
        target: config.environment.get(target)
        for target in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
    }
    if any(not source for source in backend_refs.values()):
        raise ValueError(
            "Smoke config must explicitly reference backend key, base URL and model"
        )
    if not args.judge_from_user_llm:
        prefix = (
            "openai/responses/"
            if (args.judge_api or values.get("GAIA2_JUDGE_API")) == "responses"
            else "openai/"
        )
        judge_model = prefix + judge_model
    user_model, user_args = caller if caller else (judge_model, judge_model_args)
    env["TAU2_NL_ASSERTIONS_MODEL"] = judge_model
    env["TAU2_NL_ASSERTIONS_REASONING_EFFORT"] = judge_model_args.get(
        "reasoning_effort", "low"
    )
    from tau2.utils.utils import RESULTS_DIR

    output = RESULTS_DIR / args.save_to
    if (output / "results.json").exists() and not args.dry_run and not args.resume:
        raise ValueError(
            "Choose a new save-to name to preserve previous smoke attempts"
        )
    output.mkdir(parents=True, exist_ok=True)
    if caller is None:
        from tau2.data_model.user_simulation import UserSimulationConfig

        caller_settings = config.evaluation or UserSimulationConfig()
        config = config.model_copy(
            update={
                "evaluation": caller_settings.model_copy(
                    update={
                        "user_decision_llm": user_model,
                        "user_decision_llm_args": user_args,
                    }
                )
            }
        )
    derived_config = output / "qwen_agent_config.json"
    derived_config.write_text(config.model_dump_json(indent=2))
    derived_config.chmod(0o600)
    if args.judge_from_user_llm and metrics_source is not None:
        import yaml

        judge_profile = {
            "model": judge_model,
            "api_base": judge_api_base,
            "api_key_env": "TAU2_RUNTIME_JUDGE_API_KEY",
            "args": judge_model_args,
        }
        # Keep file-backed calibration resolved when relocating this profile.
        metrics_value = metrics.model_dump(mode="json")
        metrics_value["model"] = judge_profile
        for role in metrics_value.get("models") or {}:
            metrics_value["models"][role] = dict(judge_profile)
        metrics_source = output / "metrics_config.runtime.yaml"
        metrics_source.write_text(
            yaml.safe_dump(metrics_value, sort_keys=False, allow_unicode=True)
        )
        env["TAU2_RUNTIME_JUDGE_API_KEY"] = judge_api_key
    command = [
        str(here / "run_hpc.sh"),
        "run",
        "--audio-native",
        "--audio-native-provider",
        "qwen_agent",
        "--qwen-agent-config",
        str(derived_config),
        "--benchmark",
        "none",
        "--domain",
        args.domain,
        "--task-split-name",
        args.task_split_name,
        "--language",
        "en",
        "--num-trials",
        "1",
        "--seed",
        str(args.seed),
        "--max-concurrency",
        str(args.max_concurrency),
        "--speech-complexity",
        "control",
        "--max-steps-seconds",
        str(args.max_seconds),
        "--realtime-generation"
        if args.realtime_generation
        else "--no-realtime-generation",
        "--user-llm",
        user_model,
        "--user-llm-args",
        json.dumps(user_args),
        "--communicate-judge",
        json.dumps({"model": judge_model, "model_args": judge_model_args}),
        "--save-to",
        args.save_to,
        "--verbose-logs",
        "--audio-taps",
    ]
    if metrics_source:
        command += ["--metrics-config", str(metrics_source.resolve())]
    if args.active_metrics_config:
        from tau2.metrics.active.config import ActiveMetricsConfig

        active_metrics = ActiveMetricsConfig.load(args.active_metrics_config)
        if active_metrics.persistent_task is not None:
            profile = active_metrics.persistent_task.generator.model
            generator = active_metrics.persistent_task.generator
            for name in (
                generator.model_env,
                generator.api_base_env,
                generator.api_key_env,
            ):
                if name:
                    env[name] = resolve_env_reference(values, name)
            if profile.api_key_env:
                env[profile.api_key_env] = resolve_env_reference(
                    values, profile.api_key_env
                )
        command += [
            "--active-metrics-config",
            str(args.active_metrics_config.resolve()),
        ]
    if getattr(args, "user_simulation_config", None):
        command += [
            "--user-simulation-config",
            str(args.user_simulation_config.resolve()),
        ]
    if args.gaia2_allow_stale_runtime:
        command.append("--gaia2-allow-stale-runtime")

    if args.max_seconds:
        command.extend(["--timeout", str(args.max_seconds + 150)])
    if args.saasr_config is None:
        parser.error("Qwen Audio Agent requires --saasr-config")
    command.extend(
        [
            "--user-audio-renderer",
            "saasr",
            "--saasr-config",
            str(args.saasr_config.resolve()),
        ]
    )
    if args.resume:
        command.append("--auto-resume")
    if args.task_ids:
        command.extend(["--task-ids", *args.task_ids])
    else:
        command.extend(["--num-tasks", str(args.num_tasks)])
    if args.dry_run:
        command.append("--dry-run")
    secrets = [
        value
        for key, value in values.items()
        if value and any(word in key for word in ("API_KEY", "TOKEN", "SECRET"))
    ]
    with subprocess.Popen(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ) as process:
        for line in process.stdout:
            for secret in secrets:
                line = line.replace(secret, "[REDACTED]")
            print(line, end="", flush=True)
        code = process.wait()
    if code == 0 and not args.dry_run:
        from tau2.utils.utils import RESULTS_DIR

        result_file = RESULTS_DIR / args.save_to / "results.json"
        if not result_file.exists():
            raise RuntimeError("Smoke run did not produce results")
        index = json.loads(result_file.read_text()).get("simulation_index", [])
        if not index or any(
            item.get("termination_reason") in {"infrastructure_error", "user_error"}
            or item.get("evaluation_status") == "failed"
            for item in index
        ):
            print(
                "Smoke run contains infrastructure or user-simulator failures; see the saved results."
            )
            code = 1
    raise SystemExit(code)


def run_shared(args, config):
    """Launch shared API roles without putting any credentials on argv."""
    from tau2.utils.api_config import get_api_configuration
    from tau2.utils.utils import RESULTS_DIR

    here = Path(__file__).resolve().parent
    directory = config.api_config_dir
    api = get_api_configuration(directory)
    agent_api = get_api_configuration(directory, "qw")
    # Resolve required launch connections once, without model/network probes.
    config.user_llm_settings()
    agent_api.agent_environment()
    api.text("USER" if args.judge_from_user_llm else "REPLY_JUDGE")
    output = RESULTS_DIR / args.save_to
    if (output / "results.json").exists() and not args.dry_run and not args.resume:
        raise ValueError("Choose a new save-to name to preserve previous attempts")
    output.mkdir(parents=True, exist_ok=True)
    derived = output / "qwen_agent_config.json"
    with derived.open("w") as stream:
        os.chmod(derived, 0o600)
        stream.write(config.model_dump_json(indent=2))
    command = [
        str(here / "run_hpc.sh"),
        "run",
        "--audio-native",
        "--audio-native-provider",
        "qwen_agent",
        "--qwen-agent-config",
        str(derived),
        "--api-config-dir",
        directory,
        "--benchmark",
        "none",
        "--domain",
        args.domain,
        "--task-split-name",
        args.task_split_name,
        "--language",
        "en",
        "--num-trials",
        "1",
        "--seed",
        str(args.seed),
        "--max-concurrency",
        str(args.max_concurrency),
        "--speech-complexity",
        "control",
        "--max-steps-seconds",
        str(args.max_seconds),
        "--realtime-generation"
        if args.realtime_generation
        else "--no-realtime-generation",
        "--save-to",
        args.save_to,
        "--verbose-logs",
        "--audio-taps",
    ]
    if args.judge_from_user_llm:
        command += ["--judge-from-user-llm"]
    elif (
        args.judge_api is not None
        or args.judge_api_key_env != "GAIA2_JUDGE_API_KEY"
        or args.judge_endpoint_env != "GAIA2_JUDGE_ENDPOINT"
        or args.judge_model_env != "GAIA2_JUDGE_MODEL"
    ):
        raise ValueError(
            "Shared API mode uses realbench.env judge roles, not legacy judge flags"
        )
    for name, flag in (
        ("metrics_config", "--metrics-config"),
        ("active_metrics_config", "--active-metrics-config"),
    ):
        path = getattr(args, name)
        if path:
            command += [flag, str(path.resolve())]
    if args.max_seconds:
        command += ["--timeout", str(args.max_seconds + 150)]
    if args.saasr_config is None:
        raise ValueError("Qwen Audio Agent requires --saasr-config")
    command += [
        "--user-audio-renderer",
        "saasr",
        "--saasr-config",
        str(args.saasr_config.resolve()),
    ]
    if getattr(args, "user_simulation_config", None):
        command += [
            "--user-simulation-config",
            str(args.user_simulation_config.resolve()),
        ]
    if args.gaia2_allow_stale_runtime:
        command.append("--gaia2-allow-stale-runtime")
    if args.resume:
        command.append("--auto-resume")
    command += (
        ["--task-ids", *args.task_ids]
        if args.task_ids
        else ["--num-tasks", str(args.num_tasks)]
    )
    if args.dry_run:
        command.append("--dry-run")
    # Filter child diagnostics as well as keeping secret values off argv.
    secrets = [
        resolve
        for snapshot in (api, agent_api)
        for key in snapshot.values
        if any(x in key for x in ("KEY", "TOKEN", "SECRET"))
        if (resolve := snapshot.value(key))
    ]
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ) as process:
        for line in process.stdout:
            for secret in secrets:
                line = line.replace(secret, "[REDACTED]")
            print(line, end="", flush=True)
        code = process.wait()
    if code == 0 and not args.dry_run:
        result_file = output / "results.json"
        if not result_file.exists():
            raise RuntimeError("Smoke run did not produce results")
        index = json.loads(result_file.read_text()).get("simulation_index", [])
        if not index or any(
            item.get("termination_reason") in {"infrastructure_error", "user_error"}
            or item.get("evaluation_status") == "failed"
            for item in index
        ):
            code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
