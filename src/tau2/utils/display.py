import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tau2.config import TERM_DARK_MODE
from tau2.data_model.audio_effects import EffectEvent, EffectTimeline
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    SystemMessage,
    Tick,
    ToolMessage,
    UserMessage,
)
from tau2.data_model.simulation import (
    Review,
    RunConfig,
    SimulationRun,
    UserOnlyReview,
    VoiceRunConfig,
)
from tau2.data_model.tasks import Action, Task
from tau2.data_model.transcript import DisplayTick
from tau2.metrics.agent_metrics import AgentMetrics, is_successful
from tau2.metrics.outcome import result_metric
from tau2.utils.transcript.display import display_tick_ranges

if TYPE_CHECKING:
    from tau2.agent.base.streaming import StreamingState
    from tau2.data_model.simulation import Info
    from tau2.data_model.transcript import ParticipantTick


@dataclass
class ColorScheme:
    """Color scheme for console display."""

    # Panel colors
    panel_border: str
    panel_title: str
    secondary_border: str
    secondary_title: str

    # Label colors
    label: str
    section_header: str

    # Message colors
    assistant_role: str
    assistant_content: str
    assistant_tool: str
    user_role: str
    user_content: str
    user_tool: str
    system_role: str
    system_content: str

    # Table colors
    table_header: str
    table_role_column: str
    table_content_column: str
    table_details_column: str


# Dark mode color scheme - optimized for dark terminal backgrounds
DARK_MODE_COLORS = ColorScheme(
    # Panel colors - using bright colors for visibility
    panel_border="bright_cyan",
    panel_title="bold bright_cyan",
    secondary_border="cyan",
    secondary_title="bold cyan",
    # Label colors
    label="white",
    section_header="bold cyan",
    # Message colors - bright variants for dark backgrounds
    assistant_role="bold bright_white",
    assistant_content="bright_white",
    assistant_tool="bright_cyan",
    user_role="bold bright_green",
    user_content="bright_green",
    user_tool="bright_yellow",
    system_role="bold bright_magenta",
    system_content="bright_magenta",
    # Table colors
    table_header="bold bright_magenta",
    table_role_column="cyan",
    table_content_column="bright_green",
    table_details_column="bright_yellow",
)

# Light mode color scheme - optimized for light terminal backgrounds
LIGHT_MODE_COLORS = ColorScheme(
    # Panel colors - using darker colors for visibility on light bg
    panel_border="magenta",
    panel_title="bold magenta",
    secondary_border="dark_cyan",
    secondary_title="bold dark_cyan",
    # Label colors
    label="grey37",
    section_header="bold dark_cyan",
    # Message colors - darker variants for light backgrounds
    assistant_role="bold blue",
    assistant_content="blue",
    assistant_tool="dark_cyan",
    user_role="bold green",
    user_content="green",
    user_tool="dark_orange",
    system_role="bold magenta",
    system_content="magenta",
    # Table colors
    table_header="bold magenta",
    table_role_column="dark_cyan",
    table_content_column="green",
    table_details_column="dark_orange",
)


def get_color_scheme() -> ColorScheme:
    """Get the appropriate color scheme based on TERM_DARK_MODE setting."""
    return DARK_MODE_COLORS if TERM_DARK_MODE else LIGHT_MODE_COLORS


class ConsoleDisplay:
    console = Console()
    colors = get_color_scheme()

    # Max characters of a tool-result payload to render. None means no limit.
    # Knowledge-domain tool results (retrieved articles) can be tens of KB,
    # which makes trajectories unreadable in the terminal viewer.
    max_tool_result_length: Optional[int] = None

    @classmethod
    def truncate_tool_result(cls, content: Optional[str]) -> str:
        if content is None:
            return ""
        limit = cls.max_tool_result_length
        if limit is None or len(content) <= limit:
            return content
        omitted = len(content) - limit
        return f"{content[:limit]} […truncated {omitted:,} chars, use --full-tool-results to show all]"

    @staticmethod
    def escape_markup(text: str) -> str:
        """
        Escape square brackets in text so Rich Console doesn't interpret them as markup.

        This is necessary for content containing tags like [sneeze] that should be displayed
        literally rather than being interpreted as Rich markup.

        Args:
            text: The text to escape

        Returns:
            Escaped text safe for Rich Console display
        """
        return escape(text)

    @classmethod
    def display_run_config(cls, config: RunConfig):
        c = cls.colors

        # Use effective values from config properties
        effective_max_steps = (
            config.effective_max_steps
            if config.effective_max_steps is not None
            else "Unlimited"
        )
        effective_agent = config.effective_agent
        effective_user = config.effective_user
        effective_agent_model = config.effective_agent_model
        effective_agent_provider = config.effective_agent_provider
        effective_user_model = config.effective_user_model

        # Build agent model string
        if effective_agent_provider:
            agent_model_str = f"{effective_agent_provider}/{effective_agent_model}"
        else:
            agent_model_str = effective_agent_model

        # Build compact header with all key info
        task_ids_str = (
            ", ".join(map(str, config.task_ids)) if config.task_ids else "All"
        )
        task_set_str = config.task_set_name if config.task_set_name else "Default"

        header_lines = [
            f"[{c.label}]Domain:[/] {config.domain}  [{c.label}]Task Set:[/] {task_set_str}  [{c.label}]Tasks:[/] {task_ids_str}",
            f"[{c.label}]Trials:[/] {config.num_trials}  [{c.label}]Max Steps:[/] {effective_max_steps}  [{c.label}]Max Errors:[/] {config.max_errors}",
            "",
            f"[{c.section_header}]Agent:[/] {effective_agent} → {agent_model_str}",
            f"[{c.section_header}]User:[/]  {effective_user} → {effective_user_model}",
        ]

        # Add save/run settings on one line
        save_to = config.save_to or "Not specified"
        header_lines.append("")
        header_lines.append(
            f"[{c.label}]Save:[/] {save_to}  [{c.label}]Concurrency:[/] {config.max_concurrency}  [{c.label}]Verbose:[/] {config.verbose_logs}"
        )

        header_content = Panel(
            "\n".join(header_lines),
            title=f"[{c.panel_title}]Simulation Configuration",
            border_style=c.panel_border,
        )
        cls.console.print(header_content)

        # Build audio-native config panel if applicable
        if isinstance(config, VoiceRunConfig):
            anc = config.audio_native_config
            bc_min = (
                f"{anc.backchannel_min_threshold_seconds}s"
                if anc.backchannel_min_threshold_seconds is not None
                else "disabled"
            )
            bc_max = (
                f"{anc.backchannel_max_threshold_seconds}s"
                if anc.backchannel_max_threshold_seconds is not None
                else "N/A"
            )

            # Use a table for cleaner display of audio params
            audio_table = Table(
                show_header=False,
                box=None,
                padding=(0, 2),
                expand=True,
            )
            audio_table.add_column("Label", style=c.label)
            audio_table.add_column("Value")
            audio_table.add_column("Label", style=c.label)
            audio_table.add_column("Value")

            # Row 1: Timing
            audio_table.add_row(
                "Tick Duration:",
                f"{anc.tick_duration_seconds}s",
                "Max Duration:",
                f"{anc.max_steps_seconds}s" if anc.max_steps_seconds else "Unlimited",
            )
            # Row 2: Sample rates
            audio_table.add_row(
                "PCM Sample Rate:",
                f"{anc.pcm_sample_rate} Hz",
                "Telephony Rate:",
                f"{anc.telephony_rate} Hz",
            )
            # Row 3: Speech
            audio_table.add_row(
                "Speech Complexity:",
                f"{config.speech_complexity}",
                "",
                "",
            )

            # Separator
            audio_table.add_row("", "", "", "")
            audio_table.add_row(
                f"[{c.section_header}]── Turn Taking ──[/]",
                "",
                f"[{c.section_header}]── Behavior ──[/]",
                "",
            )
            audio_table.add_row(
                "Wait (other):",
                f"{anc.wait_to_respond_threshold_other_seconds}s",
                "",
                "",
            )
            audio_table.add_row(
                "Wait (self):",
                f"{anc.wait_to_respond_threshold_self_seconds}s",
                "",
                "",
            )
            audio_table.add_row(
                "Yield (interrupted):",
                f"{anc.yield_threshold_when_interrupted_seconds}s",
                "Send Audio Instant:",
                f"{anc.send_audio_instant}",
            )
            audio_table.add_row(
                "Yield (interrupting):",
                f"{anc.yield_threshold_when_interrupting_seconds}s",
                "",
                "",
            )
            audio_table.add_row(
                "Interruption Check:",
                f"{anc.interruption_check_interval_seconds}s",
                "",
                "",
            )

            # Separator
            audio_table.add_row("", "", "", "")
            audio_table.add_row(
                f"[{c.section_header}]── Processing ──[/]",
                "",
                f"[{c.section_header}]── Backchannel ──[/]",
                "",
            )
            # Determine backchannel policy
            bc_policy = "LLM" if anc.use_llm_backchannel else "Poisson"
            audio_table.add_row(
                "Integration Duration:",
                f"{anc.integration_duration_seconds}s",
                "Policy:",
                bc_policy,
            )
            audio_table.add_row(
                "Silence Annotation:",
                f"{anc.silence_annotation_threshold_seconds}s",
                "Min Threshold:",
                bc_min if not anc.use_llm_backchannel else "N/A (LLM)",
            )
            audio_table.add_row(
                "",
                "",
                "Max Threshold:",
                bc_max if not anc.use_llm_backchannel else "N/A (LLM)",
            )
            audio_table.add_row(
                "",
                "",
                "Poisson Rate:",
                (
                    f"{anc.backchannel_poisson_rate}/s"
                    if not anc.use_llm_backchannel
                    else "N/A (LLM)"
                ),
            )

            audio_content = Panel(
                audio_table,
                title=f"[{c.panel_title}]Audio Native Configuration",
                border_style=c.panel_border,
            )
            cls.console.print(audio_content)

    @classmethod
    def display_task(cls, task: Task):
        c = cls.colors
        # Build content string showing only non-None fields
        content_parts = []

        if task.id is not None:
            content_parts.append(f"[{c.label}]ID:[/] {task.id}")

        if task.description:
            if task.description.purpose:
                content_parts.append(
                    f"[{c.label}]Purpose:[/] {task.description.purpose}"
                )
            if task.description.relevant_policies:
                content_parts.append(
                    f"[{c.label}]Relevant Policies:[/] {task.description.relevant_policies}"
                )
            if task.description.notes:
                content_parts.append(f"[{c.label}]Notes:[/] {task.description.notes}")

        # User Scenario section
        scenario_parts = []
        # Persona
        if task.user_scenario.persona:
            scenario_parts.append(
                f"[{c.label}]Persona:[/] {task.user_scenario.persona}"
            )

        # User Instruction
        scenario_parts.append(
            f"[{c.label}]Task Instructions:[/] {task.user_scenario.instructions}"
        )

        if scenario_parts:
            content_parts.append(
                f"[{c.section_header}]User Scenario:[/]\n" + "\n".join(scenario_parts)
            )

        # Initial State section
        if task.initial_state:
            initial_state_parts = []
            if task.initial_state.initialization_data:
                initial_state_parts.append(
                    f"[{c.label}]Initialization Data:[/]\n{task.initial_state.initialization_data.model_dump_json(indent=2)}"
                )
            if task.initial_state.initialization_actions:
                initial_state_parts.append(
                    f"[{c.label}]Initialization Actions:[/]\n{json.dumps([a.model_dump() for a in task.initial_state.initialization_actions], indent=2)}"
                )
            if task.initial_state.message_history:
                initial_state_parts.append(
                    f"[{c.label}]Message History:[/]\n{json.dumps([m.model_dump() for m in task.initial_state.message_history], indent=2)}"
                )

            if initial_state_parts:
                content_parts.append(
                    f"[{c.section_header}]Initial State:[/]\n"
                    + "\n".join(initial_state_parts)
                )

        # Evaluation Criteria section
        if task.evaluation_criteria:
            eval_parts = []
            if task.evaluation_criteria.actions:
                eval_parts.append(
                    f"[{c.label}]Required Actions:[/]\n{json.dumps([a.model_dump() for a in task.evaluation_criteria.actions], indent=2)}"
                )
            if task.evaluation_criteria.env_assertions:
                eval_parts.append(
                    f"[{c.label}]Env Assertions:[/]\n{json.dumps([a.model_dump() for a in task.evaluation_criteria.env_assertions], indent=2)}"
                )
            if task.evaluation_criteria.communicate_info:
                eval_parts.append(
                    f"[{c.label}]Information to Communicate:[/]\n{json.dumps(task.evaluation_criteria.communicate_info, indent=2)}"
                )
            if eval_parts:
                content_parts.append(
                    f"[{c.section_header}]Evaluation Criteria:[/]\n"
                    + "\n".join(eval_parts)
                )
        content = "\n\n".join(content_parts)

        # Create and display panel
        task_panel = Panel(
            content,
            title=f"[{c.panel_title}]Task Details",
            border_style=c.panel_border,
            expand=True,
        )

        cls.console.print(task_panel)

    @classmethod
    def display_simulation(
        cls,
        simulation: SimulationRun,
        show_details: bool = True,
        consolidated_ticks: bool = True,
        tick_duration_ms: Optional[int] = None,
    ):
        """
        Display the simulation content in a formatted way using Rich library.

        Args:
            simulation: The simulation object to display
            show_details: Whether to show detailed information
            consolidated_ticks: If True, group consecutive text chunks in tick display.
                               If False, show each tick as a separate row.
            tick_duration_ms: Duration of each tick in milliseconds. If provided,
                             used to display time column in tick trajectory. If None,
                             will try to read from simulation.info["tick_duration_ms"].
        """
        c = cls.colors
        # Create main simulation info panel
        sim_info = Text()
        if show_details:
            sim_info.append("Simulation ID: ", style=c.section_header)
            sim_info.append(f"{simulation.id}\n")
        sim_info.append("Task ID: ", style=c.section_header)
        sim_info.append(f"{simulation.task_id}\n")
        sim_info.append("Trial: ", style=c.section_header)
        sim_info.append(f"{simulation.trial}\n")
        if show_details:
            sim_info.append("Start Time: ", style=c.section_header)
            sim_info.append(f"{simulation.start_time}\n")
            sim_info.append("End Time: ", style=c.section_header)
            sim_info.append(f"{simulation.end_time}\n")
        sim_info.append("Duration: ", style=c.section_header)
        sim_info.append(f"{simulation.duration:.2f}s\n")
        sim_info.append("Mode: ", style=c.section_header)
        sim_info.append(f"{simulation.mode}\n")
        sim_info.append("Termination Reason: ", style=c.section_header)
        sim_info.append(f"{simulation.termination_reason}\n")
        if simulation.evaluation_status == "failed":
            sim_info.append("Evaluation: failed (unscored)\n", style=c.section_header)
        if show_details and simulation.agent_cost is not None:
            sim_info.append("Agent Cost: ", style=c.section_header)
            sim_info.append(f"${simulation.agent_cost:.4f}\n")
        if show_details and simulation.user_cost is not None:
            sim_info.append("User Cost: ", style=c.section_header)
            sim_info.append(f"${simulation.user_cost:.4f}\n")
        if simulation.diagnostics_ref:
            diagnostics = simulation.diagnostics_ref
            sim_info.append("Diagnostics: ", style=c.section_header)
            sim_info.append("\n")
            for name, result in (
                diagnostics.get("summary", {}).get("measures", {}).items()
            ):
                if name == "interaction":
                    rate = result.get("effective_interaction_rate")
                    rate_text = f"{rate:.1%}" if rate is not None else "N/A"
                    sim_info.append(f"  interaction: {rate_text}\n")
                    continue
                value = (
                    result.get("f1")
                    if name == "intent"
                    else result.get("total", {}).get("score")
                    if name == "execution"
                    else result.get("status")
                )
                sim_info.append(f"  {name}: {value}\n")
                if name == "conciseness":
                    trial = result.get("trial", {})
                    sim_info.append(
                        f"    feedback version: {result.get('version', 'tau-feedback-conciseness/1')}\n"
                        f"    coverage={trial.get('required_coverage')} "
                        f"efficiency={trial.get('length_efficiency')} "
                        f"score={trial.get('feedback_score')}\n"
                    )
                if show_details and name == "intent" and "repetition" in result:
                    repetition = result["repetition"]
                    sim_info.append(
                        f"    Content precision: {result['content_scores'].get('precision')}; "
                        f"final P/R/F1: {result.get('precision')}/"
                        f"{result.get('recall')}/{result.get('f1')}\n"
                        f"    Duplicate delegation check: {repetition['status']}; "
                        f"extra goals: {repetition['extra_goal_count']}; "
                        f"checked: {repetition['checked_goal_count']}/"
                        f"{repetition['total_goal_count']}\n"
                    )
                    for group in repetition.get("groups", []):
                        sim_info.append(
                            f"      Goals: {group['goal_paths']}; requested: "
                            f"{group['allowed_count']}; extra: {group['extra_goal_paths']}\n"
                            f"      {group['reason']}\n"
                        )
                        for citation in group["evidence"]:
                            sim_info.append(
                                f"        {citation['side']} {citation['source_id']}: {citation['quote']}\n"
                            )
                    for adjustment in repetition.get("adjustments", []):
                        sim_info.append(
                            f"      {adjustment['item_id']} ({adjustment['goal_path']}): "
                            f"{adjustment['original_status']} -> {adjustment['final_status']}\n"
                        )
                    if repetition.get("reason"):
                        sim_info.append(f"    Check reason: {repetition['reason']}\n")
            if diagnostics.get("directory"):
                sim_info.append(
                    "  Evidence and score details: " + diagnostics["directory"] + "\n"
                )
        if simulation.reward_info:
            label, value = result_metric(simulation)
            sim_info.append(label + ": ", style=c.section_header)
            if value is None:
                sim_info.append("unavailable\n")
            else:
                marker = "✅" if is_successful(value) else "❌"
                sim_info.append(f"{marker} {value:.4f}\n")

            # Add DB check info if present
            if simulation.reward_info.db_check:
                sim_info.append("\nDB Check:", style=c.system_role)
                sim_info.append(
                    f"{'✅' if simulation.reward_info.db_check.db_match else '❌'} {simulation.reward_info.db_check.db_reward}\n"
                )

            # Add env assertions if present
            if simulation.reward_info.env_assertions:
                sim_info.append("\nEnv Assertions:\n", style=c.system_role)
                for i, assertion in enumerate(simulation.reward_info.env_assertions):
                    sim_info.append(
                        f"- {i}: {assertion.env_assertion.env_type} {assertion.env_assertion.func_name} {'✅' if assertion.met else '❌'} {assertion.reward}\n"
                    )

            # Add action checks if present
            if simulation.reward_info.action_checks:
                sim_info.append("\nAction Checks:\n", style=c.system_role)
                for i, check in enumerate(simulation.reward_info.action_checks):
                    tool_type_str = (
                        f" [{check.tool_type.value}]" if check.tool_type else ""
                    )
                    requestor_str = (
                        "user" if check.action.requestor == "user" else "agent"
                    )
                    sim_info.append(
                        f"- {i}: {requestor_str} {check.action.name}{tool_type_str} {'✅' if check.action_match else '❌'} {check.action_reward}\n"
                    )
                # Add partial reward breakdown
                partial = simulation.reward_info.partial_action_reward
                if partial:
                    total = partial["total"]
                    sim_info.append(
                        f"\nPartial Action Reward: ", style=c.section_header
                    )
                    sim_info.append(
                        f"{total['correct']}/{total['count']} ({total['proportion']:.1%})\n"
                    )
                    if partial.get("read"):
                        read = partial["read"]
                        sim_info.append(
                            f"  Read:  {read['correct']}/{read['count']} ({read['proportion']:.1%})\n"
                        )
                    if partial.get("write"):
                        write = partial["write"]
                        sim_info.append(
                            f"  Write: {write['correct']}/{write['count']} ({write['proportion']:.1%})\n"
                        )

            # Add communication checks if present
            if simulation.reward_info.communicate_checks:
                sim_info.append("\nCommunicate Checks:\n", style=c.system_role)
                for i, check in enumerate(simulation.reward_info.communicate_checks):
                    sim_info.append(
                        f"- {i}: {check.info} {'✅' if check.met else '❌'}\n"
                    )

            # Add NL assertions if present
            if simulation.reward_info.nl_assertions:
                sim_info.append("\nNL Assertions:\n", style=c.system_role)
                for i, assertion in enumerate(simulation.reward_info.nl_assertions):
                    sim_info.append(
                        f"- {i}: {assertion.nl_assertion} {'✅' if assertion.met else '❌'}\n\t{assertion.justification}\n"
                    )

            # Add additional info if present
            if simulation.reward_info.info:
                sim_info.append("\nAdditional Info:\n", style=c.system_role)
                for key, value in simulation.reward_info.info.items():
                    sim_info.append(f"{key}: {value}\n")

        cls.console.print(
            Panel(sim_info, title="Simulation Overview", border_style=c.panel_border)
        )

        # Display trajectory based on mode
        if show_details:
            # Check if this is a full-duplex simulation
            is_full_duplex = (
                simulation.mode == "FULL_DUPLEX" or simulation.ticks is not None
            )

            if is_full_duplex and simulation.ticks:
                # Use consolidated tick display for full-duplex mode
                # Convert ticks from dict to Tick objects if needed
                from tau2.data_model.message import Tick

                ticks = []
                for tick_data in simulation.ticks:
                    if isinstance(tick_data, dict):
                        ticks.append(Tick.model_validate(tick_data))
                    else:
                        ticks.append(tick_data)

                cls.display_ticks(
                    ticks,
                    consolidated=consolidated_ticks,
                    tick_duration_in_ms=tick_duration_ms,
                    effect_timeline=simulation.effect_timeline,
                )

                if simulation.effect_timeline:
                    cls.display_effect_configs(simulation)
                    cls.display_effect_timeline(simulation.effect_timeline)
            elif simulation.messages:
                # Half-duplex: use traditional messages table
                table = Table(
                    title="Messages",
                    show_header=True,
                    header_style=c.table_header,
                    show_lines=True,  # Add horizontal lines between rows
                )
                table.add_column("Role", style=c.table_role_column, no_wrap=True)
                table.add_column("Content", style=c.table_content_column)
                table.add_column("Details", style=c.table_details_column)
                table.add_column("Turn", style=c.table_details_column, no_wrap=True)

                current_turn = None
                for msg in simulation.messages:
                    if isinstance(msg, ToolMessage):
                        content = cls.escape_markup(
                            cls.truncate_tool_result(msg.content)
                        )
                    else:
                        content = (
                            cls.escape_markup(msg.content)
                            if msg.content is not None
                            else ""
                        )
                    details = ""

                    # Set different colors based on message type
                    if isinstance(msg, AssistantMessage):
                        role_style = c.assistant_role
                        content_style = c.assistant_content
                        tool_style = c.assistant_tool
                    elif isinstance(msg, UserMessage):
                        role_style = c.user_role
                        content_style = c.user_content
                        tool_style = c.user_tool
                    elif isinstance(msg, ToolMessage):
                        # For tool messages, use the color of the requestor's tool style
                        if msg.requestor == "user":
                            role_style = c.user_role
                            content_style = c.user_tool
                        else:  # assistant
                            role_style = c.assistant_role
                            content_style = c.assistant_tool
                        tool_style = content_style
                    else:  # SystemMessage
                        role_style = c.system_role
                        content_style = c.system_content
                        tool_style = c.system_content

                    if isinstance(msg, AssistantMessage) or isinstance(
                        msg, UserMessage
                    ):
                        if msg.tool_calls:
                            tool_calls = []
                            for tool in msg.tool_calls:
                                tool_calls.append(
                                    f"[{tool_style}]Tool: {tool.name}[/]\n[{tool_style}]Args: {json.dumps(tool.arguments, indent=2)}[/]"
                                )
                            details = "\n".join(tool_calls)
                    elif isinstance(msg, ToolMessage):
                        details = f"[{content_style}]Tool ID: {msg.id}. Requestor: {msg.requestor}[/]"
                        if msg.error:
                            details += " [bold red](Error)[/]"

                    # Add empty row between turns
                    if current_turn is not None and msg.turn_idx != current_turn:
                        table.add_row("", "", "", "")
                    current_turn = msg.turn_idx

                    table.add_row(
                        f"[{role_style}]{msg.role}[/]",
                        f"[{content_style}]{content}[/]",
                        details,
                        str(msg.turn_idx) if msg.turn_idx is not None else "",
                    )
                cls.console.print(table)

        # Display reviews if present
        if simulation.review is not None:
            cls.display_review(simulation.review)

        if simulation.user_only_review is not None:
            cls.display_user_only_review(simulation.user_only_review)

    @classmethod
    def display_review(
        cls,
        review: Review,
        title: str = "LLM Conversation Review",
        console: Optional[Console] = None,
    ):
        """
        Display a Review object with summary and errors.

        Args:
            review: The Review object to display.
            title: Title for the review panel.
            console: Optional Console instance. Uses class console if not provided.
        """
        if console is None:
            console = cls.console

        # Build summary panel content
        summary_lines = []
        if review.has_errors:
            if review.critical_user_error:
                status = "❌ Errors Found (Critical User Error)"
            else:
                status = "❌ Errors Found"
        else:
            status = "✅ No Errors"
        summary_lines.append(f"[bold]{status}[/bold]")

        if review.has_errors:
            error_parts = []
            if review.agent_error:
                error_parts.append("[red]🤖 Agent Error[/red]")
            if review.user_error:
                if review.critical_user_error:
                    error_parts.append("[red]👤 User Error (Critical)[/red]")
                else:
                    error_parts.append("[blue]👤 User Error (Minor)[/blue]")
            if error_parts:
                summary_lines.append(" | ".join(error_parts))

        if review.summary:
            summary_lines.append(f"\n{review.summary}")

        if review.cost is not None:
            summary_lines.append(f"\nReview cost: ${review.cost:.4f}")

        summary_panel = Panel(
            "\n".join(summary_lines),
            title=f"[bold cyan]{title}[/bold cyan]",
            border_style="cyan",
        )
        console.print(summary_panel)

        # Display errors table if there are errors
        if review.errors and review.has_errors:
            table = Table(
                title="[bold]Review Errors[/bold]",
                show_header=True,
                header_style="bold magenta",
                expand=True,
                show_lines=True,
            )
            table.add_column("#", style="white", width=4)
            table.add_column("Source", style="cyan", width=8)
            table.add_column("Severity", width=16)
            table.add_column("Error Type", style="yellow", width=14)
            table.add_column("Tags", style="magenta", no_wrap=False)
            table.add_column("Location", style="green", width=12)
            table.add_column("Reasoning", style="white", ratio=2)
            table.add_column("Correct Behavior", style="white", ratio=1)

            for i, error in enumerate(review.errors, 1):
                if error.source == "unknown":
                    continue
                reasoning_text = error.reasoning
                correct_behavior_text = (
                    error.correct_behavior if error.correct_behavior else "-"
                )
                # Show severity for both agent and user errors with color
                if error.severity:
                    if error.severity == "critical" or error.severity.startswith(
                        "critical_"
                    ):
                        severity_text = f"[red]{error.severity}[/]"
                    elif error.severity == "minor":
                        severity_text = f"[yellow]{error.severity}[/]"
                    else:
                        severity_text = error.severity
                else:
                    severity_text = "[dim]-[/]"
                # Format error tags (one per line)
                if error.error_tags:
                    tags_text = "\n".join(error.error_tags)
                else:
                    tags_text = "-"
                # Show tick range for full-duplex, turn_idx for turn-based
                if error.tick_start is not None:
                    if (
                        error.tick_end is not None
                        and error.tick_end != error.tick_start
                    ):
                        location = f"ticks {error.tick_start}-{error.tick_end}"
                    else:
                        location = f"tick {error.tick_start}"
                elif error.turn_idx is not None:
                    location = f"turn {error.turn_idx}"
                else:
                    location = "-"

                # Color the source based on who made the error
                source_style = "red" if error.source == "agent" else "blue"
                table.add_row(
                    str(i),
                    f"[{source_style}]{error.source}[/{source_style}]",
                    severity_text,
                    error.error_type or "-",
                    tags_text,
                    location,
                    reasoning_text,
                    correct_behavior_text,
                )

            console.print(table)

    @classmethod
    def display_user_only_review(
        cls,
        review: UserOnlyReview,
        title: str = "LLM User Simulator Review",
        console: Optional[Console] = None,
    ):
        """
        Display a UserOnlyReview object with summary and errors.

        Args:
            review: The UserOnlyReview object to display.
            title: Title for the review panel.
            console: Optional Console instance. Uses class console if not provided.
        """
        if console is None:
            console = cls.console

        # Build summary panel content
        summary_lines = []
        if review.has_errors:
            if review.critical_user_error:
                status = "❌ Critical User Errors Found"
            else:
                status = "⚠️ User Errors Found (Minor)"
        else:
            status = "✅ No User Errors"
        summary_lines.append(f"[bold]{status}[/bold]")

        if review.has_errors:
            if review.critical_user_error:
                summary_lines.append(
                    f"[red]👤 {len(review.errors)} User Error(s) - Critical[/red]"
                )
            else:
                summary_lines.append(
                    f"[blue]👤 {len(review.errors)} User Error(s) - Minor[/blue]"
                )

        if review.summary:
            summary_lines.append(f"\n{review.summary}")

        if review.cost is not None:
            summary_lines.append(f"\nReview cost: ${review.cost:.4f}")

        summary_panel = Panel(
            "\n".join(summary_lines),
            title=f"[bold cyan]{title}[/bold cyan]",
            border_style="cyan",
        )
        console.print(summary_panel)

        # Display errors table if there are errors
        if review.errors and review.has_errors:
            table = Table(
                title="[bold]User Simulator Errors[/bold]",
                show_header=True,
                header_style="bold magenta",
                expand=True,
                show_lines=True,
            )
            table.add_column("#", style="white", width=4)
            table.add_column("Severity", style="red", width=10)
            table.add_column("Type", style="yellow", width=14)
            table.add_column("Location", style="green", width=12)
            table.add_column("Message", style="blue", ratio=1)
            table.add_column("Reasoning", style="white", ratio=2)
            table.add_column("Correct Behavior", style="white", ratio=1)

            for i, error in enumerate(review.errors, 1):
                # Color severity
                if error.severity:
                    if error.severity.startswith("critical"):
                        severity_text = f"[red]{error.severity}[/]"
                    elif error.severity == "minor":
                        severity_text = f"[yellow]{error.severity}[/]"
                    else:
                        severity_text = error.severity
                else:
                    severity_text = "[dim]-[/]"
                # Show tick range for full-duplex, turn_idx for turn-based
                if error.tick_start is not None:
                    if (
                        error.tick_end is not None
                        and error.tick_end != error.tick_start
                    ):
                        location = f"ticks {error.tick_start}-{error.tick_end}"
                    else:
                        location = f"tick {error.tick_start}"
                elif error.turn_idx is not None:
                    location = f"turn {error.turn_idx}"
                else:
                    location = "-"

                table.add_row(
                    str(i),
                    severity_text,
                    error.error_type,
                    location,
                    error.user_message or "-",
                    error.reasoning,
                    error.correct_behavior or "-",
                )

            console.print(table)

    @classmethod
    def display_agent_metrics(cls, metrics: AgentMetrics):
        """Display capability scores; operational counts are saved in logs."""
        from rich.table import Table

        table = Table("Metric", "Value")
        table.add_row(
            "Average reward",
            f"{metrics.avg_reward:.4f}" if metrics.avg_reward is not None else "N/A",
        )
        for k, value in sorted(metrics.pass_hat_ks.items()):
            table.add_row(f"Pass^{k}", f"{value:.4f}")
        cls.console.print(table)

    @classmethod
    def display_info(cls, info: "Info"):
        """
        Display simulation run configuration/info.

        Args:
            info: The Info object containing run configuration details.
        """

        c = cls.colors

        # Build a single header panel with all run info including agent/user
        header_lines = [
            f"[{c.label}]Domain:[/] {info.environment_info.domain_name}",
            f"[{c.label}]Git Commit:[/] {info.git_commit[:12]}...",
            f"[{c.label}]Trials:[/] {info.num_trials}  [{c.label}]Max Steps:[/] {info.max_steps if info.max_steps is not None else 'Unlimited'}  [{c.label}]Max Errors:[/] {info.max_errors}",
        ]
        if info.seed is not None:
            header_lines[-1] += f"  [{c.label}]Seed:[/] {info.seed}"

        # Add agent info
        if info.audio_native_config:
            agent_model = (
                f"{info.audio_native_config.provider}/{info.audio_native_config.model}"
            )
        elif info.agent_info.llm:
            agent_model = info.agent_info.llm
        else:
            agent_model = "N/A"
        header_lines.append("")
        header_lines.append(
            f"[{c.section_header}]Agent:[/] {info.agent_info.implementation} → {agent_model}"
        )

        # Add user info
        user_model = info.user_info.llm or "N/A"
        header_lines.append(
            f"[{c.section_header}]User:[/]  {info.user_info.implementation} → {user_model}"
        )

        header_content = Panel(
            "\n".join(header_lines),
            title=f"[{c.panel_title}]Run Configuration",
            border_style=c.panel_border,
        )

        cls.console.print(header_content)

        # Build audio-native config panel if applicable
        if info.audio_native_config:
            anc = info.audio_native_config
            bc_min = (
                f"{anc.backchannel_min_threshold_seconds}s"
                if anc.backchannel_min_threshold_seconds is not None
                else "disabled"
            )
            bc_max = (
                f"{anc.backchannel_max_threshold_seconds}s"
                if anc.backchannel_max_threshold_seconds is not None
                else "N/A"
            )

            # Use a table for cleaner display of audio params
            audio_table = Table(
                show_header=False,
                box=None,
                padding=(0, 2),
                expand=True,
            )
            audio_table.add_column("Label", style=c.label)
            audio_table.add_column("Value")
            audio_table.add_column("Label", style=c.label)
            audio_table.add_column("Value")

            # Row 1: Timing
            audio_table.add_row(
                "Tick Duration:",
                f"{anc.tick_duration_seconds}s",
                "Max Duration:",
                f"{anc.max_steps_seconds}s" if anc.max_steps_seconds else "Unlimited",
            )
            # Row 2: Sample rates
            audio_table.add_row(
                "PCM Sample Rate:",
                f"{anc.pcm_sample_rate} Hz",
                "Telephony Rate:",
                f"{anc.telephony_rate} Hz",
            )
            # Row 3: Speech
            audio_table.add_row(
                "Speech Complexity:",
                f"{info.speech_complexity or 'N/A'}",
                "",
                "",
            )

            # Separator
            audio_table.add_row("", "", "", "")
            audio_table.add_row(
                f"[{c.section_header}]── Turn Taking ──[/]",
                "",
                f"[{c.section_header}]── Behavior ──[/]",
                "",
            )
            audio_table.add_row(
                "Wait (other):",
                f"{anc.wait_to_respond_threshold_other_seconds}s",
                "",
                "",
            )
            audio_table.add_row(
                "Wait (self):",
                f"{anc.wait_to_respond_threshold_self_seconds}s",
                "",
                "",
            )
            audio_table.add_row(
                "Yield (interrupted):",
                f"{anc.yield_threshold_when_interrupted_seconds}s",
                "Send Audio Instant:",
                f"{anc.send_audio_instant}",
            )
            audio_table.add_row(
                "Yield (interrupting):",
                f"{anc.yield_threshold_when_interrupting_seconds}s",
                "",
                "",
            )
            audio_table.add_row(
                "Interruption Check:",
                f"{anc.interruption_check_interval_seconds}s",
                "",
                "",
            )

            # Separator
            audio_table.add_row("", "", "", "")
            audio_table.add_row(
                f"[{c.section_header}]── Processing ──[/]",
                "",
                f"[{c.section_header}]── Backchannel ──[/]",
                "",
            )
            # Determine backchannel policy
            bc_policy = "LLM" if anc.use_llm_backchannel else "Poisson"
            audio_table.add_row(
                "Integration Duration:",
                f"{anc.integration_duration_seconds}s",
                "Policy:",
                bc_policy,
            )
            audio_table.add_row(
                "Silence Annotation:",
                f"{anc.silence_annotation_threshold_seconds}s",
                "Min Threshold:",
                bc_min if not anc.use_llm_backchannel else "N/A (LLM)",
            )
            audio_table.add_row(
                "",
                "",
                "Max Threshold:",
                bc_max if not anc.use_llm_backchannel else "N/A (LLM)",
            )
            audio_table.add_row(
                "",
                "",
                "Poisson Rate:",
                (
                    f"{anc.backchannel_poisson_rate}/s"
                    if not anc.use_llm_backchannel
                    else "N/A (LLM)"
                ),
            )

            audio_content = Panel(
                audio_table,
                title=f"[{c.panel_title}]Audio Native Configuration",
                border_style=c.panel_border,
            )
            cls.console.print(audio_content)

    # =========================================================================
    # Tick-based Trajectory Display Methods
    # =========================================================================

    @staticmethod
    def _format_time_ms(ms: int) -> str:
        """Format milliseconds as min:sec:ms."""
        minutes = ms // 60000
        remaining_ms = ms % 60000
        seconds = remaining_ms // 1000
        milliseconds = remaining_ms % 1000
        return f"{minutes}:{seconds:02d}:{milliseconds:03d}"

    @staticmethod
    def _format_seconds(ms: int) -> str:
        """Format milliseconds as a human-readable string.

        Sub-second values are shown in ms for precision (e.g. '150ms'),
        larger values in seconds with two decimals (e.g. '3.20s').
        """
        if abs(ms) < 1000:
            return f"{ms}ms"
        return f"{ms / 1000:.2f}s"

    @staticmethod
    def _format_effect_params(params: Optional[dict]) -> str:
        """Format an EffectEvent.params dict as a compact string."""
        if not params:
            return ""
        parts = []
        for k, v in params.items():
            if isinstance(v, float):
                parts.append(f"{k}={v:.1f}")
            else:
                parts.append(f"{k}={escape(str(v))}")
        return ", ".join(parts)

    @staticmethod
    def _get_overlapping_effects(
        timeline: EffectTimeline,
        start_ms: int,
        end_ms: int,
    ) -> list[EffectEvent]:
        """Return events that overlap the given time range."""
        return [
            e
            for e in timeline.events
            if e.start_ms < end_ms and (e.end_ms or float("inf")) > start_ms
        ]

    @classmethod
    def _format_effect_cell(
        cls,
        overlapping: list[EffectEvent],
    ) -> str:
        """Render overlapping effects as a compact multi-line cell string."""
        if not overlapping:
            return ""
        lines = []
        for e in overlapping:
            start = cls._format_seconds(e.start_ms)
            end = cls._format_seconds(e.end_ms) if e.end_ms is not None else "..."
            params_str = cls._format_effect_params(e.params)
            label = e.effect_type
            entry = f"{label} ({start}-{end})"
            if params_str:
                entry += f" {params_str}"
            lines.append(entry)
        return "\n".join(lines)

    @classmethod
    def display_effect_timeline(
        cls,
        timeline: Optional[EffectTimeline],
    ) -> None:
        """Print a standalone Rich table summarising the effect timeline."""
        if not timeline or not timeline.events:
            return

        c = cls.colors
        sorted_events = sorted(timeline.events, key=lambda e: e.start_ms)

        table = Table(
            title=f"Effect Timeline ({len(sorted_events)} events)",
            show_header=True,
            header_style=c.table_header,
            show_lines=True,
        )
        table.add_column("Start", style=c.table_details_column, no_wrap=True, width=8)
        table.add_column("End", style=c.table_details_column, no_wrap=True, width=8)
        table.add_column("Effect", style=c.user_tool, width=20)
        table.add_column("Participant", style=c.table_details_column, width=12)
        table.add_column("Duration", style=c.table_details_column, width=10)
        table.add_column("Details", style=c.table_details_column, overflow="fold")

        for event in sorted_events:
            start = cls._format_seconds(event.start_ms)
            end = (
                cls._format_seconds(event.end_ms) if event.end_ms is not None else "..."
            )
            duration = (
                cls._format_seconds(event.duration_ms)
                if event.duration_ms is not None
                else "-"
            )
            details = cls._format_effect_params(event.params)
            table.add_row(
                start,
                end,
                event.effect_type,
                event.participant,
                duration,
                details or "-",
            )

        cls.console.print(table)

    @classmethod
    def display_effect_configs(
        cls,
        simulation: SimulationRun,
    ) -> None:
        """Print a summary panel of the effect configuration used for this simulation."""
        env = simulation.speech_environment
        if env is None:
            return

        has_configs = (
            env.source_effects_config is not None
            or env.speech_effects_config is not None
            or env.channel_effects_config is not None
        )
        if not has_configs:
            return

        c = cls.colors
        table = Table(
            title=f"Effect Configuration (complexity={env.complexity})",
            show_header=True,
            header_style=c.table_header,
            show_lines=False,
            padding=(0, 1),
        )
        table.add_column("Parameter", style=c.user_tool, no_wrap=True)
        table.add_column("Value", style=c.table_details_column)

        if env.source_effects_config is not None:
            src = env.source_effects_config
            table.add_row("background_noise", str(src.enable_background_noise))
            table.add_row("noise_snr_db", f"{src.noise_snr_db}")
            table.add_row("noise_snr_drift_db", f"{src.noise_snr_drift_db}")
            table.add_row("noise_variation_speed", f"{src.noise_variation_speed}")
            table.add_row("burst_noise", str(src.enable_burst_noise))
            table.add_row(
                "burst_noise_events_per_min", f"{src.burst_noise_events_per_minute}"
            )
            table.add_row("burst_snr_range_db", str(src.burst_snr_range_db))

        if env.speech_effects_config is not None:
            spc = env.speech_effects_config
            table.add_row("dynamic_muffling", str(spc.enable_dynamic_muffling))
            table.add_row("muffle_probability", f"{spc.muffle_probability}")
            table.add_row("muffle_cutoff_freq", f"{spc.muffle_cutoff_freq}")
            table.add_row(
                "out_of_turn_speech",
                str(spc.enable_non_directed_phrases),
            )
            table.add_row(
                "speech_insert_events_per_min",
                f"{spc.speech_insert_events_per_minute}",
            )

        if env.channel_effects_config is not None:
            ch = env.channel_effects_config
            table.add_row("frame_drops", str(ch.enable_frame_drops))
            table.add_row("frame_drop_rate", f"{ch.frame_drop_rate}")
            table.add_row(
                "frame_drop_burst_duration_ms", f"{ch.frame_drop_burst_duration_ms}"
            )

        table.add_row("snr_speech_reference_rms", f"{env.snr_speech_reference_rms}")
        table.add_row("telephony", str(env.telephony_enabled))

        cls.console.print(table)

    @classmethod
    def display_ticks(
        cls,
        ticks: list["Tick"],
        consolidated: bool = False,
        tick_duration_in_ms: int | None = None,
        effect_timeline: Optional[EffectTimeline] = None,
    ):
        """
        Display tick-based trajectory from FullDuplexOrchestrator.

        Args:
            ticks: List of Tick objects from the orchestrator.
            consolidated: If True, group consecutive text chunks with | separators.
                         This makes very short ticks (e.g., 200ms) easier to read.
            tick_duration_in_ms: If provided, adds a column showing simulation time
                                in milliseconds (tick_id * tick_duration_in_ms).
            effect_timeline: If provided, adds an "Effects" column showing which
                            audio effects overlap each row's time range.
        """
        if consolidated:
            cls._display_ticks_consolidated(
                ticks, tick_duration_in_ms, effect_timeline=effect_timeline
            )
        else:
            cls._display_ticks_expanded(
                ticks, tick_duration_in_ms, effect_timeline=effect_timeline
            )

    @classmethod
    def _display_ticks_expanded(
        cls,
        ticks: list["Tick"],
        tick_duration_in_ms: int | None = None,
        effect_timeline: Optional[EffectTimeline] = None,
    ):
        """Display each tick as a separate row (original behavior)."""
        c = cls.colors
        show_effects = (
            effect_timeline is not None
            and tick_duration_in_ms is not None
            and len(effect_timeline.events) > 0
        )

        table = Table(
            title="Full-Duplex Tick Trajectory",
            show_header=True,
            header_style=c.table_header,
            show_lines=True,
        )
        table.add_column("Tick", style=c.table_details_column, no_wrap=True, width=4)
        if tick_duration_in_ms is not None:
            table.add_column(
                "Time", style=c.table_details_column, no_wrap=True, width=12
            )
        table.add_column("Agent", style=c.assistant_content)
        table.add_column("Agent Calls", style=c.assistant_tool, overflow="fold")
        table.add_column("Agent Results", style=c.assistant_tool, overflow="fold")
        table.add_column("Agent Turn Action", style=c.assistant_tool, overflow="fold")
        table.add_column("User", style=c.user_content)
        table.add_column("User Transcript", style=c.user_content, overflow="fold")
        table.add_column("User Calls", style=c.user_tool, overflow="fold")
        table.add_column("User Results", style=c.user_tool, overflow="fold")
        table.add_column("User Turn Action", style=c.user_tool, overflow="fold")
        if show_effects:
            table.add_column("Effects", style=c.user_tool, overflow="fold")

        for tick in ticks:
            agent_content = ""
            if tick.agent_chunk and tick.agent_chunk.content:
                agent_content = cls.escape_markup(tick.agent_chunk.content)

            agent_calls = ""
            if tick.agent_tool_calls:
                agent_calls = "\n".join(
                    f"{tc.name}({json.dumps(tc.arguments)})"
                    for tc in tick.agent_tool_calls
                )

            agent_results = ""
            if tick.agent_tool_results:
                agent_results = "\n".join(
                    cls.truncate_tool_result(r.content) for r in tick.agent_tool_results
                )

            agent_turn_action = ""
            if (
                tick.agent_chunk
                and hasattr(tick.agent_chunk, "turn_taking_action")
                and tick.agent_chunk.turn_taking_action
            ):
                action = tick.agent_chunk.turn_taking_action.action
                info = tick.agent_chunk.turn_taking_action.info
                agent_turn_action = f"{action}: {info}" if info else action

            user_content = ""
            if tick.user_chunk and tick.user_chunk.content:
                user_content = cls.escape_markup(tick.user_chunk.content)

            user_calls = ""
            if tick.user_tool_calls:
                user_calls = "\n".join(
                    f"{tc.name}({json.dumps(tc.arguments)})"
                    for tc in tick.user_tool_calls
                )

            user_results = ""
            if tick.user_tool_results:
                user_results = "\n".join(
                    cls.truncate_tool_result(r.content) for r in tick.user_tool_results
                )

            user_turn_action = ""
            if (
                tick.user_chunk
                and hasattr(tick.user_chunk, "turn_taking_action")
                and tick.user_chunk.turn_taking_action
            ):
                action = tick.user_chunk.turn_taking_action.action
                info = tick.user_chunk.turn_taking_action.info
                user_turn_action = f"{action}: {info}" if info else action

            row_data = [str(tick.tick_id)]
            if tick_duration_in_ms is not None:
                sim_time = tick.tick_id * tick_duration_in_ms
                row_data.append(cls._format_time_ms(sim_time))

            user_transcript = (
                cls.escape_markup(tick.user_transcript) if tick.user_transcript else ""
            )

            row_data.extend(
                [
                    agent_content or "-",
                    agent_calls or "-",
                    agent_results or "-",
                    agent_turn_action or "-",
                    user_content or "-",
                    user_transcript or "-",
                    user_calls or "-",
                    user_results or "-",
                    user_turn_action or "-",
                ]
            )

            if show_effects:
                tick_start_ms = tick.tick_id * tick_duration_in_ms
                tick_end_ms = tick_start_ms + tick_duration_in_ms
                overlapping = cls._get_overlapping_effects(
                    effect_timeline, tick_start_ms, tick_end_ms
                )
                row_data.append(cls._format_effect_cell(overlapping) or "-")

            table.add_row(*row_data)

        cls.console.print(table)

    @classmethod
    def _display_ticks_consolidated(
        cls,
        ticks: list["Tick"],
        tick_duration_in_ms: int | None = None,
        show_turn_actions: bool = True,
        effect_timeline: Optional[EffectTimeline] = None,
    ):
        """Display ticks with consecutive text chunks grouped together.

        This consolidates sequential chunks of the same speaker type,
        separating individual chunk contents with ' | '. Tool calls and
        results are shown separately and break consolidation.

        Empty columns are automatically dropped.
        """
        c = cls.colors

        # Column definitions with their styles and widths
        # We'll determine which columns have data and only show those
        column_defs = {
            "ticks": {"name": "Ticks", "style": c.table_details_column, "width": 8},
            "time": {"name": "Time", "style": c.table_details_column},
            "agent_content": {
                "name": "Agent",
                "style": c.assistant_content,
                "min_width": 15,
            },
            "agent_calls": {
                "name": "Agent Calls",
                "style": c.assistant_tool,
                "overflow": "fold",
            },
            "agent_results": {
                "name": "Agent Results",
                "style": c.assistant_tool,
                "overflow": "fold",
            },
            "agent_turn_action": {
                "name": "Turn Action",
                "style": c.assistant_tool,
                "overflow": "fold",
            },
            "user_content": {"name": "User", "style": c.user_content, "min_width": 15},
            "user_transcript": {
                "name": "Transcript",
                "style": c.user_content,
                "overflow": "fold",
            },
            "user_calls": {
                "name": "User Calls",
                "style": c.user_tool,
                "overflow": "fold",
            },
            "user_results": {
                "name": "User Results",
                "style": c.user_tool,
                "overflow": "fold",
            },
            "user_turn_action": {
                "name": "User Turn",
                "style": c.user_tool,
                "overflow": "fold",
            },
            "effects": {
                "name": "Effects",
                "style": c.user_tool,
                "overflow": "fold",
            },
        }

        show_effects = (
            effect_timeline is not None
            and tick_duration_in_ms is not None
            and len(effect_timeline.events) > 0
        )

        # Helper to extract info from a tick
        def extract_tick_info(tick: "Tick") -> dict:
            info = {
                "agent_content": "",
                "agent_calls": "",
                "agent_results": "",
                "agent_turn_action": "",
                "user_content": "",
                "user_transcript": "",
                "user_calls": "",
                "user_results": "",
                "user_turn_action": "",
            }

            if tick.agent_chunk and tick.agent_chunk.content:
                info["agent_content"] = cls.escape_markup(tick.agent_chunk.content)
            if tick.agent_tool_calls:
                info["agent_calls"] = "\n".join(
                    f"{tc.name}({json.dumps(tc.arguments)})"
                    for tc in tick.agent_tool_calls
                )
            if tick.agent_tool_results:
                info["agent_results"] = "\n".join(
                    cls.truncate_tool_result(r.content) for r in tick.agent_tool_results
                )
            if (
                tick.agent_chunk
                and hasattr(tick.agent_chunk, "turn_taking_action")
                and tick.agent_chunk.turn_taking_action
            ):
                action = tick.agent_chunk.turn_taking_action.action
                info_text = tick.agent_chunk.turn_taking_action.info
                info["agent_turn_action"] = (
                    f"{action}: {info_text}" if info_text else action
                )
            if tick.user_chunk and tick.user_chunk.content:
                info["user_content"] = cls.escape_markup(tick.user_chunk.content)
            if tick.user_transcript:
                info["user_transcript"] = cls.escape_markup(tick.user_transcript)
            if tick.user_tool_calls:
                info["user_calls"] = "\n".join(
                    f"{tc.name}({json.dumps(tc.arguments)})"
                    for tc in tick.user_tool_calls
                )
            if tick.user_tool_results:
                info["user_results"] = "\n".join(
                    cls.truncate_tool_result(r.content) for r in tick.user_tool_results
                )
            if (
                tick.user_chunk
                and hasattr(tick.user_chunk, "turn_taking_action")
                and tick.user_chunk.turn_taking_action
            ):
                action = tick.user_chunk.turn_taking_action.action
                info_text = tick.user_chunk.turn_taking_action.info
                info["user_turn_action"] = (
                    f"{action}: {info_text}" if info_text else action
                )
            return info

        # Helper to check if a tick has tool activity (breaks consolidation)
        def has_tool_activity(info: dict) -> bool:
            return bool(
                info["agent_calls"]
                or info["agent_results"]
                or info["user_calls"]
                or info["user_results"]
            )

        # Group ticks using shared helper
        infos = [extract_tick_info(tick) for tick in ticks]
        ranges = display_tick_ranges(
            [
                DisplayTick(
                    agent_content=info["agent_content"],
                    user_content=info["user_content"],
                    agent_turn_action=info["agent_turn_action"],
                    user_turn_action=info["user_turn_action"],
                    has_tool_activity=has_tool_activity(info),
                )
                for info in infos
            ]
        )
        groups = [
            (ticks[r.start].tick_id, ticks[r.end - 1].tick_id, infos[r.start : r.end])
            for r in ranges
        ]

        # Helper to consolidate turn-taking actions with count
        def consolidate_actions(actions: list[str]) -> str:
            """Group consecutive identical actions with count (e.g., 'X (x4)')."""
            if not actions:
                return ""
            result = []
            current_action = actions[0]
            count = 1
            for action in actions[1:]:
                if action == current_action:
                    count += 1
                else:
                    if count > 1:
                        result.append(f"{current_action} (x{count})")
                    else:
                        result.append(current_action)
                    current_action = action
                    count = 1
            # Handle the last group
            if count > 1:
                result.append(f"{current_action} (x{count})")
            else:
                result.append(current_action)
            return "\n".join(result)

        # First pass: build all row data and track which columns have content
        all_rows = []
        columns_with_data = set(["ticks"])  # Ticks is always shown
        if tick_duration_in_ms is not None:
            columns_with_data.add("time")

        for start_tick, end_tick, infos in groups:
            tick_label = (
                str(start_tick)
                if start_tick == end_tick
                else f"{start_tick}-{end_tick}"
            )

            # Time label - show start and end on separate lines
            time_label = ""
            if tick_duration_in_ms is not None:
                start_time = start_tick * tick_duration_in_ms
                end_time = end_tick * tick_duration_in_ms
                if start_tick == end_tick:
                    time_label = cls._format_time_ms(start_time)
                else:
                    time_label = f"{cls._format_time_ms(start_time)}\n{cls._format_time_ms(end_time)}"

            # Consolidate text content (join directly without separators)
            agent_parts = [i["agent_content"] for i in infos if i["agent_content"]]
            user_parts = [i["user_content"] for i in infos if i["user_content"]]
            user_transcript_parts = [
                i["user_transcript"] for i in infos if i["user_transcript"]
            ]

            agent_content = "".join(agent_parts) if agent_parts else ""
            user_content = "".join(user_parts) if user_parts else ""
            user_transcript = (
                "".join(user_transcript_parts) if user_transcript_parts else ""
            )

            # Tool calls/results don't consolidate (only one tick has them per group)
            agent_calls = next(
                (i["agent_calls"] for i in infos if i["agent_calls"]), ""
            )
            agent_results = next(
                (i["agent_results"] for i in infos if i["agent_results"]), ""
            )
            user_calls = next((i["user_calls"] for i in infos if i["user_calls"]), "")
            user_results = next(
                (i["user_results"] for i in infos if i["user_results"]), ""
            )

            # Turn-taking actions
            agent_turn_actions = [
                i["agent_turn_action"] for i in infos if i["agent_turn_action"]
            ]
            agent_turn_action = consolidate_actions(agent_turn_actions)

            user_turn_actions = [
                i["user_turn_action"] for i in infos if i["user_turn_action"]
            ]
            user_turn_action = consolidate_actions(user_turn_actions)

            # Effects column
            effects_cell = ""
            if show_effects:
                row_start_ms = start_tick * tick_duration_in_ms
                row_end_ms = (end_tick + 1) * tick_duration_in_ms
                overlapping = cls._get_overlapping_effects(
                    effect_timeline, row_start_ms, row_end_ms
                )
                effects_cell = cls._format_effect_cell(overlapping)

            # Build row data dict
            row = {
                "ticks": tick_label,
                "time": time_label,
                "agent_content": (
                    cls.escape_markup(agent_content) if agent_content else ""
                ),
                "agent_calls": agent_calls,
                "agent_results": agent_results,
                "agent_turn_action": agent_turn_action,
                "user_content": cls.escape_markup(user_content) if user_content else "",
                "user_transcript": user_transcript,
                "user_calls": user_calls,
                "user_results": user_results,
                "user_turn_action": user_turn_action,
                "effects": effects_cell,
            }

            # Track which columns have data
            for key, value in row.items():
                if value and value != "-":
                    columns_with_data.add(key)

            all_rows.append(row)

        # Determine which columns to show (in order)
        column_order = [
            "ticks",
            "time",
            "agent_content",
            "agent_calls",
            "agent_results",
        ]
        if show_turn_actions:
            column_order.append("agent_turn_action")
        column_order.extend(
            ["user_content", "user_transcript", "user_calls", "user_results"]
        )
        if show_turn_actions:
            column_order.append("user_turn_action")
        if show_effects:
            column_order.append("effects")

        # Filter to only columns with data
        active_columns = [col for col in column_order if col in columns_with_data]

        # Build the table with only active columns
        table = Table(
            title="Full-Duplex Tick Trajectory (Consolidated)",
            show_header=True,
            header_style=c.table_header,
            show_lines=True,
        )

        for col_key in active_columns:
            col_def = column_defs[col_key]
            kwargs = {"style": col_def["style"]}
            if "width" in col_def:
                kwargs["width"] = col_def["width"]
                kwargs["no_wrap"] = True
            if "min_width" in col_def:
                kwargs["min_width"] = col_def["min_width"]
            if "overflow" in col_def:
                kwargs["overflow"] = col_def["overflow"]
            table.add_column(col_def["name"], **kwargs)

        # Add rows with only active columns
        for row in all_rows:
            row_data = [row[col] if row[col] else "-" for col in active_columns]
            table.add_row(*row_data)

        cls.console.print(table)

    @classmethod
    def display_participant_ticks(
        cls,
        ticks: list["ParticipantTick"],
        self_label: str = "Self",
        other_label: str = "Other",
    ):
        """
        Display a list of ParticipantTick objects in a table format.

        Args:
            ticks: List of ParticipantTick objects from a streaming participant.
            self_label: Label for the self column (e.g., "Agent" or "User").
            other_label: Label for the other column (e.g., "User" or "Agent").
        """
        c = cls.colors
        table = Table(
            title=f"Participant Ticks ({self_label} perspective)",
            show_header=True,
            header_style=c.table_header,
            show_lines=True,
        )
        table.add_column("Tick", style=c.table_details_column, no_wrap=True, width=4)
        table.add_column("Timestamp", style=c.label, no_wrap=True, width=12)
        table.add_column(self_label, style=c.assistant_content)
        table.add_column(f"{self_label} Tools", style=c.assistant_tool)
        table.add_column(other_label, style=c.user_content)
        table.add_column(f"{other_label} Tools", style=c.user_tool)

        for tick in ticks:
            # Self chunk content
            self_content = ""
            self_tools = ""
            if tick.self_chunk:
                if hasattr(tick.self_chunk, "content") and tick.self_chunk.content:
                    content = cls.escape_markup(tick.self_chunk.content)
                    self_content = content
                if (
                    hasattr(tick.self_chunk, "tool_calls")
                    and tick.self_chunk.tool_calls
                ):
                    self_tools = "\n".join(
                        f"{tc.name}({json.dumps(tc.arguments)})"
                        for tc in tick.self_chunk.tool_calls
                    )
                # Handle MultiToolMessage (list of tool results)
                if hasattr(tick.self_chunk, "tool_messages"):
                    self_tools = "\n".join(
                        (
                            f"Result: {tm.content}"
                            if tm.content
                            else f"Result: {tm.content}"
                        )
                        for tm in tick.self_chunk.tool_messages
                    )

            # Other chunk content
            other_content = ""
            other_tools = ""
            if tick.other_chunk:
                if hasattr(tick.other_chunk, "content") and tick.other_chunk.content:
                    content = cls.escape_markup(tick.other_chunk.content)
                    other_content = content
                if (
                    hasattr(tick.other_chunk, "tool_calls")
                    and tick.other_chunk.tool_calls
                ):
                    other_tools = "\n".join(
                        f"{tc.name}({json.dumps(tc.arguments)})"
                        for tc in tick.other_chunk.tool_calls
                    )
                # Handle MultiToolMessage (list of tool results)
                if hasattr(tick.other_chunk, "tool_messages"):
                    other_tools = "\n".join(
                        (
                            f"Result: {tm.content}"
                            if tm.content
                            else f"Result: {tm.content}"
                        )
                        for tm in tick.other_chunk.tool_messages
                    )

            # Extract timestamp (show only time portion if it's a full ISO timestamp)
            timestamp = tick.timestamp
            if "T" in timestamp:
                timestamp = timestamp.split("T")[1][:12]

            table.add_row(
                str(tick.tick_id),
                timestamp,
                self_content or "-",
                self_tools or "-",
                other_content or "-",
                other_tools or "-",
            )

        cls.console.print(table)

    @classmethod
    def display_streaming_state(
        cls,
        state: "StreamingState",
        self_label: str = "Self",
        other_label: str = "Other",
        show_buffers: bool = True,
    ):
        """
        Display a StreamingState with its tick history and current buffers.

        Args:
            state: The StreamingState object to display.
            self_label: Label for the self participant (e.g., "Agent" or "User").
            other_label: Label for the other participant (e.g., "User" or "Agent").
            show_buffers: Whether to show the input/output buffer contents.
        """
        c = cls.colors

        # Create summary panel
        summary = Text()
        summary.append("Streaming State Summary\n\n", style=c.section_header)

        summary.append("Tick History: ", style=c.label)
        summary.append(f"{len(state.ticks)} ticks\n")

        summary.append("Time Since Last Talk: ", style=c.label)
        summary.append(f"{state.time_since_last_talk}\n")

        summary.append("Time Since Last Other Talk: ", style=c.label)
        summary.append(f"{state.time_since_last_other_talk}\n")

        summary.append("\nBuffers:\n", style=c.section_header)
        summary.append("  Input Turn-Taking Buffer: ", style=c.label)
        summary.append(f"{len(state.input_turn_taking_buffer)} chunks\n")
        summary.append("  Output Streaming Queue: ", style=c.label)
        summary.append(f"{len(state.output_streaming_queue)} chunks\n")

        summary.append("  Is Talking: ", style=c.label)
        summary.append(f"{'Yes' if state.is_talking else 'No'}\n")

        cls.console.print(
            Panel(summary, title="Streaming State", border_style=c.panel_border)
        )

        # Display tick history
        if state.ticks:
            cls.display_participant_ticks(state.ticks, self_label, other_label)

        # Optionally show buffer contents
        if show_buffers:
            if state.input_turn_taking_buffer:
                cls._display_chunk_buffer(
                    state.input_turn_taking_buffer,
                    f"Input Turn-Taking Buffer ({other_label})",
                    c.user_content,
                )

            if state.output_streaming_queue:
                cls._display_chunk_buffer(
                    state.output_streaming_queue,
                    f"Output Streaming Queue ({self_label})",
                    c.assistant_content,
                )

    @classmethod
    def _display_chunk_buffer(
        cls, chunks: list, title: str, content_style: str, max_chunks: int = 10
    ):
        """
        Display a buffer of message chunks.

        Args:
            chunks: List of message chunks to display.
            title: Title for the buffer display.
            content_style: Rich style to use for content.
            max_chunks: Maximum number of chunks to display.
        """
        c = cls.colors
        table = Table(
            title=title,
            show_header=True,
            header_style=c.table_header,
            show_lines=True,
        )
        table.add_column("#", style=c.table_details_column, no_wrap=True, width=4)
        table.add_column("Content", style=content_style)
        table.add_column("Info", style=c.label)

        display_chunks = chunks[-max_chunks:] if len(chunks) > max_chunks else chunks
        if len(chunks) > max_chunks:
            table.add_row("...", f"({len(chunks) - max_chunks} earlier chunks)", "")

        start_idx = max(0, len(chunks) - max_chunks)
        for i, chunk in enumerate(display_chunks):
            idx = start_idx + i
            content = ""
            info_parts = []

            if hasattr(chunk, "content") and chunk.content:
                content = cls.escape_markup(chunk.content)
                if len(content) > 80:
                    content = content

            if hasattr(chunk, "role"):
                info_parts.append(f"role: {chunk.role}")
            if hasattr(chunk, "is_final_chunk"):
                info_parts.append(f"final: {chunk.is_final_chunk}")
            if hasattr(chunk, "contains_speech"):
                info_parts.append(f"speech: {chunk.contains_speech}")

            table.add_row(str(idx), content or "-", ", ".join(info_parts))

        cls.console.print(table)


class MarkdownDisplay:
    @classmethod
    def display_actions(cls, actions: List[Action]) -> str:
        """Display actions in markdown format."""
        return f"```json\n{json.dumps([action.model_dump() for action in actions], indent=2)}\n```"

    @classmethod
    def display_messages(cls, messages: list[Message]) -> str:
        """Display messages in markdown format."""
        return "\n\n".join(cls.display_message(msg) for msg in messages)

    @classmethod
    def display_simulation(cls, sim: SimulationRun) -> str:
        """Display simulation in markdown format."""
        # Otherwise handle SimulationRun object
        output = []

        # Add basic simulation info
        output.append(f"**Task ID**: {sim.task_id}")
        output.append(f"**Trial**: {sim.trial}")
        output.append(f"**Duration**: {sim.duration:.2f}s")
        output.append(f"**Termination**: {sim.termination_reason}")
        if sim.agent_cost is not None:
            output.append(f"**Agent Cost**: ${sim.agent_cost:.4f}")
        if sim.user_cost is not None:
            output.append(f"**User Cost**: ${sim.user_cost:.4f}")

        # Add reward info if present
        if sim.reward_info:
            label, value = result_metric(sim)
            output.append(
                f"**{label}**: {value:.4f}"
                if value is not None
                else f"**{label}**: unavailable"
            )

            # Add DB check info if present
            if sim.reward_info.db_check:
                output.append("\n**DB Check**")
                output.append(
                    f"- Status: {'✅' if sim.reward_info.db_check.db_match else '❌'} {sim.reward_info.db_check.db_reward}"
                )

            # Add env assertions if present
            if sim.reward_info.env_assertions:
                output.append("\n**Env Assertions**")
                for i, assertion in enumerate(sim.reward_info.env_assertions):
                    output.append(
                        f"- {i}: {assertion.env_assertion.env_type} {assertion.env_assertion.func_name} {'✅' if assertion.met else '❌'} {assertion.reward}"
                    )

            # Add action checks if present
            if sim.reward_info.action_checks:
                output.append("\n**Action Checks**")
                for i, check in enumerate(sim.reward_info.action_checks):
                    tool_type_str = (
                        f" [{check.tool_type.value}]" if check.tool_type else ""
                    )
                    requestor_str = (
                        "user" if check.action.requestor == "user" else "agent"
                    )
                    output.append(
                        f"- {i}: {requestor_str} {check.action.name}{tool_type_str} {'✅' if check.action_match else '❌'} {check.action_reward}"
                    )
                # Add partial reward breakdown
                partial = sim.reward_info.partial_action_reward
                if partial:
                    total = partial["total"]
                    output.append(
                        f"\n**Partial Action Reward**: {total['correct']}/{total['count']} ({total['proportion']:.1%})"
                    )
                    if partial.get("read"):
                        read = partial["read"]
                        output.append(
                            f"  - Read: {read['correct']}/{read['count']} ({read['proportion']:.1%})"
                        )
                    if partial.get("write"):
                        write = partial["write"]
                        output.append(
                            f"  - Write: {write['correct']}/{write['count']} ({write['proportion']:.1%})"
                        )

            # Add communication checks if present
            if sim.reward_info.communicate_checks:
                output.append("\n**Communicate Checks**")
                for i, check in enumerate(sim.reward_info.communicate_checks):
                    output.append(
                        f"- {i}: {check.info} {'✅' if check.met else '❌'} {check.justification}"
                    )

            # Add NL assertions if present
            if sim.reward_info.nl_assertions:
                output.append("\n**NL Assertions**")
                for i, assertion in enumerate(sim.reward_info.nl_assertions):
                    output.append(
                        f"- {i}: {assertion.nl_assertion} {'✅' if assertion.met else '❌'} {assertion.justification}"
                    )

            # Add additional info if present
            if sim.reward_info.info:
                output.append("\n**Additional Info**")
                for key, value in sim.reward_info.info.items():
                    output.append(f"- {key}: {value}")

        # Add messages using the display_message method
        messages = sim.get_messages()
        if messages:
            output.append("\n**Messages**:")
            output.extend(cls.display_message(msg) for msg in messages)

        if sim.effect_timeline and sim.effect_timeline.events:
            effect_config_md = cls.display_effect_configs(sim)
            if effect_config_md:
                output.append(effect_config_md)
            output.append(cls.display_effect_timeline(sim.effect_timeline))

        return "\n\n".join(output)

    @classmethod
    def display_effect_configs(cls, sim: SimulationRun) -> Optional[str]:
        """Return a markdown table of the effect configuration for this simulation."""
        env = sim.speech_environment
        if env is None:
            return None

        has_configs = (
            env.source_effects_config is not None
            or env.speech_effects_config is not None
            or env.channel_effects_config is not None
        )
        if not has_configs:
            return None

        rows: list[tuple[str, str]] = []

        if env.source_effects_config is not None:
            src = env.source_effects_config
            rows.append(("background_noise", str(src.enable_background_noise)))
            rows.append(("noise_snr_db", f"{src.noise_snr_db}"))
            rows.append(("noise_snr_drift_db", f"{src.noise_snr_drift_db}"))
            rows.append(("noise_variation_speed", f"{src.noise_variation_speed}"))
            rows.append(("burst_noise", str(src.enable_burst_noise)))
            rows.append(
                (
                    "burst_noise_events_per_min",
                    f"{src.burst_noise_events_per_minute}",
                )
            )
            rows.append(("burst_snr_range_db", str(src.burst_snr_range_db)))

        if env.speech_effects_config is not None:
            spc = env.speech_effects_config
            rows.append(("dynamic_muffling", str(spc.enable_dynamic_muffling)))
            rows.append(("muffle_probability", f"{spc.muffle_probability}"))
            rows.append(("muffle_cutoff_freq", f"{spc.muffle_cutoff_freq}"))
            rows.append(("out_of_turn_speech", str(spc.enable_non_directed_phrases)))
            rows.append(
                (
                    "speech_insert_events_per_min",
                    f"{spc.speech_insert_events_per_minute}",
                )
            )

        if env.channel_effects_config is not None:
            ch = env.channel_effects_config
            rows.append(("frame_drops", str(ch.enable_frame_drops)))
            rows.append(("frame_drop_rate", f"{ch.frame_drop_rate}"))
            rows.append(
                (
                    "frame_drop_burst_duration_ms",
                    f"{ch.frame_drop_burst_duration_ms}",
                )
            )

        rows.append(("snr_speech_reference_rms", f"{env.snr_speech_reference_rms}"))
        rows.append(("telephony", str(env.telephony_enabled)))

        lines = [
            f"### Effect Configuration (complexity={env.complexity})",
            "",
            "| Parameter | Value |",
            "| --- | --- |",
        ]
        for param, value in rows:
            lines.append(f"| {param} | {value} |")

        return "\n".join(lines)

    @classmethod
    def display_result(
        cls,
        task: Task,
        sim: SimulationRun,
        reward: Optional[float] = None,
        show_task_id: bool = False,
    ) -> str:
        """Display a single result with all its components in markdown format."""
        output = [
            f"## Task {task.id}" if show_task_id else "## Task",
            "\n### User Instruction",
            task.user_scenario.instructions,
            "\n### Ground Truth Actions",
            cls.display_actions(task.evaluation_criteria.actions),
        ]

        if task.evaluation_criteria.communicate_info:
            output.extend(
                [
                    "\n### Communicate Info",
                    "```\n" + str(task.evaluation_criteria.communicate_info) + "\n```",
                ]
            )

        # The simulation emits its single result; the optional total is legacy input.
        output.extend(["\n### Simulation", cls.display_simulation(sim)])

        return "\n".join(output)

    @classmethod
    def display_message(cls, msg: Message) -> str:
        """Display a single message in markdown format."""
        # Common message components
        parts = []

        # Add turn index if present
        turn_prefix = f"[TURN {msg.turn_idx}] " if msg.turn_idx is not None else ""

        # Format based on message type
        if isinstance(msg, AssistantMessage) or isinstance(msg, UserMessage):
            parts.append(f"{turn_prefix}**{msg.role}**:")
            if msg.content:
                parts.append(msg.content)
            if msg.tool_calls:
                tool_calls = []
                for tool in msg.tool_calls:
                    tool_calls.append(
                        f"**Tool Call**: {tool.name}\n```json\n{json.dumps(tool.arguments, indent=2)}\n```"
                    )
                parts.extend(tool_calls)

        elif isinstance(msg, ToolMessage):
            status = " (Error)" if msg.error else ""
            parts.append(f"{turn_prefix}**tool{status}**:")
            parts.append(f"Reponse to: {msg.requestor}")
            if msg.content:
                parts.append(f"```\n{msg.content}\n```")

        elif isinstance(msg, SystemMessage):
            parts.append(f"{turn_prefix}**system**:")
            if msg.content:
                parts.append(msg.content)

        return "\n".join(parts)

    @classmethod
    def display_effect_timeline(
        cls,
        timeline: Optional[EffectTimeline],
    ) -> str:
        """Return a markdown table summarising the effect timeline."""
        if not timeline or not timeline.events:
            return ""

        sorted_events = sorted(timeline.events, key=lambda e: e.start_ms)

        def _fmt_s(ms: int) -> str:
            return f"{ms / 1000:.1f}s"

        def _fmt_params(params: Optional[dict]) -> str:
            if not params:
                return "-"
            parts = []
            for k, v in params.items():
                if isinstance(v, float):
                    parts.append(f"{k}={v:.1f}")
                else:
                    parts.append(f"{k}={v}")
            return ", ".join(parts)

        lines = [
            f"### Effect Timeline ({len(sorted_events)} events)",
            "",
            "| Start | End | Effect | Participant | Duration | Details |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for event in sorted_events:
            start = _fmt_s(event.start_ms)
            end = _fmt_s(event.end_ms) if event.end_ms is not None else "..."
            duration = (
                _fmt_s(event.duration_ms) if event.duration_ms is not None else "-"
            )
            details = _fmt_params(event.params)
            lines.append(
                f"| {start} | {end} | {event.effect_type} | {event.participant} | {duration} | {details} |"
            )

        return "\n".join(lines)

    @classmethod
    def display_ticks_consolidated(
        cls,
        ticks: list["Tick"],
        user_visible_only: bool = False,
        effect_timeline: Optional[EffectTimeline] = None,
        tick_duration_in_ms: int | None = None,
    ) -> str:
        """
        Display ticks in a consolidated markdown table format.

        Consecutive speech chunks from the same speaker are grouped together.
        Tool calls and results are shown separately and break consolidation.
        This provides a cleaner, tabular view of the conversation similar to ConsoleDisplay.

        Args:
            ticks: List of Tick objects from full-duplex simulation.
            user_visible_only: If True, hide agent tool calls/results (internal to agent).
                This shows only what the user can see/hear.

        Returns:
            Markdown-formatted table string of the consolidated conversation.
        """
        if not ticks:
            return ""

        # Helper to escape markdown table special characters
        def escape_table(text: str) -> str:
            if not text:
                return ""
            # Replace pipe and newlines for table cells
            return text.replace("|", "\\|").replace("\n", " ")

        # Helper to extract info from a tick
        def extract_tick_info(tick: "Tick") -> dict:
            info = {
                "agent_content": "",
                "agent_calls": [],
                "agent_results": [],
                "agent_turn_action": "",
                "user_content": "",
                "user_calls": [],
                "user_results": [],
                "user_turn_action": "",
            }

            if tick.agent_chunk and tick.agent_chunk.content:
                info["agent_content"] = tick.agent_chunk.content
            # Only include agent tool calls/results if not in user_visible_only mode
            if not user_visible_only:
                if tick.agent_tool_calls:
                    info["agent_calls"] = [
                        {"name": tc.name, "arguments": tc.arguments}
                        for tc in tick.agent_tool_calls
                    ]
                if tick.agent_tool_results:
                    info["agent_results"] = [r.content for r in tick.agent_tool_results]
            if (
                tick.agent_chunk
                and hasattr(tick.agent_chunk, "turn_taking_action")
                and tick.agent_chunk.turn_taking_action
            ):
                action = tick.agent_chunk.turn_taking_action.action
                info_text = tick.agent_chunk.turn_taking_action.info
                info["agent_turn_action"] = (
                    f"{action}: {info_text}" if info_text else action
                )

            if tick.user_chunk and tick.user_chunk.content:
                info["user_content"] = tick.user_chunk.content
            if tick.user_tool_calls:
                info["user_calls"] = [
                    {"name": tc.name, "arguments": tc.arguments}
                    for tc in tick.user_tool_calls
                ]
            if tick.user_tool_results:
                info["user_results"] = [r.content for r in tick.user_tool_results]
            if (
                tick.user_chunk
                and hasattr(tick.user_chunk, "turn_taking_action")
                and tick.user_chunk.turn_taking_action
            ):
                action = tick.user_chunk.turn_taking_action.action
                info_text = tick.user_chunk.turn_taking_action.info
                info["user_turn_action"] = (
                    f"{action}: {info_text}" if info_text else action
                )

            return info

        # Helper to check if a tick has tool activity (breaks consolidation)
        def has_tool_activity(info: dict) -> bool:
            return bool(
                info["agent_calls"]
                or info["agent_results"]
                or info["user_calls"]
                or info["user_results"]
            )

        # Group ticks using shared helper from ConsoleDisplay
        infos = [extract_tick_info(tick) for tick in ticks]
        ranges = display_tick_ranges(
            [
                DisplayTick(
                    agent_content=info["agent_content"],
                    user_content=info["user_content"],
                    agent_turn_action=info["agent_turn_action"],
                    user_turn_action=info["user_turn_action"],
                    has_tool_activity=has_tool_activity(info),
                )
                for info in infos
            ]
        )
        groups = [
            (ticks[r.start].tick_id, ticks[r.end - 1].tick_id, infos[r.start : r.end])
            for r in ranges
        ]

        show_effects = (
            effect_timeline is not None
            and tick_duration_in_ms is not None
            and len(effect_timeline.events) > 0
        )

        # Determine which columns we need
        has_agent_calls = any(
            any(inf["agent_calls"] for inf in grp[2]) for grp in groups
        )
        has_agent_results = any(
            any(inf["agent_results"] for inf in grp[2]) for grp in groups
        )
        has_user_calls = any(any(inf["user_calls"] for inf in grp[2]) for grp in groups)
        has_user_results = any(
            any(inf["user_results"] for inf in grp[2]) for grp in groups
        )

        # Build table header
        headers = ["Ticks", "Agent"]
        if has_agent_calls and not user_visible_only:
            headers.append("Agent Calls")
        if has_agent_results and not user_visible_only:
            headers.append("Tool Results")
        headers.append("User")
        if has_user_calls:
            headers.append("User Calls")
        if has_user_results:
            headers.append("User Results")
        if show_effects:
            headers.append("Effects")

        # Build table rows
        rows = []
        for start_tick, end_tick, infos in groups:
            # Create tick label
            if start_tick == end_tick:
                tick_label = str(start_tick)
            else:
                tick_label = f"{start_tick}-{end_tick}"

            # Consolidate text content
            agent_parts = [
                inf["agent_content"] for inf in infos if inf["agent_content"]
            ]
            user_parts = [inf["user_content"] for inf in infos if inf["user_content"]]

            agent_content = escape_table("".join(agent_parts).strip())
            user_content = escape_table("".join(user_parts).strip())

            # Tool calls/results (only one tick has them per group)
            agent_calls = next(
                (inf["agent_calls"] for inf in infos if inf["agent_calls"]), []
            )
            agent_results = next(
                (inf["agent_results"] for inf in infos if inf["agent_results"]), []
            )
            user_calls = next(
                (inf["user_calls"] for inf in infos if inf["user_calls"]), []
            )
            user_results = next(
                (inf["user_results"] for inf in infos if inf["user_results"]), []
            )

            # Format tool calls/results for table
            agent_calls_str = ""
            if agent_calls:
                agent_calls_str = "; ".join(
                    f"{tc['name']}({json.dumps(tc['arguments'])})" for tc in agent_calls
                )
                agent_calls_str = escape_table(agent_calls_str)

            agent_results_str = ""
            if agent_results:
                agent_results_str = escape_table("; ".join(agent_results))

            user_calls_str = ""
            if user_calls:
                user_calls_str = "; ".join(
                    f"{tc['name']}({json.dumps(tc['arguments'])})" for tc in user_calls
                )
                user_calls_str = escape_table(user_calls_str)

            user_results_str = ""
            if user_results:
                # This Markdown is also judge input: hiding fields can turn
                # grounded customer claims into apparent hallucinations.
                user_results_str = escape_table("; ".join(user_results))

            # Build row
            row = [tick_label, agent_content]
            if has_agent_calls and not user_visible_only:
                row.append(agent_calls_str)
            if has_agent_results and not user_visible_only:
                row.append(agent_results_str)
            row.append(user_content)
            if has_user_calls:
                row.append(user_calls_str)
            if has_user_results:
                row.append(user_results_str)
            if show_effects:
                row_start_ms = start_tick * tick_duration_in_ms
                row_end_ms = (end_tick + 1) * tick_duration_in_ms
                overlapping = ConsoleDisplay._get_overlapping_effects(
                    effect_timeline, row_start_ms, row_end_ms
                )
                effects_parts = []
                for e in overlapping:
                    s = f"{e.start_ms / 1000:.1f}s"
                    en = f"{e.end_ms / 1000:.1f}s" if e.end_ms is not None else "..."
                    effects_parts.append(f"{e.effect_type} ({s}-{en})")
                row.append(
                    escape_table("; ".join(effects_parts)) if effects_parts else ""
                )

            rows.append(row)

        # Build markdown table
        table_lines = []

        # Header
        table_lines.append("| " + " | ".join(headers) + " |")

        # Separator
        table_lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

        # Rows with separators between them for better readability
        separator_row = "| " + " | ".join(["───"] * len(headers)) + " |"
        for i, row in enumerate(rows):
            table_lines.append("| " + " | ".join(row) + " |")
            if i < len(rows) - 1:  # Add separator between rows, not after last
                table_lines.append(separator_row)

        return "\n".join(table_lines)
