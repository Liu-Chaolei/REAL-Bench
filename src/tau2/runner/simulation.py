"""
Layer 1: Simulation execution.

Runs a pre-built orchestrator and evaluates the result.
No registry dependency, no config parsing, no side effects.
"""

from typing import Callable, Optional, Union

from loguru import logger

from tau2.data_model.semantic_communication import CommunicateScoring
from tau2.data_model.simulation import SimulationRun
from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
from tau2.evaluator.evaluator_base import evaluation_error_info
from tau2.evaluator.outcome import can_grade, stamp_grading
from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator
from tau2.orchestrator.modes import CommunicationMode
from tau2.orchestrator.orchestrator import Orchestrator


def run_simulation(
    orchestrator: Union[Orchestrator, FullDuplexOrchestrator],
    *,
    evaluation_type: EvaluationType = EvaluationType.ALL,
    env_kwargs: Optional[dict] = None,
    communicate_scoring: Optional[CommunicateScoring] = None,
    canonical_tasks: bool = False,
    on_trajectory_ready: Optional[Callable[[SimulationRun], None]] = None,
    metrics_config: Optional[dict] = None,
    active_metrics_config: Optional[dict] = None,
    trial: Optional[int] = None,
    diagnostics_dir=None,
) -> SimulationRun:
    """Run a simulation and evaluate the result.

    Takes a fully constructed orchestrator (with agent, user, environment, and task
    already wired in), runs the simulation, evaluates it, and returns the result
    with reward_info attached.

    This is the lowest-level entry point. It has no dependency on the registry
    or RunConfig -- everything is already encapsulated in the orchestrator.

    Args:
        orchestrator: A fully constructed Orchestrator (half-duplex) or
            FullDuplexOrchestrator (full-duplex/voice). Must have agent, user,
            environment, and task set.
        evaluation_type: The type of evaluation to perform. Defaults to ALL.
        env_kwargs: Additional kwargs passed to the evaluator's environment
            constructor (e.g., retrieval_variant for banking_knowledge).
        communicate_scoring: Scorer for orchestrators built without a call
            context; defaults to the pinned semantic judge. An orchestrator's
            call context always supplies its own scorer.
        canonical_tasks: Whether the run's plan bound the canonical registry
            tasks (voice-v1); the call's language reload must repeat it.
        on_trajectory_ready: Optional checkpoint callback, invoked with the full
            completed conversation before any grading model or state comparison.

    Returns:
        SimulationRun with reward_info attached.

    Example:
        # Build your own instances (no registry needed):
        env = MyEnvironment()
        agent = MyAgent(tools=env.get_tools(), domain_policy=env.get_policy())
        user = UserSimulator(llm="gpt-4.1", instructions=task.user_scenario,
                             tools=env.get_user_tools())
        orchestrator = Orchestrator(
            domain="airline", agent=agent, user=user,
            environment=env, task=task, max_steps=100,
        )
        result = run_simulation(orchestrator)
        print(result.reward_info.reward)
    """
    from tau2.persistent_tasks.availability import reject_retired_task

    reject_retired_task(orchestrator.task)
    # Direct callers of this layer need the same fresh scenario for replay that
    # batch execution passes explicitly. Resolve it before any provider work.
    call_context = orchestrator.call_context
    customer_language = None
    if call_context is not None:
        from tau2.domains.gaia2.language_runtime import (
            language_environment_kwargs,
            task_digest,
        )
        from tau2.runner.presets import reload_recorded_call

        context = call_context.language
        loaded = reload_recorded_call(
            context,
            orchestrator.environment.get_domain_name(),
            canonical_tasks=canonical_tasks,
        )
        if task_digest(orchestrator.task) != context.task_sha256:
            raise ValueError(
                "Evaluation task differs from the constructed language call"
            )
        expected_kwargs = language_environment_kwargs(loaded)
        if expected_kwargs:
            if env_kwargs is None:
                env_kwargs = expected_kwargs
            else:
                if any(
                    key not in env_kwargs or env_kwargs[key] != value
                    for key, value in expected_kwargs.items()
                ):
                    raise ValueError(
                        "Explicit evaluation environment differs from the constructed language call"
                    )
                # Retrieval and intake add domain-specific constructor arguments
                # alongside the pinned regional database and policy inputs.
                env_kwargs = {**env_kwargs, **expected_kwargs}
        if env_kwargs is not None and (
            "task" in env_kwargs and env_kwargs["task"] != orchestrator.task
        ):
            raise ValueError(
                "Evaluation task differs from the constructed language call"
            )
        if communicate_scoring is not None and (
            communicate_scoring != call_context.communicate_scoring
        ):
            raise ValueError("communicate scoring differs from the call context")
        communicate_scoring = call_context.communicate_scoring
        customer_language = context.provider_language
    communicate_scoring = communicate_scoring or CommunicateScoring()
    for key, value in getattr(
        orchestrator.environment, "evaluation_kwargs", lambda: {}
    )().items():
        if env_kwargs is not None and key in env_kwargs and env_kwargs[key] != value:
            raise ValueError(
                f"Evaluation constructor input differs from the live environment: {key}"
            )
        env_kwargs = {**(env_kwargs or {}), key: value}

    from tau2.domains.gaia2.interaction_variants import variant_for

    variant = variant_for(orchestrator.task)
    scenario_mechanism_manager = None
    if variant is not None:
        from tau2.metrics.active.config import ActiveMetricsConfig
        from tau2.metrics.scenario_mechanisms import ScenarioMechanismManager

        mechanism_config = None
        if active_metrics_config is not None:
            mechanism_config = ActiveMetricsConfig.model_validate(
                active_metrics_config
            ).persistent_task
        scenario_mechanism_manager = ScenarioMechanismManager(
            orchestrator=orchestrator,
            variant=variant,
            config=mechanism_config,
            seed=getattr(orchestrator, "seed", 0) or 0,
            trial=trial,
        )
        scenario_mechanism_manager.install()
        orchestrator.scenario_mechanism_manager = scenario_mechanism_manager
        # A variant is a scenario mechanism; do not silently turn it into a
        # regular active persistent-task metric or legacy runtime.
        active_metrics_config = None

    active_manager = None

    recorder = None
    # Collect evidence once. Semantic diagnostics run only when explicitly
    # selected in this run's metrics configuration and share its budget.
    if (
        metrics_config is not None
        or active_metrics_config is not None
        or diagnostics_dir is not None
    ):
        from pathlib import Path

        from tau2.diagnostics.collect import EvidenceRecorder
        from tau2.diagnostics.config import MetricsConfig

        if metrics_config is None:
            metrics_config = MetricsConfig(measures=[], auto_score=False)
        elif "model" not in metrics_config and communicate_scoring.judge is not None:
            metrics_config = {
                **metrics_config,
                "model": {
                    "model": communicate_scoring.judge.model,
                    "args": {
                        k: v
                        for k, v in communicate_scoring.judge.model_args.items()
                        if k != "api_key"
                    },
                },
            }
        metrics_config = MetricsConfig.model_validate(metrics_config)
        if diagnostics_dir is None:
            raise ValueError("Diagnostics require an artifact directory")
        diagnostics_dir = Path(diagnostics_dir)
        recorder = EvidenceRecorder(
            orchestrator.environment, orchestrator.task, diagnostics_dir / "evidence"
        )
        recorder.metrics_config = metrics_config.model_dump(mode="json")
        orchestrator.environment.diagnostic_recorder = recorder
        orchestrator.user.diagnostic_recorder = recorder
        agent = orchestrator.agent
        agent.diagnostic_recorder = recorder
        if getattr(agent, "provider", None) == "qwen_agent":
            agent.adapter.provider.diagnostics_enabled = True

    if active_metrics_config is not None:
        from tau2.metrics.active.config import ActiveMetricsConfig
        from tau2.metrics.active.manager import ActiveMetricManager

        active_config = ActiveMetricsConfig.model_validate(active_metrics_config)
        active_manager = ActiveMetricManager(
            active_config,
            orchestrator=orchestrator,
            seed=getattr(orchestrator, "seed", 0) or 0,
            trial=trial,
        )
        active_manager.install()
        orchestrator.active_metric_manager = active_manager

    # Run the orchestrator
    try:
        simulation = orchestrator.run()
        if active_manager is not None:
            active_info = dict((simulation.info or {}).get("active_metrics", {}))
            try:
                active_info["scores"] = active_manager.score(communicate_scoring)
            except Exception as error:
                active_info["scores"] = {
                    "persistent_task": {
                        "status": "evidence_unavailable",
                        "reason": type(error).__name__,
                    }
                }
            simulation.info = {**(simulation.info or {}), "active_metrics": active_info}
        if scenario_mechanism_manager is not None:
            mechanism_info = dict(
                (simulation.info or {}).get("scenario_mechanisms", {})
            )
            mechanism_info["scores"] = scenario_mechanism_manager.score(
                communicate_scoring
            )
            simulation.info = {
                **(simulation.info or {}),
                "scenario_mechanisms": mechanism_info,
            }
    except BaseException:
        if recorder is not None:
            recorder.close()
        if active_manager is not None:
            active_manager.finalize(
                getattr(orchestrator.termination_reason, "value", "error"),
                getattr(orchestrator.environment.scenario_runtime, "time", lambda: 0)(),
            )
        if scenario_mechanism_manager is not None:
            scenario_mechanism_manager.finalize(
                getattr(orchestrator.termination_reason, "value", "error"),
                getattr(orchestrator.environment.scenario_runtime, "time", lambda: 0)(),
            )
        raise
    if call_context is not None:
        simulation.language_context = call_context.language
    evidence = None
    if recorder is not None:
        from tau2.diagnostics.collect import finalize_evidence

        try:
            evidence = finalize_evidence(
                orchestrator, simulation, recorder, diagnostics_dir / "evidence"
            )
            simulation.diagnostics_ref = {
                "evidence": str(diagnostics_dir / "evidence"),
                "status": "collected",
            }
        except Exception as error:
            simulation.diagnostics_ref = {
                "status": "collection_error",
                "reason": type(error).__name__,
            }

    def finalize_diagnostics():
        if evidence is not None and metrics_config.auto_score:
            from tau2.diagnostics.suite import evaluate_evidence

            try:
                result = evaluate_evidence(
                    orchestrator.task,
                    evidence,
                    diagnostics_dir / "evidence",
                    metrics_config,
                )
                simulation.diagnostics_ref.update(result)
            except Exception as error:
                simulation.diagnostics_ref.update(
                    status="grading_error", reason=type(error).__name__
                )

    agent = getattr(orchestrator, "agent", None)
    adapter = getattr(agent, "_adapter", None)
    if getattr(agent, "provider", None) == "qwen_agent" and adapter:
        # No complete frontend/backend usage ledger is reported by the Gateway.
        simulation.agent_cost = None
        simulation.agent_system_metadata = {
            **adapter.provider.metadata,
            "observer_asr": adapter.config.asr_model,
            "transcript_origin": "recorded_audio",
            "tick_duration_ms": adapter.tick_duration_ms,
            "observer_finalization_latency": adapter.transcriber.summary(),
        }
    if call_context is not None:
        from tau2.voice.voice_gender import VOICE_GENDER_CATALOG_REVISION

        simulation.agent_voice = call_context.agent_voice
        simulation.agent_voice_gender = call_context.agent_voice_gender
        if call_context.agent_voice or call_context.agent_voice_gender:
            simulation.agent_voice_catalog = VOICE_GENDER_CATALOG_REVISION

    # Save the actual policy used for this simulation
    simulation.policy = orchestrator.environment.get_policy()
    user = getattr(orchestrator, "user", None)
    recovery = getattr(user, "_candidate_recovery_events", None)
    if recovery is not None and getattr(user, "scenario_constraints", None) is not None:
        simulation.info = {
            **(simulation.info or {}),
            "candidate_recovery": list(recovery),
            "candidate_recovery_settings": {"max_retries": user.candidate_max_retries},
        }

    # Extract context from the orchestrator -- no external params needed
    domain = orchestrator.environment.get_domain_name()
    task = orchestrator.task
    is_full_duplex = isinstance(orchestrator, FullDuplexOrchestrator)
    mode = (
        CommunicationMode.FULL_DUPLEX
        if is_full_duplex
        else CommunicationMode.HALF_DUPLEX
    )
    solo_mode = getattr(orchestrator, "solo_mode", False)

    # Evaluate
    stamp_grading(simulation)
    simulation.evaluation_status = "pending"
    if on_trajectory_ready is not None:
        on_trajectory_ready(simulation)
    try:
        reward_info = evaluate_simulation(
            simulation=simulation,
            task=task,
            evaluation_type=evaluation_type,
            solo_mode=solo_mode,
            domain=domain,
            mode=mode,
            env_kwargs=env_kwargs,
            communicate_scoring=communicate_scoring,
            customer_language=customer_language,
        )
    except Exception as error:
        # Preserve the actual completed conversation, never a new empty run.
        simulation.reward_info = None
        simulation.evaluation_status = "failed"
        simulation.evaluation_error = evaluation_error_info(error)
        logger.warning(
            f"Scoring failed for simulation {simulation.id}: {type(error).__name__}"
        )
        finalize_diagnostics()
        return simulation
    simulation.reward_info = reward_info
    simulation.evaluation_status = (
        "completed" if can_grade(simulation.termination_reason) else "skipped"
    )

    logger.info(
        f"Simulation complete: domain={domain}, task={task.id}, "
        f"reward={reward_info.reward}"
    )

    finalize_diagnostics()
    return simulation
