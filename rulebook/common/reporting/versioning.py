from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class PipelineVersion:
    family: str = "eea-rulebook"
    tag: str = "post-selection-contract"
    lineage: str = "effect-bundle-runtime"
    stability: str = "experimental"
    summary: str = (
        "Current EEA runtime uses canonical repair programs, bundle-aware dry-run, "
        "and replay-gated pattern evolution."
    )

    @property
    def label(self) -> str:
        return f"{self.family}/{self.tag}"


def get_pipeline_version() -> PipelineVersion:
    return PipelineVersion()


def detect_git_context(cwd: str | Path | None = None) -> Dict[str, Any]:
    workdir = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        return {
            "git_available": False,
            "git_root": None,
            "git_branch": None,
            "git_commit": None,
            "git_dirty": None,
        }

    branch = None
    commit = None
    dirty = None
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--short"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except Exception:
        pass

    return {
        "git_available": True,
        "git_root": root,
        "git_branch": branch,
        "git_commit": commit,
        "git_dirty": dirty,
    }


def build_run_metadata(
    *,
    stage: str,
    output_dir: str,
    inputs: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    version = get_pipeline_version()
    return {
        "pipeline_version": asdict(version),
        "pipeline_label": version.label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "output_dir": output_dir,
        "git": detect_git_context(output_dir),
        "inputs": inputs or {},
        "config": config or {},
        "extra": extra or {},
    }


def write_run_metadata(
    output_dir: str | Path,
    *,
    stage: str,
    inputs: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
    filename: str = "00_run_metadata.json",
) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    metadata = build_run_metadata(
        stage=stage,
        output_dir=str(output_path),
        inputs=inputs,
        config=config,
        extra=extra,
    )
    target = output_path / filename
    with target.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    return target
