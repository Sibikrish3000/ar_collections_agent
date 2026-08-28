"""The one place that talks to a model.

Any OpenAI-compatible endpoint works — the official API, a gateway, vLLM, Ollama,
LM Studio, OpenRouter, Azure-style proxies — because the base URL, API key and
model name are all configuration:

    export LLM_BASE_URL=https://my-gateway.example/v1
    export LLM_API_KEY=sk-...
    export LLM_MODEL=my-deployed-model
    ar-collections-agent classify --llm

``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` / ``OPENAI_MODEL`` are honoured as
fallbacks, so an environment already set up for the official API needs no changes.

Two rules hold everywhere this module is used:

* the ``openai`` package is imported lazily, inside the call — a missing or broken
  install can never affect the default deterministic run;
* every failure is returned, not raised. A model outage degrades the agent to its
  rule-based behaviour instead of stopping a collections run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_SECONDS = 30.0

_dotenv_loaded = False


def _load_dotenv() -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    for path in (Path(".env"), Path(__file__).resolve().parents[2] / ".env"):
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
            except OSError:
                pass
            break


def _env(*names: str) -> str | None:
    _load_dotenv()
    for name in names:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Endpoint settings. Read from the environment; overridable per run."""

    api_key: str | None = None
    base_url: str | None = None
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(
        cls, model: str | None = None, base_url: str | None = None
    ) -> LLMConfig:
        """Explicit arguments win over the environment; env wins over defaults."""
        timeout = _env("LLM_TIMEOUT_SECONDS")
        return cls(
            api_key=_env("LLM_API_KEY", "OPENAI_API_KEY"),
            base_url=base_url or _env("LLM_BASE_URL", "OPENAI_BASE_URL"),
            model=model or _env("LLM_MODEL", "OPENAI_MODEL") or DEFAULT_MODEL,
            timeout=float(timeout) if timeout else DEFAULT_TIMEOUT_SECONDS,
        )

    @property
    def is_configured(self) -> bool:
        """A key, or a base URL for a local endpoint that does not need one."""
        return bool(self.api_key or self.base_url)

    def missing_reason(self) -> str:
        return (
            "no LLM endpoint configured: set LLM_API_KEY (or OPENAI_API_KEY), and "
            "LLM_BASE_URL plus LLM_MODEL for a non-default endpoint"
        )

    def describe(self) -> str:
        return f"{self.model} @ {self.base_url or 'api.openai.com'}"


class LLMResult:
    """Outcome of one call: either ``text``, or an ``error`` explaining why not."""

    __slots__ = ("text", "error")

    def __init__(self, text: str = "", error: str = "") -> None:
        self.text = text
        self.error = error

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text)


def complete(
    config: LLMConfig,
    system: str,
    user: str,
    *,
    json_mode: bool = False,
    max_tokens: int = 400,
) -> LLMResult:
    """One chat completion against an OpenAI-compatible endpoint.

    ``temperature=0`` throughout: this agent has no use for creative variance.
    ``json_mode`` asks for a JSON object where the endpoint supports it, and is
    retried without that parameter for endpoints that reject it.
    """
    if not config.is_configured:
        return LLMResult(error=config.missing_reason())

    try:
        from openai import OpenAI  # noqa: PLC0415  (lazy by design)
    except ImportError:
        return LLMResult(
            error="the optional `openai` package is not installed "
            "(pip install 'ar-collections-agent[llm]')"
        )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    kwargs: dict[str, object] = {
        "model": config.model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        client = OpenAI(
            # A local endpoint may not check the key, but the client demands one.
            api_key=config.api_key or "not-required",
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=2,
        )
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception:
            if not json_mode:
                raise
            # Not every OpenAI-compatible server implements response_format.
            kwargs.pop("response_format", None)
            response = client.chat.completions.create(**kwargs)
        return LLMResult(text=(response.choices[0].message.content or "").strip())
    except Exception as exc:  # noqa: BLE001 - a bad model call must not stop a run
        return LLMResult(error=f"{type(exc).__name__}: {exc}")


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


def strip_code_fence(text: str) -> str:
    """Some endpoints wrap JSON in a markdown fence even in JSON mode."""
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text)
    return text.strip()


__all__ = [
    "DEFAULT_MODEL",
    "LLMConfig",
    "LLMResult",
    "complete",
    "strip_code_fence",
]
