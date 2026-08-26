"""Tiny pluggable LLM transport layer.

Zero new runtime dependencies: uses only stdlib `urllib.request` + `json` for
HTTP-based providers, and `subprocess` to shell out to the `claude` CLI.
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when an LLM transport fails to produce a completion."""


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key_env: str
    default_model: str
    extra_body: dict


PROVIDERS: dict[str, Provider] = {
    "minimax": Provider(
        name="minimax",
        base_url="https://api.minimax.io/v1",
        api_key_env="MINIMAX_API_KEY",
        default_model="MiniMax-M3",
        extra_body={"thinking": {"type": "disabled"}},
    ),
    "openai": Provider(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-5-mini",
        extra_body={},
    ),
    "claude_cli": Provider(
        name="claude_cli",
        base_url="",
        api_key_env="",
        default_model="",
        extra_body={},
    ),
}


def available(transport: str) -> bool:
    """Return True if `transport` is known and its requirement is satisfied."""
    provider = PROVIDERS.get(transport)
    if provider is None:
        return False
    if transport == "claude_cli":
        return shutil.which("claude") is not None
    return bool(os.environ.get(provider.api_key_env))


def complete(
    prompt: str,
    *,
    system: str | None = None,
    transport: str = "minimax",
    model: str | None = None,
    timeout: int = 30,
    max_tokens: int = 800,
) -> str:
    """Complete `prompt` using the given transport, returning the text response."""
    provider = PROVIDERS.get(transport)
    if provider is None:
        raise LLMError(f"Unknown LLM transport: {transport!r}")

    if transport == "claude_cli":
        return _complete_claude_cli(prompt, system=system, model=model, timeout=timeout)

    return _complete_openai_compatible(
        prompt,
        provider=provider,
        system=system,
        model=model,
        timeout=timeout,
        max_tokens=max_tokens,
    )


def _complete_openai_compatible(
    prompt: str,
    *,
    provider: Provider,
    system: str | None,
    model: str | None,
    timeout: int,
    max_tokens: int,
) -> str:
    api_key = os.environ.get(provider.api_key_env)
    if not api_key:
        raise LLMError(
            f"Missing API key for {provider.name!r}: set the {provider.api_key_env} "
            "environment variable."
        )

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model or provider.default_model,
        "messages": messages,
        "max_tokens": max_tokens,
        **provider.extra_body,
    }

    url = f"{provider.base_url}/chat/completions"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw_body = ""
        try:
            raw_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        snippet = raw_body[:300]
        raise LLMError(f"{provider.name} request failed with HTTP {exc.code}: {snippet}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise LLMError(f"{provider.name} request failed: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"{provider.name} returned invalid JSON: {exc}") from exc

    base_resp = parsed.get("base_resp") if isinstance(parsed, dict) else None
    if isinstance(base_resp, dict) and base_resp.get("status_code"):
        status_msg = base_resp.get("status_msg", "unknown error")
        raise LLMError(f"{provider.name} error: {status_msg}")

    try:
        choices = parsed["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"{provider.name} returned an unexpected response shape") from exc

    return _extract_text(content, provider.name)


def _extract_text(content, provider_name: str) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "".join(parts)
    raise LLMError(f"{provider_name} returned an unexpected content shape")


def _complete_claude_cli(
    prompt: str,
    *,
    system: str | None,
    model: str | None,
    timeout: int,
) -> str:
    input_text = f"{system}\n\n{prompt}" if system else prompt
    cmd = ["claude", "-p", "--model", model or "haiku", "--output-format", "json"]

    try:
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude CLI timed out after {timeout}s") from exc
    except OSError as exc:
        raise LLMError(f"failed to invoke claude CLI: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:300]
        raise LLMError(f"claude CLI exited with code {result.returncode}: {stderr}")

    stdout = result.stdout or ""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()

    if isinstance(parsed, dict) and isinstance(parsed.get("result"), str):
        return parsed["result"]

    return stdout.strip()
