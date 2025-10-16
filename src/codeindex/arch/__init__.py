from __future__ import annotations
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path

from ..logger import logger
from .builder import ArchBuilder
from .llm_architect import LLMArchitect, LLMArchitectConfig


@dataclass
class ArchConfig:
    index_dir: Path
    out_dir: Path
    llm_model: str = "gpt-5-mini"
    max_tokens: int = 4000
    temperature: float | None = None
    reasoning_effort: str = "medium"
    verbosity: str | None = "medium"
    stub: bool = False
    diagram_mode: str = "llm"
    diagram_coverage: float = 0.95
    diagram_max_redrafts: int = 2
    diagram_feature_flag: str = "arch.llm.diagrams.enabled"


def _is_flag_enabled(flag_name: str) -> bool:
    env_key = "CODEINDEX_FLAG_" + flag_name.replace(".", "_").upper()
    raw = os.getenv(env_key)
    if raw is None:
        return True
    return raw.lower() not in {"0", "false", "off", "no"}


def generate_architecture(config: ArchConfig) -> None:
    """
    Generate architecture documentation exclusively through an LLM. This replaces
    prior heuristic-based doc generation.
    """
    out_dir = config.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Generating LLM architecture pack from %s -> %s",
        config.index_dir,
        config.out_dir,
    )

    builder = ArchBuilder.from_index(config.index_dir)

    feature_flag_enabled = _is_flag_enabled(config.diagram_feature_flag)
    diagram_mode = config.diagram_mode
    if diagram_mode != "deterministic" and not feature_flag_enabled:
        logger.info(
            "Feature flag %s disabled; using deterministic diagram mode",
            config.diagram_feature_flag,
        )
        diagram_mode = "deterministic"

    architect = LLMArchitect(
        builder,
        LLMArchitectConfig(
            out_dir=config.out_dir,
            model=config.llm_model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            reasoning_effort=config.reasoning_effort,
            verbosity=config.verbosity,
            stub=config.stub,
            diagram_mode=diagram_mode,
            coverage_threshold=config.diagram_coverage,
            max_redrafts=config.diagram_max_redrafts,
        ),
    )
    asyncio.run(architect.generate())
    meta = {
        "node_count": len(builder.nodes_by_id),
        "edge_count": len(builder.edges),
        "package_count": len(builder.packages),
        "component_count": len(builder.components),
    }
    (out_dir / "meta.json").write_text(ArchBuilder.json_dumps(meta), encoding="utf-8")
    logger.info(
        "LLM architecture bundle ready: components=%d",
        meta["component_count"],
    )


__all__ = ["ArchConfig", "generate_architecture"]
