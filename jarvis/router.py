"""Multi-provider failover + per-tier API-key pools in front of the LLM call.

Chain = ordered tiers tried in sequence (cross-provider failover). Within a tier a
pool of API keys is rotated (rate-limit headroom) before advancing to the next tier.
Failover is turn-scoped (Option A): a turn only ever advances through the chain; the
next turn resets to tier 0.

    new_turn()  agent calls once per user message  -> floor=0, last_switch=None
    chat()      agent calls each tool round         -> walks chain from the turn floor

      TURN (floor=0):
        chat(): try tier[floor] key-pool -> ok  -> floor=max(floor,i), return msg
                                         -> all -> tier+1 ... -> ChainExhausted
        next round chat(): starts at floor, never retries a tier BELOW it (so a dead
        or timing-out lower tier is not re-tried 6x per turn — Hermes "at most once").
      NEXT TURN: new_turn() -> floor=0 (dead primary gets a fresh chance every message)

Key-pool rotation (within one tier), cooling keys skipped:
    fill_first (default) | round_robin | least_used | random
Per-key errors:  429 -> retry once, 2nd consecutive -> 1h cooldown + rotate
                 402 -> 24h cooldown + rotate   401/403 -> cooldown + rotate
                 timeout/conn/5xx/404/malformed -> rotate
State (cooldowns, use counts) is in-memory: a restart re-tries a cooled key once,
it re-fails and re-cools. # ponytail: no persistence until restarts get frequent.
"""
import os
import random
import time

import requests

from jarvis import config, llm
from jarvis.audit import audit

COOLDOWN_429 = 3600      # 1h after a second consecutive 429
COOLDOWN_402 = 86400     # 24h after quota exhaustion
TIMEOUT = 300            # matches the old llm.chat; a real generation can be slow

last_switch = None       # {provider, model, primary} when the answering tier != primary; agent reads it
_floor = 0               # lowest chain index this turn may use (monotonic within a turn)
_active = 0              # index of the tier that last answered (for the UI active dot)
_key_state = {}          # env-var name -> {"until": epoch, "uses": int, "c429": int}
_rr = {}                 # tier name -> round-robin cursor


class ChainExhausted(RuntimeError):
    """Every tier failed this turn. Subclass of RuntimeError so the server envelope catches it."""


class _Retry429(Exception): pass   # HTTP 429
class _Quota(Exception): pass      # HTTP 402
class _Auth(Exception): pass       # HTTP 401/403
class _Transient(Exception): pass  # timeout/conn/5xx/404/malformed -> rotate


def new_turn():
    """Reset per-turn failover state. Agent calls this once per user message."""
    global _floor, last_switch
    _floor = 0
    last_switch = None


def _chain(cfg=None):
    cfg = cfg or config.load()
    chain = cfg.get("model_chain") or []
    if chain:
        return chain
    # ponytail: no chain configured -> wrap the legacy single-model settings so
    # existing installs (user's z-ai/glm-5.2 on NVIDIA) keep working unchanged.
    return [{
        "name": "default", "base_url": cfg["llm_base_url"], "model": cfg["llm_model"],
        "vision": True, "key_envs": [cfg.get("llm_key_env") or "JARVIS_LLM_API_KEY"],
        "rotation": "fill_first",
    }]


def _healthy(env):
    st = _key_state.get(env)
    return not st or st.get("until", 0) <= time.time()


def _order(tier):
    """Env-var names to try this tier per rotation strategy, cooling ones dropped.
    No key_envs -> [None] (anonymous, e.g. local Ollama). All cooling -> []."""
    envs = tier.get("key_envs") or []
    if not envs:
        return [None]
    healthy = [e for e in envs if _healthy(e)]
    if not healthy:
        return []
    strat = tier.get("rotation", "fill_first")
    if strat == "round_robin":
        i = _rr.get(tier["name"], 0) % len(healthy)
        _rr[tier["name"]] = i + 1
        return healthy[i:] + healthy[:i]
    if strat == "least_used":
        return sorted(healthy, key=lambda e: _key_state.get(e, {}).get("uses", 0))
    if strat == "random":
        random.shuffle(healthy)
        return healthy
    return healthy  # fill_first: listed order


def _cooldown(env, secs):
    if env is not None:
        _key_state.setdefault(env, {})["until"] = time.time() + secs


def _use(env):
    if env is not None:
        st = _key_state.setdefault(env, {})
        st["uses"] = st.get("uses", 0) + 1
        st["c429"] = 0  # a success breaks a 429 streak


def _post(tier, env, messages, tools):
    """One HTTP attempt. Returns the assistant message dict or raises a classified error.
    This is the seam tests monkeypatch — nothing above it touches the network."""
    headers = {"Content-Type": "application/json"}
    key = os.environ.get(env, "") if env else ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    base = tier["base_url"].rstrip("/")
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            json=llm.build_payload(tier["model"], messages, tools),
            headers=headers, timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException:
        raise _Transient  # connection refused, timeout, DNS, TLS...
    s = resp.status_code
    if s == 429:
        raise _Retry429
    if s == 402:
        raise _Quota
    if s in (401, 403):
        raise _Auth
    if not resp.ok:
        raise _Transient  # 404, 5xx, anything else non-2xx
    try:
        return resp.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError):
        raise _Transient  # malformed / empty choices -> fail over, don't crash


def _attempt_key(tier, env, messages, tools):
    """Try one key with a single retry on a first 429. Returns a msg dict, or None to rotate."""
    for attempt in (1, 2):
        try:
            msg = _post(tier, env, messages, tools)
            _use(env)
            return msg
        except _Retry429:
            c = 1
            if env is not None:
                st = _key_state.setdefault(env, {})
                st["c429"] = c = st.get("c429", 0) + 1
            if c >= 2 or attempt == 2:  # second consecutive 429 -> cool + rotate
                _cooldown(env, COOLDOWN_429)
                return None
            # first blip: loop once more on the SAME key
        except _Quota:
            _cooldown(env, COOLDOWN_402)
            return None
        except _Auth:
            _cooldown(env, COOLDOWN_429)  # no refresh mechanism -> cool + rotate
            return None
        except _Transient:
            return None
    return None


def _try_tier(tier, messages, tools):
    """Walk this tier's key pool. Returns a msg dict, or None if the whole pool failed."""
    for env in _order(tier):
        msg = _attempt_key(tier, env, messages, tools)
        if msg is not None:
            return msg
    return None


def _needs_vision(messages):
    """True if any message carries an image part (OpenAI multimodal content array)."""
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            return True
    return False


def chat(messages, tools=None, quiet=False):
    """Walk the chain from the turn floor; return the first tier's answer. Raise
    ChainExhausted if every tier from the floor down fails. quiet=True (internal calls
    like reflect) skips the switch banner/audit so it can't spoof the user-facing turn."""
    global _floor, _active, last_switch
    chain = _chain()
    _floor = min(_floor, len(chain) - 1)
    need_vision = _needs_vision(messages)  # skip text-only tiers when an image is attached (§1)
    if need_vision and not any(t.get("vision") for t in chain[_floor:]):
        raise ChainExhausted("This message has an image but no vision-capable tier is configured.")
    tried = []
    for i in range(_floor, len(chain)):
        tier = chain[i]
        if need_vision and not tier.get("vision"):
            continue
        msg = _try_tier(tier, messages, tools)
        if msg is not None:
            if i > 0 and not quiet:  # answered by a non-primary tier -> a real model change
                last_switch = {"provider": tier["name"], "model": tier["model"],
                               "primary": chain[0]["name"]}
                audit("model_failover", provider=tier["name"], model=tier["model"], tier=i)
            _floor, _active = max(_floor, i), i
            return msg
        tried.append(tier["name"])
    raise ChainExhausted(
        "All model providers failed (" + ", ".join(tried) + "). Check the Model Hierarchy."
    )


def active_tier():
    """Name of the tier that last answered, for the UI active indicator."""
    chain = _chain()
    return chain[min(_active, len(chain) - 1)]["name"] if chain else None


def key_status(env):
    """'cooling' if this key is in a cooldown window, else 'healthy'."""
    return "healthy" if _healthy(env) else "cooling"
