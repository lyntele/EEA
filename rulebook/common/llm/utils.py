"""Shared LLM JSON-call helper for the current EEA stack."""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from method.EEA.rulebook.common.core.config import load_config
from method.EEA.rulebook.common.llm.client import LLMClient


_CONFIG: Optional[Any] = None
_CLIENT: Optional[LLMClient] = None


def _strip_proxy_env() -> None:
    for key in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        os.environ.pop(key, None)


@contextlib.contextmanager
def _llm_hard_timeout(seconds: int):
    if (
        seconds <= 0
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
    ):
        yield
        return

    def _raise_timeout(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"LLM hard timeout after {seconds}s")

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, old_handler)


def _hard_timeout_seconds() -> int:
    raw = os.getenv("RULEBOOK_LLM_HARD_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    base_timeout = int(getattr(getattr(_CONFIG, "rulebook", None), "llm_timeout", 30) or 30)
    return max(base_timeout + 15, 45)


def _get_client() -> LLMClient:
    global _CONFIG, _CLIENT
    if _CLIENT is None:
        _strip_proxy_env()
        _CONFIG = load_config()
        _CLIENT = LLMClient(_CONFIG.llm, timeout=_CONFIG.rulebook.llm_timeout)
    return _CLIENT


def _extract_code_fence_payload(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else text


def _extract_balanced_json_snippet(text: str) -> str:
    start = -1
    opener = ""
    closer = ""
    for idx, ch in enumerate(text):
        if ch == "{":
            start = idx
            opener = "{"
            closer = "}"
            break
        if ch == "[":
            start = idx
            opener = "["
            closer = "]"
            break
    if start < 0:
        raise ValueError("No JSON opener found")

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == opener:
            depth += 1
            continue
        if ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    raise ValueError("No balanced JSON snippet found")


def _parse_json_block(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty LLM response")
    try:
        return json.loads(text)
    except Exception:
        pass

    fenced = _extract_code_fence_payload(text)
    if fenced != text:
        try:
            return json.loads(fenced)
        except Exception:
            text = fenced

    snippet = _extract_balanced_json_snippet(text)
    try:
        return json.loads(snippet)
    except Exception:
        pass

    normalized = re.sub(r",(\s*[}\]])", r"\1", snippet)
    try:
        return json.loads(normalized)
    except Exception:
        pass

    parsed = ast.literal_eval(normalized)
    if isinstance(parsed, (dict, list)):
        return parsed
    raise ValueError("No valid JSON block found in LLM response")


def _write_llm_trace(row: Dict[str, Any]) -> None:
    trace_path = os.getenv("EEA_LLM_TRACE_PATH", "").strip()
    if not trace_path:
        return
    try:
        path = Path(trace_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        return


def _prompt_section_stats(prompt: str) -> List[Dict[str, Any]]:
    """Best-effort prompt section summary for token-budget audits.

    Prompts are authored by several nodes and do not share a strict schema.
    This keeps tracing answer-agnostic and compact: it records section labels
    and character counts, never raw prompt text unless explicitly enabled.
    """
    lines = str(prompt or "").splitlines()
    sections: List[Dict[str, Any]] = []
    current_name = "preamble"
    current_chars = 0

    def is_heading(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if stripped.startswith("#"):
            return True
        if stripped.endswith(":") and len(stripped) <= 80:
            alpha = [ch for ch in stripped if ch.isalpha()]
            return bool(alpha) and sum(ch.isupper() for ch in alpha) >= max(2, len(alpha) // 2)
        return False

    for line in lines:
        if is_heading(line):
            if current_chars or current_name != "preamble":
                sections.append({"name": current_name[:120], "chars": current_chars})
            current_name = line.strip().lstrip("#").strip(" :") or "section"
            current_chars = len(line) + 1
        else:
            current_chars += len(line) + 1
    if current_chars or not sections:
        sections.append({"name": current_name[:120], "chars": current_chars})
    return sections[:80]


def call_llm(
    prompt: str,
    expect_json: bool = True,
    *,
    stage: str = "unknown",
    trace_context: Optional[Dict[str, Any]] = None,
) -> Union[Dict[str, Any], str]:
    client = _get_client()
    hard_timeout = _hard_timeout_seconds()
    prompt_sections = _prompt_section_stats(prompt)
    trace_base = {
        "timestamp_utc": datetime.utcnow().isoformat(),
        "stage": stage,
        "expect_json": bool(expect_json),
        "prompt_chars": len(prompt or ""),
        "prompt_lines": len(str(prompt or "").splitlines()),
        "prompt_section_count": len(prompt_sections),
        "prompt_sections": prompt_sections,
        "context": trace_context or {},
    }
    if str(os.getenv("EEA_LLM_TRACE_INCLUDE_PROMPT", "")).lower() in {"1", "true", "yes", "on"}:
        trace_base["prompt"] = prompt

    if not expect_json:
        with _llm_hard_timeout(hard_timeout):
            completion = client.complete(prompt=prompt, temperature=0.0)
        _write_llm_trace({**trace_base, "status": "ok", "attempts": 1})
        return completion

    last_err: Optional[Exception] = None
    for attempt in range(3):
        retry_suffix = ""
        if attempt > 0:
            retry_suffix = (
                "\n\nIMPORTANT: Return only valid JSON (object/array) with double quotes. "
                "No markdown fences, no explanation, no extra text."
            )
        try:
            with _llm_hard_timeout(hard_timeout):
                completion = client.complete(prompt=prompt + retry_suffix, temperature=0.0)
            parsed = _parse_json_block(completion)
            _write_llm_trace(
                {
                    **trace_base,
                    "status": "ok",
                    "attempts": attempt + 1,
                    "retry_suffix_chars": len(retry_suffix),
                    "completion_chars": len(completion or ""),
                }
            )
            return parsed
        except Exception as exc:
            last_err = exc
    _write_llm_trace(
        {
            **trace_base,
            "status": "error",
            "attempts": 3,
            "error": f"{type(last_err).__name__}: {last_err}",
        }
    )
    raise RuntimeError(f"Failed to parse LLM JSON response: {last_err}")
