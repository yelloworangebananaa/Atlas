"""Offline tests for jarvis/router.py failover + key pools. Run: python test_router.py
Monkeypatches _post (the network seam) so nothing here touches a real endpoint."""
import requests

from jarvis import router

_REAL_POST = router._post      # captured before any test monkeypatches the seam
_REAL_LOAD = router.config.load


def reset():
    router._floor = 0
    router._active = 0
    router.last_switch = None
    router._key_state = {}
    router._rr = {}
    router._post = _REAL_POST   # so the classify test exercises the real _post
    router.config.load = _REAL_LOAD


def set_chain(chain):
    """Force router._chain() to return this chain regardless of config.json."""
    router.config.load = lambda *a, **k: {"model_chain": chain, "llm_base_url": "http://x/v1",
                                          "llm_model": "legacy", "llm_key_env": "K"}


def scripted_post(script):
    """script: {(tier_name, env): [outcome, ...]} where outcome is 'ok' or an Exception class.
    Records every (tier_name, env) into router._calls for order assertions."""
    router._calls = []

    def _p(tier, env, messages, tools):
        router._calls.append((tier["name"], env))
        outcome = script[(tier["name"], env)].pop(0)
        if outcome == "ok":
            return {"content": f"hi from {tier['name']}"}
        raise outcome()

    router._post = _p


T0 = {"name": "T0", "base_url": "http://a/v1", "model": "m0", "key_envs": ["E0"], "rotation": "fill_first"}
T1 = {"name": "T1", "base_url": "http://b/v1", "model": "m1", "key_envs": ["E1"], "rotation": "fill_first"}


def test_legacy_fallback():
    reset()
    router.config.load = lambda *a, **k: {"model_chain": [], "llm_base_url": "http://x/v1",
                                          "llm_model": "glm", "llm_key_env": "K"}
    c = router._chain()
    assert len(c) == 1 and c[0]["name"] == "default" and c[0]["model"] == "glm", c
    print("ok  legacy single-model fallback when model_chain empty")


def test_cross_tier_failover_sets_switch():
    reset()
    set_chain([T0, T1])
    scripted_post({("T0", "E0"): [router._Transient], ("T1", "E1"): ["ok"]})
    router.new_turn()
    msg = router.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "hi from T1", msg
    assert router.last_switch and router.last_switch["provider"] == "T1", router.last_switch
    print("ok  tier0 down -> tier1 answers, last_switch records the model change")


def test_option_a_floor_no_retry_below():
    reset()
    set_chain([T0, T1])
    scripted_post({("T0", "E0"): [router._Transient], ("T1", "E1"): ["ok", "ok"]})
    router.new_turn()
    router.chat([{"role": "user", "content": "a"}])   # T0 fails -> T1, floor=1
    router.chat([{"role": "user", "content": "b"}])   # must start at floor 1, skip T0
    assert router._calls.count(("T0", "E0")) == 1, router._calls
    assert router._calls.count(("T1", "E1")) == 2, router._calls
    print("ok  Option A: turn floor advances, dead lower tier not retried this turn")


def test_new_turn_resets_floor():
    reset()
    set_chain([T0, T1])
    scripted_post({("T0", "E0"): [router._Transient, "ok"], ("T1", "E1"): ["ok"]})
    router.new_turn()
    router.chat([{"role": "user", "content": "a"}])   # T0 fails -> T1, floor=1
    router.new_turn()                                  # new user message resets floor
    msg = router.chat([{"role": "user", "content": "b"}])  # T0 gets a fresh chance
    assert msg["content"] == "hi from T0", msg
    assert router.last_switch is None, "new turn should clear the switch banner"
    print("ok  new_turn resets floor to 0 (dead primary retried next message)")


def test_key_pool_rotate_on_transient():
    reset()
    tier = {"name": "P", "base_url": "http://p/v1", "model": "m", "key_envs": ["A", "B"], "rotation": "fill_first"}
    set_chain([tier])
    scripted_post({("P", "A"): [router._Transient], ("P", "B"): ["ok"]})
    router.new_turn()
    msg = router.chat([{"role": "user", "content": "x"}])
    assert msg["content"] == "hi from P" and router._calls == [("P", "A"), ("P", "B")], router._calls
    assert router.last_switch is None, "same tier, different key is NOT a model change"
    print("ok  key pool rotates A->B on transient; no switch banner (same model)")


def test_429_retry_once_then_cooldown():
    reset()
    tier = {"name": "P", "base_url": "http://p/v1", "model": "m", "key_envs": ["A", "B"], "rotation": "fill_first"}
    set_chain([tier])
    scripted_post({("P", "A"): [router._Retry429, router._Retry429], ("P", "B"): ["ok"]})
    router.new_turn()
    msg = router.chat([{"role": "user", "content": "x"}])
    assert msg["content"] == "hi from P", msg
    assert router._calls == [("P", "A"), ("P", "A"), ("P", "B")], router._calls  # one retry on A
    assert router.key_status("A") == "cooling", "2nd consecutive 429 must cool the key"
    print("ok  429 retries same key once, 2nd consecutive cools + rotates")


def test_402_cools_24h_and_rotates():
    reset()
    tier = {"name": "P", "base_url": "http://p/v1", "model": "m", "key_envs": ["A", "B"], "rotation": "fill_first"}
    set_chain([tier])
    scripted_post({("P", "A"): [router._Quota], ("P", "B"): ["ok"]})
    router.new_turn()
    router.chat([{"role": "user", "content": "x"}])
    assert router.key_status("A") == "cooling" and router._key_state["A"]["until"] - __import__("time").time() > 80000
    print("ok  402 cools the key ~24h and rotates immediately")


def test_chain_exhausted_raises():
    reset()
    set_chain([T0, T1])
    scripted_post({("T0", "E0"): [router._Transient], ("T1", "E1"): [router._Transient]})
    router.new_turn()
    try:
        router.chat([{"role": "user", "content": "x"}])
        assert False, "should have raised ChainExhausted"
    except router.ChainExhausted as exc:
        assert "T0" in str(exc) and "T1" in str(exc), exc
    print("ok  whole chain dead -> ChainExhausted naming every tier")


def test_order_round_robin_and_least_used():
    reset()
    rr = {"name": "R", "key_envs": ["A", "B", "C"], "rotation": "round_robin"}
    assert router._order(rr) == ["A", "B", "C"]
    assert router._order(rr) == ["B", "C", "A"]
    assert router._order(rr) == ["C", "A", "B"]
    reset()
    router._key_state = {"A": {"uses": 5}, "B": {"uses": 1}, "C": {"uses": 3}}
    lu = {"name": "L", "key_envs": ["A", "B", "C"], "rotation": "least_used"}
    assert router._order(lu) == ["B", "C", "A"], router._order(lu)
    print("ok  round_robin cycles, least_used picks fewest-used first")


def test_all_keys_cooling_skips_tier():
    reset()
    tier = {"name": "P", "base_url": "http://p/v1", "model": "m", "key_envs": ["A"], "rotation": "fill_first"}
    set_chain([tier, T1])
    router._key_state = {"A": {"until": __import__("time").time() + 999}}  # A cooling
    scripted_post({("T1", "E1"): ["ok"]})
    router.new_turn()
    msg = router.chat([{"role": "user", "content": "x"}])
    assert msg["content"] == "hi from T1", msg
    assert ("P", "A") not in getattr(router, "_calls", []), "cooling key must not be tried"
    print("ok  tier whose whole pool is cooling is skipped to next tier")


def test_post_classifies_status_codes():
    reset()

    class FakeResp:
        def __init__(self, status, body=None, raise_json=False):
            self.status_code = status
            self.ok = 200 <= status < 300
            self._body = body
            self._rj = raise_json

        def json(self):
            if self._rj:
                raise ValueError("bad json")
            return self._body

    tier = {"name": "X", "base_url": "http://x/v1", "model": "m", "key_envs": []}
    good = {"choices": [{"message": {"content": "hi"}}]}

    def patch(resp):
        router.requests.post = lambda *a, **k: resp

    patch(FakeResp(200, good))
    assert router._post(tier, None, [], None)["content"] == "hi"
    for status, exc in [(429, router._Retry429), (402, router._Quota), (401, router._Auth),
                        (403, router._Auth), (404, router._Transient), (500, router._Transient)]:
        patch(FakeResp(status))
        try:
            router._post(tier, None, [], None)
            assert False, f"{status} should raise"
        except exc:
            pass
    patch(FakeResp(200, {}))           # missing choices
    try:
        router._post(tier, None, [], None); assert False
    except router._Transient:
        pass
    patch(FakeResp(200, raise_json=True))  # malformed body
    try:
        router._post(tier, None, [], None); assert False
    except router._Transient:
        pass

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("refused")
    router.requests.post = boom
    try:
        router._post(tier, None, [], None); assert False
    except router._Transient:
        pass
    print("ok  _post classifies 429/402/401/403/404/5xx/malformed/conn-error correctly")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} ROUTER TESTS PASSED")
