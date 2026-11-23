"""Call provenance beside benchmark rewards; no independent quality score."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from tau2.data_model.benchmark import BenchmarkFrame
from tau2.data_model.judges import (
    ContractModel,
    JudgeCatalog,
)
from tau2.data_model.simulation import Results, SimulationRun, TerminationReason

QUALITY_FILE_NAME = "benchmark-quality.json"
QUALITY_BENCHMARKS = frozenset({"voice-v2"})


class CallQuality(ContractModel):
    """Operational provenance for a single call."""

    simulation_id: str
    task_id: str
    evaluation_log: dict = Field(default_factory=dict)


class CellQuality(ContractModel):
    """Call evidence in the log, without independent performance metrics."""

    language: str
    domain: str
    evaluation_log: dict = Field(default_factory=dict)


class BenchmarkQuality(ContractModel):
    """Current quality output; historical v1 sidecars are not rewritten."""

    schema_version: Literal["benchmark-quality-v4"] = "benchmark-quality-v4"
    benchmark_version: str
    frame_revision: str
    cells: list[CellQuality]

    @model_validator(mode="before")
    @classmethod
    def read_historical_flags(cls, value):
        """Keep old sidecars readable as provenance without reviving a metric."""
        if (
            isinstance(value, dict)
            and value.get("schema_version") == "benchmark-quality-v3"
        ):
            value = dict(value, schema_version="benchmark-quality-v4")
            cells = []
            for original in value.get("cells", []):
                cell = dict(original)
                log = dict(cell.get("evaluation_log", {}))
                log["historical_schema_version"] = "benchmark-quality-v3"
                log["historical_metrics"] = cell.pop("metrics", {})
                log["calls"] = [
                    dict(
                        simulation_id=c["simulation_id"],
                        task_id=c["task_id"],
                        evaluation_log={"historical_flags": c.get("flags", {})},
                    )
                    for c in log.get("calls", [])
                ]
                cell["evaluation_log"] = log
                cells.append(cell)
            value["cells"] = cells
        return value


def score_call(simulation: SimulationRun) -> CallQuality:
    """Record call identity; no audio extraction or model work is needed."""
    return CallQuality(
        simulation_id=simulation.id,
        task_id=simulation.task_id,
        evaluation_log={"termination_reason": simulation.termination_reason.value},
    )


def cell_quality(language: str, domain: str, calls: list[CallQuality]) -> CellQuality:
    """Keep operational provenance without an empty performance block."""
    return CellQuality(
        language=language,
        domain=domain,
        evaluation_log={"calls": [call.model_dump() for call in calls]},
    )


def build_quality(frame: BenchmarkFrame, cells: list[CellQuality]) -> BenchmarkQuality:
    order = [(cell.language, cell.domain) for cell in frame.cells]
    if [(cell.language, cell.domain) for cell in cells] != order:
        raise ValueError("Quality cells must follow the frame's cells")
    return BenchmarkQuality(
        benchmark_version=frame.benchmark_version,
        frame_revision=frame.revision,
        cells=cells,
    )


def check_quality(quality: BenchmarkQuality, frame: BenchmarkFrame) -> None:
    """The sidecar cites the package's frame and covers each of its cells once."""
    if (
        quality.benchmark_version != frame.benchmark_version
        or quality.frame_revision != frame.revision
    ):
        raise ValueError("Quality and rewards cite different frames")
    if [(c.language, c.domain) for c in quality.cells] != [
        (c.language, c.domain) for c in frame.cells
    ]:
        raise ValueError("Quality must cover every frame cell once, in frame order")


def _scored(simulation: SimulationRun) -> bool:
    """Calls with a task reward, as ``read_task_trials`` scores them."""
    return simulation.reward_info is not None and simulation.termination_reason not in {
        TerminationReason.INFRASTRUCTURE_ERROR,
        TerminationReason.UNEXPECTED_ERROR,
    }


def collect_quality(
    root: Path, frame: BenchmarkFrame, catalog: JudgeCatalog
) -> BenchmarkQuality:
    """Collect call provenance only; catalog is retained for callers."""

    cells = []
    for cell in frame.cells:
        directory = root / f"{cell.language}-{cell.domain}"
        calls = [
            score_call(simulation)
            for simulation in Results.iter_simulations(directory / "results.json")
            if _scored(simulation)
        ]
        cells.append(cell_quality(cell.language, cell.domain, calls))
    return build_quality(frame, cells)
