import json

import httpx
import pytest

from jev_llm_router.config import Config, load_config
from jev_llm_router.protocol import input_keys, user_texts
from jev_llm_router.routing import Router
from jev_llm_router.state import AffinityStore, Decision, RouteError


@pytest.fixture
def router(config):
    store = AffinityStore(config.server.state_path)
    yield Router(config, store)
    store.close()


@pytest.mark.parametrize("prompt,tier,effort", [
    ("Translate this paragraph into English", "luna", "low"),
    ("Add a login form and unit tests", "terra", "medium"),
    ("Investigate this intermittent race condition", "sol", "high"),
    ("Implement an end-to-end architecture across the entire repository", "astra", "medium"),
    ("請翻譯這段文章", "luna", "low"),
    ("修正多執行緒死鎖問題", "sol", "high"),
    ("完成跨系統整合", "astra", "medium"),
    ("Sol is a name in this short story", "terra", "medium"),
])
def test_rules(router, prompt, tier, effort):
    result = router.rules({"input": prompt})
    assert (result.tier, result.effort) == (tier, effort)


def test_only_user_content_is_classified(router):
    body = {"instructions": "cross-system architecture", "input": [
        {"role": "developer", "content": "entire repository"},
        {"type": "function_call_output", "call_id": "old", "output": "formal verification"},
        {"role": "user", "content": [{"type": "input_text", "text": "Translate this"},
                                      {"type": "input_image", "image_url": "data:image/png;base64,abc"}]},
    ]}
    assert router.rules(body).tier == "luna"
    assert user_texts(body) == ["Translate this"]


def test_followup_uses_previous_intent(router):
    body = {"input": [{"role": "user", "content": "Find the race condition"},
                      {"role": "assistant", "content": "I found it"},
                      {"role": "user", "content": "continue"}]}
    assert router.rules(body).tier == "sol"


async def test_explicit_model_and_effort(router):
    result = await router.route({"model": "auto", "input": "Translate this"},
                                {"x-jev-model": "sol", "x-jev-effort": "low"})
    assert (result.model, result.effort, result.reason) == ("gpt-5.6-sol", "low", "explicit")


async def test_codex_defaults_are_replaced(router):
    result = await router.route({"model": "gpt-5.6-terra", "input": "Find the deadlock",
                                  "reasoning": {"effort": "medium"}}, {})
    assert result.tier == "sol" and result.effort == "high"


@pytest.mark.parametrize("body,headers", [
    ({"model": "untrusted-model"}, {}),
    ({"model": []}, {}),
    ({"reasoning": "high"}, {}),
    ({}, {"x-jev-effort": "ultra"}),
    ({}, {"x-jev-model": "astra", "x-jev-effort": "none"}),
    ({}, {"x-jev-session": ""}),
])
async def test_bad_routes_rejected(router, body, headers):
    with pytest.raises(RouteError):
        await router.route(body, headers)


async def test_budget_caps(router):
    router.config.routing.max_tier = "terra"
    router.config.routing.max_effort = "low"
    result = await router.route({"input": "Design the entire repository"}, {})
    assert (result.tier, result.effort) == ("terra", "low")
    with pytest.raises(RouteError):
        await router.route({}, {"x-jev-model": "astra"})


async def test_pin_tool_loop_and_missing_state(router):
    decision = router.rules({"input": "Find the deadlock"})
    router.store.bind(["call:c1", "response:r1"], decision)
    body = {"input": [{"type": "function_call_output", "call_id": "c1", "output": "done"}]}
    assert (await router.route(body, {})).model == "gpt-5.6-sol"
    with pytest.raises(RouteError, match="change route"):
        await router.route(body, {"x-jev-model": "luna", "x-jev-effort": "low"})
    with pytest.raises(RouteError):
        await router.route({"previous_response_id": "missing", "input": "continue"}, {})


async def test_explicit_recovery_requires_effort(router):
    body = {"previous_response_id": "r-old", "input": "continue"}
    with pytest.raises(RouteError):
        await router.route(body, {"x-jev-model": "sol"})
    result = await router.route(body, {"x-jev-model": "sol", "x-jev-effort": "high"})
    assert result.tier == "sol"
    assert (await router.route(body, {})).reason == "affinity"


async def test_unknown_opaque_not_inferred_from_known_state(router):
    router.store.bind(["response:r1"], router.rules({"input": "Find the deadlock"}))
    body = {"previous_response_id": "r1", "input": [
        {"type": "reasoning", "encrypted_content": "unknown-ciphertext"}]}
    with pytest.raises(RouteError, match="Untracked linked state"):
        await router.route(body, {})


async def test_session_pin_and_preview_does_not_mutate(router):
    first = await router.route({"input": "Translate this"}, {"x-jev-session": "session-a"})
    second = await router.route({"input": "Design architecture"}, {"x-jev-session": "session-a"})
    assert first.model == second.model
    await router.route({"input": "Translate this"}, {"x-jev-session": "preview"}, preview=True)
    assert router.store.resolve(["session:preview"])[0] is None


async def test_mapping_change_fails_closed(router):
    await router.route({"input": "Translate this"}, {"x-jev-session": "a"})
    router.config.models["luna"].id = "new-luna"
    with pytest.raises(RouteError, match="mapping changed"):
        await router.route({}, {"x-jev-session": "a"})


def test_historical_tool_calls_and_opaque_state():
    body = {"input": [{"type": "function_call", "call_id": "old"},
                      {"role": "user", "content": "New independent task"}]}
    assert input_keys(body) == []
    body["input"].insert(0, {"type": "compaction", "encrypted_content": "opaque"})
    assert input_keys(body) == ["opaque:opaque"]


@pytest.mark.parametrize("response", [
    {"tier": "sol", "effort": "medium"},
    {"tier": "not-allowed", "effort": "high"},
    {"tier": "sol", "effort": "ultra"},
    {"tier": "sol", "effort": "high", "url": "http://evil.invalid"},
    "not-json-object",
])
async def test_classifier_validation(router, response):
    router.config.routing.mode = "hybrid"
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(response)}]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await router.route({"input": "Build a new feature", "tools": [{"type": "function"}]},
                                    {}, client, {"Authorization": "Bearer upstream"})
    expected = "sol" if response == {"tier": "sol", "effort": "medium"} else "terra"
    assert result.tier == expected
    assert seen[0]["store"] is False
    assert "tools" not in seen[0]
    assert seen[0]["model"] == "gpt-5.6-luna"


async def test_classifier_timeout_falls_back(router):
    router.config.routing.mode = "hybrid"

    def handle(request):
        raise httpx.ReadTimeout("private details must not leak")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await router.route({"input": "Add a button"}, {}, client, {})
    assert result.tier == "terra"


def test_state_persistence_privacy_and_expiry(tmp_path):
    path = str(tmp_path / "affinity.db")
    now = [100.0]
    decision = Decision("sol", "gpt-5.6-sol", "high", "complex")
    first = AffinityStore(path, ttl=60, limit=2, clock=lambda: now[0])
    first.bind(["response:private-id"], decision)
    assert first.db.execute("SELECT key FROM affinity").fetchone()[0] != "response:private-id"
    first.close()
    second = AffinityStore(path, ttl=60, limit=2, clock=lambda: now[0])
    assert second.resolve(["response:private-id"])[0].model == decision.model
    now[0] += 61
    assert second.resolve(["response:private-id"])[0] is None
    second.bind(["a", "b", "c"], decision)
    assert second.db.execute("SELECT count(*) FROM affinity").fetchone()[0] == 2
    second.close()


def test_conflicting_state(router):
    router.store.bind(["a"], router.rules({"input": "Translate this"}))
    router.store.bind(["b"], router.rules({"input": "Find a deadlock"}))
    with pytest.raises(RouteError):
        router.store.resolve(["a", "b"])
    with pytest.raises(RouteError):
        router.store.bind(["a"], router.rules({"input": "Find a deadlock"}))


def test_example_config_and_invalid_upstream():
    config = load_config("examples/router.toml")
    assert config.models["astra"].id == "gpt-6-astra"
    for url in ["http://example.com/v1", "https://secret:pass@example.com/v1", "https://host/v1?key=x"]:
        with pytest.raises(ValueError):
            Config.model_validate({"upstream": {"base_url": url}})
    with pytest.raises(ValueError):
        Config.model_validate({"unknown": "typo"})
