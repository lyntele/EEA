from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx
from openai import APIConnectionError, APITimeoutError, AzureOpenAI, OpenAI, OpenAIError

from method.EEA.rulebook.common.core.config import LLMSettings
from method.EEA.rulebook.common.core.schema import BranchCandidate


@dataclass
class LLMResponse:
    text: str
    raw: Dict[str, Any]


class LLMClient:
    def __init__(self, settings: LLMSettings, timeout: int = 30, max_retries: int = 1):
        self.settings = settings
        self.timeout = timeout
        self.max_retries = max_retries
        self._deepeye_llm = self._build_deepeye_llm()
        self._client = self._build_client()
        self._last_error: Optional[Exception] = None

    def is_ready(self) -> bool:
        return bool(self.settings.model and self.settings.base_url and self.settings.api_key)

    def _build_client(self) -> Optional[OpenAI | AzureOpenAI]:
        if not self.is_ready():
            return None
        if self.settings.api_type == "azure":
            return AzureOpenAI(
                api_key=self.settings.api_key,
                base_url=self.settings.base_url,
                api_version=self.settings.api_version,
            )
        default_headers: Dict[str, str] = {}
        if "openrouter.ai" in (self.settings.base_url or ""):
            default_headers = {
                "HTTP-Referer": "https://github.com/openai/codex",
                "X-Title": "ACE4SQL Rulebook",
            }
        return OpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            default_headers=default_headers or None,
        )

    def _build_deepeye_llm(self) -> Optional[Any]:
        # The DeepEye LLM wrapper caches instances by model name only. In mixed
        # Rulebook/DeepEye processes that can accidentally reuse a different
        # max_tokens/base configuration and make small Rulebook prompts exceed
        # provider context limits. Keep this path opt-in; the direct OpenAI
        # client below respects the Rulebook config exactly.
        if str(os.getenv("RULEBOOK_USE_DEEPEYE_LLM_WRAPPER", "")).lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return None
        if not self.is_ready():
            return None
        try:
            from app.config import LLMConfig
            from app.llm.llm import LLM
        except Exception:
            return None

        try:
            llm_config = LLMConfig(
                model=self.settings.model,
                base_url=self.settings.base_url,
                api_key=self.settings.api_key,
                max_tokens=int(self.settings.max_tokens),
                temperature=float(self.settings.temperature),
                api_type=self.settings.api_type,
                api_version=self.settings.api_version or "",
            )
            return LLM(llm_config)
        except Exception:
            return None

    # Unified public chat interface
    def chat(self, messages: List[Dict[str, str]], temperature: Optional[float] = None) -> Optional[LLMResponse]:
        """
        Public wrapper for a single chat completion call.

        This should be the preferred entry point for callers that already
        build role-based messages.
        """
        return self._chat(messages, temperature)

    def _chat(self, messages: List[Dict[str, str]], temperature: Optional[float]) -> Optional[LLMResponse]:
        if not self.is_ready():
            return None

        if self._deepeye_llm is not None:
            try:
                responses, usage = self._deepeye_llm.ask(
                    messages,
                    timeout=self.timeout,
                    temperature=float(temperature if temperature is not None else self.settings.temperature),
                    max_tokens=int(self.settings.max_tokens),
                )
                if responses:
                    text = str(getattr(responses[0], "content", "") or "").strip()
                    raw = {
                        "choices": [{"message": {"content": text}}],
                        "usage": usage,
                    }
                    if text:
                        return LLMResponse(text=text, raw=raw)
                self._last_error = RuntimeError("Empty LLM response")
            except Exception as exc:
                self._last_error = exc
            return None

        if self._client is None:
            return None

        request_params = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": float(temperature if temperature is not None else self.settings.temperature),
            "max_tokens": int(self.settings.max_tokens),
            "timeout": self.timeout,
        }
        self._last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.chat.completions.create(**request_params)
                raw = response.model_dump() if hasattr(response, "model_dump") else {}
                text = _extract_text(raw)
                if text:
                    return LLMResponse(text=text, raw=raw)
                self._last_error = RuntimeError("Empty LLM response")
            except TimeoutError as exc:
                self._last_error = exc
                return None
            except (APIConnectionError, APITimeoutError, httpx.HTTPError, OpenAIError) as exc:
                self._last_error = exc
                if attempt >= self.max_retries:
                    return None
                time.sleep(min(2 ** attempt, 8))
            except Exception as exc:
                self._last_error = exc
                if attempt >= self.max_retries:
                    return None
                time.sleep(min(2 ** attempt, 8))
        return None

    def complete(self, prompt: str, temperature: float = 0.0, system_prompt: Optional[str] = None) -> str:
        """
        Convenience helper for pattern_system and other simple callers that
        work with a single prompt string.

        Returns the plain text content of the first message. Raises
        RuntimeError if the client is not ready or the call repeatedly fails.
        """
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = self._chat(messages, temperature=temperature)
        if not response:
            if not self.is_ready():
                raise RuntimeError("LLM client not ready")
            raise RuntimeError(f"LLM call failed: {self._last_error}")
        return response.text

    def generate_sql_candidates(self, messages: List[Dict[str, str]]) -> Tuple[List[BranchCandidate], int]:
        """
        Legacy helper retained for compatibility with `repair.py`.

        The model is expected to return either:
        - `{"candidates": [{"sql": "..."}]}`
        - `[{"sql": "..."}]`
        - `{"sql": "..."}`
        """
        response = self._chat(messages, temperature=None)
        if not response:
            return [], 0

        payload = _extract_json_payload(response.text)
        raw_candidates: List[Any]
        if isinstance(payload, dict):
            if isinstance(payload.get("candidates"), list):
                raw_candidates = payload["candidates"]
            else:
                raw_candidates = [payload]
        elif isinstance(payload, list):
            raw_candidates = payload
        else:
            raw_candidates = []

        candidates: List[BranchCandidate] = []
        for idx, item in enumerate(raw_candidates):
            candidate = _coerce_branch_candidate(item, idx)
            if candidate is not None:
                candidates.append(candidate)
        return candidates, _extract_total_tokens(response.raw)


def _resolve_endpoint(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    if base_url.endswith("/v1/"):
        return f"{base_url}chat/completions"
    return f"{base_url.rstrip('/')}/v1/chat/completions"


def _extract_text(raw: Dict[str, Any]) -> str:
    choices = raw.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _extract_json_payload(text: str) -> Any:
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        json_start = min((idx for idx in (text.find("{"), text.find("[")) if idx >= 0), default=-1)
        json_end_obj = text.rfind("}")
        json_end_arr = text.rfind("]")
        json_end = max(json_end_obj, json_end_arr)
        if json_start >= 0 and json_end > json_start:
            return json.loads(text[json_start : json_end + 1])
    return {}


def _coerce_branch_candidate(item: Any, idx: int) -> Optional[BranchCandidate]:
    if isinstance(item, str):
        sql = item.strip()
        if not sql:
            return None
        return BranchCandidate(
            candidate_id=f"LLM_{idx}",
            sql=sql,
            source="LLM",
            op_sequence=["LLM_GENERATOR"],
            op_args_sigs=[],
        )

    if not isinstance(item, dict):
        return None

    sql = str(item.get("sql") or item.get("SQL") or "").strip()
    if not sql:
        return None

    op_sequence = item.get("op_sequence") or item.get("OP_SEQUENCE") or ["LLM_GENERATOR"]
    op_args_sigs = item.get("op_args_sigs") or item.get("OP_ARGS_SIGS") or []
    if not isinstance(op_sequence, list):
        op_sequence = [str(op_sequence)]
    if not isinstance(op_args_sigs, list):
        op_args_sigs = [str(op_args_sigs)]

    return BranchCandidate(
        candidate_id=str(item.get("candidate_id") or item.get("CANDIDATE_ID") or f"LLM_{idx}"),
        sql=sql,
        source=str(item.get("source") or item.get("SOURCE") or "LLM"),
        op_sequence=[str(op) for op in op_sequence],
        op_args_sigs=[str(sig) for sig in op_args_sigs],
    )


def _extract_total_tokens(raw: Dict[str, Any]) -> int:
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return 0
    total_tokens = usage.get("total_tokens")
    if isinstance(total_tokens, int):
        return total_tokens
    return 0
