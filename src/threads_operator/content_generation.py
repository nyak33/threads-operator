"""Provider-agnostic LLM text generation for engagement drafts.

Configuration is resolved at call time, never hardcoded:

1. ``THREADS_OPERATOR_LLM_*`` environment overrides (base_url / api_key / model).
2. The operator's own Hermes config (``~/.hermes/config.yaml``): primary
   ``model`` block, then ``fallback_providers``, then ``custom_providers``.
   ``${ENV_VAR}`` references in api_key values are expanded from the process
   environment.

Anything that fails on one provider falls through to the next. All providers
must speak the OpenAI chat-completions wire format (``POST {base_url}/chat/
completions``); providers that do not are skipped with a logged reason.

No module-level state: every call re-reads config so credential rotation and
tests do not fight cached clients.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path.home() / ".hermes" / "config.yaml"
_ENV_PREFIX = "THREADS_OPERATOR_LLM_"


@dataclass(frozen=True)
class LLMProviderSpec:
    """One resolved provider endpoint."""

    name: str
    base_url: str
    api_key: str
    model: str
    max_tokens: int = 8192

    @property
    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def _expand_env(value: Any) -> Any:
    """Expand ${VAR} references in config strings from os.environ."""
    if not isinstance(value, str):
        return value

    def _sub(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, value)


def _spec_from_block(name: str, block: dict[str, Any]) -> LLMProviderSpec | None:
    base_url = _expand_env(block.get("base_url"))
    api_key = _expand_env(block.get("api_key") or block.get("key"))
    model = block.get("model") or block.get("default")
    if isinstance(model, dict):  # primary block nests under 'default'
        model = model.get("model") or block.get("default")
    if isinstance(block.get("model"), dict):
        model = block["model"].get("model")
    if not (base_url and api_key and model):
        return None
    try:
        max_tokens = int(block.get("max_tokens") or 8192)
    except (TypeError, ValueError):
        max_tokens = 8192
    return LLMProviderSpec(
        name=name,
        base_url=str(base_url),
        api_key=str(api_key),
        model=str(model),
        max_tokens=max_tokens,
    )


def resolve_providers(config_path: Path | None = None) -> list[LLMProviderSpec]:
    """Resolve the ordered provider chain. Empty list = generation unavailable."""
    override_base = os.environ.get(f"{_ENV_PREFIX}BASE_URL")
    override_key = os.environ.get(f"{_ENV_PREFIX}API_KEY")
    override_model = os.environ.get(f"{_ENV_PREFIX}MODEL")
    if override_base and override_key and override_model:
        return [
            LLMProviderSpec(
                name="env-override",
                base_url=override_base,
                api_key=override_key,
                model=override_model,
                max_tokens=int(os.environ.get(f"{_ENV_PREFIX}MAX_TOKENS", "8192")),
            )
        ]

    path = config_path or _DEFAULT_CONFIG
    if not path.exists():
        logger.warning("LLM config not found at %s", path)
        return []
    try:
        import yaml  # local import: yaml is an optional dependency

        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.exception("failed to parse LLM config %s", path)
        return []

    specs: list[LLMProviderSpec] = []

    primary = cfg.get("model")
    if isinstance(primary, dict):
        block = dict(primary)
        if isinstance(block.get("model"), str) and "default" in primary:
            pass  # already flat
        elif "default" in primary and isinstance(primary.get("default"), str):
            block["model"] = primary["default"]
        spec = _spec_from_block("primary", block)
        if spec:
            specs.append(spec)

    for idx, fb in enumerate(cfg.get("fallback_providers") or []):
        if isinstance(fb, dict):
            spec = _spec_from_block(f"fallback-{idx}", fb)
            if spec:
                specs.append(spec)

    for cp in cfg.get("custom_providers") or []:
        if not isinstance(cp, dict):
            continue
        block = dict(cp)
        if not block.get("api_key") and block.get("key_env"):
            block["api_key"] = os.environ.get(str(block["key_env"]), "")
        model = block.get("model")
        if isinstance(model, dict):
            model = model.get("model")
        if not model and block.get("models"):
            first = block["models"][0]
            model = first.get("id") if isinstance(first, dict) else first
        block["model"] = model
        spec = _spec_from_block(str(cp.get("name") or "custom"), block)
        if spec:
            specs.append(spec)

    return specs


def generate_text(
    *,
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int = 700,
    temperature: float = 0.7,
    providers: list[LLMProviderSpec] | None = None,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> str:
    """Generate text with the first provider that answers. Raises RuntimeError if all fail."""
    chain = providers if providers is not None else resolve_providers()
    if not chain:
        raise RuntimeError("no LLM providers configured")

    errors: list[str] = []
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        for spec in chain:
            payload = {
                "model": spec.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": min(max_output_tokens, spec.max_tokens),
                "temperature": temperature,
            }
            try:
                response = http.post(
                    spec.chat_completions_url,
                    headers={
                        "Authorization": f"Bearer {spec.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json() or {}
                choices = data.get("choices") or []
                content = ((choices[0] or {}).get("message") or {}).get("content") if choices else None
                if isinstance(content, list):  # some gateways return content parts
                    content = "".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                if content and str(content).strip():
                    return str(content).strip()
                errors.append(f"{spec.name}: empty completion")
            except Exception as exc:  # noqa: BLE001 - record and try next provider
                errors.append(f"{spec.name}: {exc}")
                logger.warning("LLM provider %s failed: %s", spec.name, exc)
    finally:
        if owns_client:
            http.close()

    raise RuntimeError("all LLM providers failed: " + "; ".join(errors))


def generate_json(
    *,
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int = 900,
    providers: list[LLMProviderSpec] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Generate a JSON object. The system prompt must demand pure JSON output;
    this helper strips code fences and parses, raising ValueError on bad output."""
    text = generate_text(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_output_tokens=max_output_tokens,
        temperature=0.4,
        providers=providers,
        client=client,
    )
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"LLM did not return a JSON object: {cleaned[:200]}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON root is not an object")
    return parsed
