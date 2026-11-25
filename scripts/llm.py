"""Minimal client for an OpenAI-compatible chat API (llama-server on the Mac).

Only what the pipeline needs: a health check, plain chat, and chat that must return JSON
matching a schema. The API key, if llama-server was started with --api-key, comes from
the LLAMA_API_KEY environment variable.
"""
import json
import os
import re
import time

import requests

from kobo_config import CFG


class LLMError(Exception):
    pass


def _base() -> str:
    return CFG["llm_base_url"].rstrip("/")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = os.environ.get("LLAMA_API_KEY")
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def model_for(task: str) -> str:
    models = CFG.get("llm_models") or {}
    return models.get(task) or models.get("rank") or ""


def available(timeout: float = 10) -> bool:
    """True if the server answers. llama-server exposes /health at the root, not under /v1."""
    root = re.sub(r"/v1$", "", _base())
    for url in (f"{root}/health", f"{_base()}/models"):
        try:
            r = requests.get(url, headers=_headers(), timeout=timeout)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            continue
    return False


def chat(messages: list[dict], task: str = "rank", think: bool = False,
         schema: "dict | None" = None, temperature: float = 0.3,
         max_tokens: int = 4096, retries: int = 2) -> str:
    body = {
        "model": model_for(task),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Qwen3.x / Gemma templates: turn the reasoning block on or off per call.
        "chat_template_kwargs": {"enable_thinking": think},
    }
    if schema:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "out", "schema": schema, "strict": True}}
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(f"{_base()}/chat/completions", headers=_headers(),
                              data=json.dumps(body), timeout=CFG["llm_timeout"])
            if r.status_code != 200:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
            choice = r.json()["choices"][0]
            content = (choice["message"].get("content") or "").strip()
            if not content and choice.get("finish_reason") == "length":
                # thinking used up max_tokens: retrying the same request would fail the same way
                raise LLMError("ran out of tokens before answering (raise max_tokens or turn thinking off)")
            return content
        except (requests.RequestException, LLMError, KeyError, ValueError) as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise LLMError(f"chat failed: {last}")


def chat_json(messages: list[dict], schema: dict, **kw) -> "dict | list":
    text = chat(messages, schema=schema, **kw)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except ValueError as e:
        # A model that ignored response_format sometimes wraps JSON in prose.
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise LLMError(f"not JSON: {text[:200]}") from e
