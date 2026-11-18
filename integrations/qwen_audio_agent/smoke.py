"""Run English retail or Gaia2 calls with MiMo audio and the configured judge."""

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path


def main():
    """Load evaluation credentials privately; keep model/key roles separate."""
    from tau2.config import DEFAULT_SEED
    from tau2.utils.env_config import read_config_environment, resolve_env_reference
    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=here / "config.speech-to-speech.example.json"
    )
    parser.add_argument("--num-tasks", type=int, default=3)
    parser.add_argument("--domain", choices=["retail", "gaia2"], default="retail")
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument(
        "--user-audio-renderer", choices=["standard", "saasr"], default="standard"
    )
    parser.add_argument("--saasr-config", type=Path)
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=0,
        help="Maximum conversation seconds; 0 (default) disables time limits.",
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
        help=(
            "Use the configured caller LLM credentials/model for all runtime "
            "judges and diagnostics, without changing the source config files."
        ),
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
    config = QwenAgentConfig.model_validate_json(args.config.read_text())
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
        # Judges use LiteLLM's ambient OpenAI-compatible connection. The source
        # JSON remains unchanged; these values exist only in this subprocess.
        env["OPENAI_API_KEY"] = judge_api_key
        env["OPENAI_API_BASE"] = judge_api_base
        env["OPENAI_BASE_URL"] = judge_api_base
        judge_model_args = {
            key: value
            for key, value in user_llm_args.items()
            if key not in {"api_key", "api_base", "base_url"}
        }
    else:
        env["OPENAI_API_KEY"] = resolve_env_reference(values, "GAIA2_JUDGE_API_KEY")
        env["OPENAI_API_BASE"] = resolve_env_reference(values, "GAIA2_JUDGE_ENDPOINT")
        env["OPENAI_BASE_URL"] = env["OPENAI_API_BASE"]
        judge_model = resolve_env_reference(values, "GAIA2_JUDGE_MODEL")
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
            if values.get("GAIA2_JUDGE_API") == "responses"
            else "openai/"
        )
        judge_model = prefix + judge_model
    user_model, user_args = caller if caller else (judge_model, judge_model_args)
    env["TAU2_NL_ASSERTIONS_MODEL"] = judge_model
    env["TAU2_NL_ASSERTIONS_REASONING_EFFORT"] = judge_model_args.get(
        "reasoning_effort", "low"
    )
    from tau2.utils.utils import DATA_DIR

    output = DATA_DIR / "simulations" / args.save_to
    if (output / "results.json").exists() and not args.dry_run and not args.resume:
        raise ValueError(
            "Choose a new save-to name to preserve previous smoke attempts"
        )
    output.mkdir(parents=True, exist_ok=True)
    if caller is None:
        config = config.model_copy(
            update={
                "user_decision_llm": user_model,
                "user_decision_llm_args": user_args,
            }
        )
    if args.judge_from_user_llm:
        # The source config may point the embedded backend at a separate
        # MiniMax GAIA2 profile. Redirect that runtime-only mapping as well, so
        # the agent, user simulator, communicative judge, NL judge, and all
        # diagnostics use the same configured DeepSeek connection.
        runtime_environment = dict(config.environment)
        runtime_environment.update(
            {
                "OPENAI_API_KEY": "DEEPSEEK_API_KEY",
                "OPENAI_BASE_URL": "DEEPSEEK_BASE_URL",
                "OPENAI_MODEL": "DEEPSEEK_MODEL",
            }
        )
        config = config.model_copy(update={"environment": runtime_environment})
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
        metrics_value = yaml.safe_load(metrics_source.read_text()) or {}
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

    if args.max_seconds:
        command.extend(["--timeout", str(args.max_seconds + 150)])
    if args.user_audio_renderer == "saasr":
        if args.saasr_config is None:
            parser.error("SAASR requires --saasr-config")
        command.extend(
            [
                "--user-audio-renderer",
                "saasr",
                "--saasr-config",
                str(args.saasr_config.resolve()),
            ]
        )
    elif args.saasr_config is not None:
        parser.error("--saasr-config requires --user-audio-renderer saasr")
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
        from tau2.utils.utils import DATA_DIR

        result_file = DATA_DIR / "simulations" / args.save_to / "results.json"
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


if __name__ == "__main__":
    main()
