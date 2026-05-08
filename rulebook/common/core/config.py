from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


RULEBOOK_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = RULEBOOK_ROOT / "config.toml"


@dataclass
class LLMSettings:
    model: str
    base_url: str
    api_key: str
    max_tokens: int = 4096
    temperature: float = 0.7
    api_type: str = "openai"
    api_version: Optional[str] = None


@dataclass
class RulebookSettings:
    timeout: int = 30
    probe_timeout: int = 10
    K_per_branch: int = 6
    adaptive_budget: bool = False
    n_plans: int = 3
    max_actions_per_plan: int = 3
    max_candidates_per_branch_step: int = 12
    ranking_weights: Optional[Dict[str, float]] = None
    llm_enabled: bool = False
    llm_timeout: int = 30


@dataclass
class RulebookConfig:
    llm: LLMSettings
    rulebook: RulebookSettings


def _load_config_dict(config_path: Path) -> Dict:
    if not config_path.exists():
        return {}
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def load_config(config_path: Optional[str] = None) -> RulebookConfig:
    config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    conf = _load_config_dict(config_path)

    llm_block = conf.get("llm", {})
    rulebook_block = conf.get("rulebook", {})

    llm = LLMSettings(
        model=llm_block.get("model") or os.getenv("RULEBOOK_LLM_MODEL", ""),
        base_url=llm_block.get("base_url") or os.getenv("RULEBOOK_LLM_BASE_URL", ""),
        api_key=llm_block.get("api_key") or os.getenv("RULEBOOK_LLM_API_KEY", ""),
        max_tokens=int(llm_block.get("max_tokens") or os.getenv("RULEBOOK_LLM_MAX_TOKENS", "4096")),
        temperature=float(llm_block.get("temperature") or os.getenv("RULEBOOK_LLM_TEMPERATURE", "0.7")),
        api_type=llm_block.get("api_type") or os.getenv("RULEBOOK_LLM_API_TYPE", "openai"),
        api_version=llm_block.get("api_version") or os.getenv("RULEBOOK_LLM_API_VERSION"),
    )

    ranking_weights = rulebook_block.get("ranking_weights") or {
        "row_count": 0.4,
        "scalar": 0.3,
        "jaccard": 0.3,
    }

    rulebook = RulebookSettings(
        timeout=int(rulebook_block.get("timeout", 30)),
        probe_timeout=int(rulebook_block.get("probe_timeout", 10)),
        K_per_branch=int(rulebook_block.get("K_per_branch", 6)),
        adaptive_budget=bool(rulebook_block.get("adaptive_budget", False)),
        n_plans=int(rulebook_block.get("n_plans", 3)),
        max_actions_per_plan=int(rulebook_block.get("max_actions_per_plan", 3)),
        max_candidates_per_branch_step=int(rulebook_block.get("max_candidates_per_branch_step", 12)),
        ranking_weights=ranking_weights,
        llm_enabled=bool(rulebook_block.get("llm_enabled", False)),
        llm_timeout=int(rulebook_block.get("llm_timeout", 30)),
    )

    return RulebookConfig(llm=llm, rulebook=rulebook)
