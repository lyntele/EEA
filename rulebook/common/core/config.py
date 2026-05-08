from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


RULEBOOK_ROOT = Path(__file__).resolve().parents[2]
LEGACY_CONFIG_PATH = RULEBOOK_ROOT / "common" / "config.toml"
DEFAULT_CONFIG_PATH = LEGACY_CONFIG_PATH
DEEPEYE_API_PROFILE_PATH = (
    RULEBOOK_ROOT.parent.parent
    / "deepeye"
    / "DeepEye-SQL"
    / "rulebook_experiments"
    / "configs"
    / "api_profile_openrouter.toml"
)


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


def _resolve_config_path(config_path: Optional[str] = None) -> Path:
    if config_path:
        return Path(config_path)
    for env_name in (
        "RULEBOOK_CONFIG_PATH",
        "RULEBOOK_API_PROFILE_PATH",
        "RULEBOOK_API_PROFILE",
        "API_PROFILE",
    ):
        env_path = os.getenv(env_name, "").strip()
        if env_path:
            return Path(env_path)
    for candidate in (LEGACY_CONFIG_PATH, DEEPEYE_API_PROFILE_PATH):
        if candidate.exists():
            return candidate
    return LEGACY_CONFIG_PATH


def _looks_like_inline_api_key(value: str) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return False
    if candidate.startswith(("sk-", "sk-or-", "sk-ant-", "sess-")):
        return True
    return len(candidate) >= 32 and "_" not in candidate and candidate.upper() != candidate


def _resolve_api_key(block: Dict[str, Any]) -> str:
    if block.get("api_key"):
        return str(block["api_key"])
    api_key_env = str(block.get("api_key_env") or "").strip()
    if not api_key_env:
        return ""
    if _looks_like_inline_api_key(api_key_env):
        return api_key_env
    return os.getenv(api_key_env, "")


def _profile_llm_block(conf: Dict[str, Any]) -> Dict[str, Any]:
    """Map DeepEye api_profile [rulebook.openrouter] into EEA [llm]."""
    rulebook_block = conf.get("rulebook")
    if not isinstance(rulebook_block, dict):
        return {}
    openrouter = rulebook_block.get("openrouter")
    if not isinstance(openrouter, dict):
        return {}
    model_map = rulebook_block.get("model_map")
    if not isinstance(model_map, dict):
        model_map = {}
    return {
        "model": openrouter.get("model") or model_map.get("qwen3") or model_map.get("default"),
        "base_url": openrouter.get("base_url"),
        "api_key": _resolve_api_key(openrouter),
        "max_tokens": openrouter.get("max_tokens"),
        "temperature": openrouter.get("temperature"),
        "api_type": openrouter.get("api_type"),
        "api_version": openrouter.get("api_version"),
    }


def load_config(config_path: Optional[str] = None) -> RulebookConfig:
    config_path = _resolve_config_path(config_path)
    conf = _load_config_dict(config_path)

    llm_block = conf.get("llm", {}) or _profile_llm_block(conf)
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
