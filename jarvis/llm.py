"""One OpenAI-compatible client. Same code path covers Ollama /v1, LM Studio /v1,
and https://integrate.api.nvidia.com/v1 — that's why there is no provider abstraction."""
import requests

from jarvis import config


def build_payload(model, messages, tools=None):
    """Pure function so tests can assert on the request body without network."""
    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    return payload


def chat(messages, tools=None):
    """Assistant message via the failover router (chain + key pools). Kept as a thin
    shim so existing callers/tests keep working; the real logic lives in jarvis.router.
    Lazy import avoids a cycle (router imports build_payload from here)."""
    from jarvis import router
    return router.chat(messages, tools)
