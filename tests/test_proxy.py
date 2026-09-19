import gzip
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from jev_llm_router.app import create_app
from jev_llm_router.protocol import SSEObserver


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, fail=False):
        self.chunks, self.fail, self.closed = chunks, fail, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError("sensitive upstream detail")

    async def aclose(self):
        self.closed = True


def event(payload):
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


def test_payload_and_credential_separation(config, auth):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "r1", "model": "gpt-5.6-sol", "output": []},
                              headers={"x-request-id": "upstream-id", "set-cookie": "do-not-forward"})

    body = {"model": "gpt-5.6-terra", "input": "Find this race condition",
            "tools": [{"type": "function", "name": "run", "parameters": {"type": "object"}}],
            "reasoning": {"effort": "medium", "summary": "auto"}, "store": False,
            "include": ["reasoning.encrypted_content"], "unknown_future_field": {"keep": True}}
    with TestClient(create_app(config, transport=httpx.MockTransport(handle))) as client:
        response = client.post("/v1/responses", json=body, headers={**auth, "Cookie": "private",
                              "OpenAI-Project": "untrusted", "X-Jev-Session": "a"})
    assert response.status_code == 200
    actual = json.loads(seen[0].content)
    assert actual == {**body, "model": "gpt-5.6-sol", "reasoning": {"effort": "high", "summary": "auto"}}
    assert seen[0].headers["authorization"] == "Bearer test-upstream-credential"
    assert "cookie" not in seen[0].headers and "openai-project" not in seen[0].headers
    assert "x-jev-session" not in seen[0].headers
    assert response.headers["x-jev-model"] == "gpt-5.6-sol"
    assert response.headers["x-request-id"] == "upstream-id"
    assert "set-cookie" not in response.headers


def test_sse_unchanged_and_tool_continuation(config, auth):
    original = b": keepalive\n\n" + event({"type": "response.created", "response": {"id": "r1"}})
    original += event({"type": "response.output_item.done", "item": {
        "id": "fc1", "type": "function_call", "call_id": "c1", "name": "run", "arguments": "{}"}})
    original += event({"type": "response.output_text.delta", "delta": "繁體中文"})
    original += event({"type": "response.completed", "response": {"id": "r1", "output": []}})
    stream = Chunks([original[i:i + 7] for i in range(0, len(original), 7)])
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "r2", "output": []})

    with TestClient(create_app(config, transport=httpx.MockTransport(handle))) as client:
        response = client.post("/v1/responses", headers=auth, json={
            "input": "Find the deadlock", "model": "auto", "stream": True})
        assert response.content == original
        assert stream.closed
        followup = client.post("/v1/responses", headers=auth, json={"model": "auto", "input": [
            {"type": "function_call_output", "call_id": "c1", "output": "file contents"}]})
        assert followup.status_code == 200
        assert seen[1]["model"] == "gpt-5.6-sol" and seen[1]["reasoning"]["effort"] == "high"


def test_compaction_preserves_opaque_window(config, auth):
    seen = []
    opaque = [{"type": "compaction", "id": "cmp1", "encrypted_content": "opaque-state"},
              {"type": "message", "role": "user", "content": "retained"}]

    def handle(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": "compact1", "output": opaque})

    with TestClient(create_app(config, transport=httpx.MockTransport(handle))) as client:
        compact = client.post("/v1/responses/compact", headers=auth,
                              json={"model": "auto", "input": "Find the deadlock"})
        assert compact.json()["output"] == opaque
        assert "reasoning" not in seen[0][1]
        followup = client.post("/v1/responses", headers=auth, json={"input": opaque + [
            {"role": "user", "content": "Translate this"}]})
        assert followup.status_code == 200
        assert seen[1][1]["model"] == "gpt-5.6-sol"
        assert seen[1][1]["input"] == opaque + [{"role": "user", "content": "Translate this"}]


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_upstream_status_no_retries(config, auth, status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "upstream error"}},
                              headers={"retry-after": "5", "x-ratelimit-remaining-requests": "0"})

    with TestClient(create_app(config, transport=httpx.MockTransport(handle))) as client:
        response = client.post("/v1/responses", headers=auth, json={"input": "test"})
    assert response.status_code == status and len(calls) == 1
    assert response.headers["retry-after"] == "5"


@pytest.mark.parametrize("exc,status", [(httpx.ConnectError, 502), (httpx.ReadTimeout, 504)])
def test_network_errors_sanitized(config, auth, exc, status):
    def handle(request):
        raise exc("secret credential")

    with TestClient(create_app(config, transport=httpx.MockTransport(handle))) as client:
        response = client.post("/v1/responses", headers=auth, json={"input": "test"})
    assert response.status_code == status
    assert "secret credential" not in response.text


def test_interrupted_stream_errors_and_closes(config, auth):
    stream = Chunks([event({"type": "response.created", "response": {"id": "r1"}})], fail=True)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, stream=stream, headers={"content-type": "text/event-stream"}))
    with TestClient(create_app(config, transport=transport)) as client:
        response = client.post("/v1/responses", headers=auth, json={"input": "test", "stream": True})
    assert b"router_stream_error" in response.content
    assert b"sensitive upstream detail" not in response.content
    assert stream.closed


def test_compressed_upstream_decoded_correctly(config, auth):
    original = event({"type": "response.completed", "response": {"id": "r1"}})
    compressed = gzip.compress(original)
    stream = Chunks([compressed])
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, stream=stream, headers={"content-type": "text/event-stream", "content-encoding": "gzip",
                                   "content-length": str(len(compressed))}))
    with TestClient(create_app(config, transport=transport)) as client:
        response = client.post("/v1/responses", headers=auth, json={"input": "test", "stream": True})
    assert response.content == original
    assert "content-encoding" not in response.headers and "content-length" not in response.headers


def test_redirect_not_followed(config, auth):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://evil.invalid"})

    with TestClient(create_app(config, transport=httpx.MockTransport(handle))) as client:
        response = client.post("/v1/responses", headers=auth, json={"input": "test"})
    assert response.status_code == 502 and len(seen) == 1


@pytest.mark.parametrize("path,method", [("/v1/responses/r1", "get"),
    ("/v1/responses/r1", "delete"), ("/v1/responses/r1/cancel", "post"),
    ("/v1/responses/r1/input_items?limit=2", "get")])
def test_response_lifecycle(config, auth, path, method):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "r1"})

    with TestClient(create_app(config, transport=httpx.MockTransport(handle))) as client:
        assert getattr(client, method)(path, headers=auth).status_code == 200
    assert seen[0].method == method.upper()
    assert str(seen[0].url).endswith(path)


def test_sse_observer_boundaries_and_oversize():
    events = []
    observer = SSEObserver(events.append, limit=64)
    payload = b'data: {"type":\r\ndata: "ok"}\r\n\r\n'
    for byte in payload:
        observer.feed(bytes([byte]))
    assert events == [{"type": "ok"}]
    observer.feed(b"data: " + b"x" * 10000 + b"\n\n")
    observer.feed(b"data: invalid\n\n")
    observer.feed(b"data: [DONE]\n\n")
    observer.feed(event({"type": "after"}))
    assert events[-1] == {"type": "after"}
    assert len(observer.line) <= 64


@pytest.mark.parametrize("spec,fail_send", [("2.3", False), ("2.4", True)])
async def test_disconnect_closes_upstream(config, auth, spec, fail_send):
    import anyio
    from starlette.requests import ClientDisconnect

    emitted = anyio.Event()
    forever = anyio.Event()

    class SlowStream(Chunks):
        async def __aiter__(self):
            yield event({"type": "response.created", "response": {"id": "r1"}})
            await forever.wait()

    stream = SlowStream([])
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, stream=stream, headers={"content-type": "text/event-stream"}))
    app = create_app(config, transport=transport)
    body = json.dumps({"input": "Test", "stream": True}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec},
             "method": "POST", "scheme": "http", "path": "/v1/responses", "raw_path": b"/v1/responses",
             "query_string": b"", "root_path": "", "http_version": "1.1",
             "server": ("localhost", 8765), "client": ("127.0.0.1", 1234),
             "headers": [(b"authorization", auth["Authorization"].encode()),
                         (b"content-type", b"application/json")]}
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await emitted.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            emitted.set()
            if fail_send:
                raise OSError("downstream disconnected")

    async with app.router.lifespan_context(app):
        with anyio.fail_after(2):
            try:
                await app(scope, receive, send)
            except ClientDisconnect:
                assert fail_send
    assert stream.closed


async def test_concurrent_admission_limit(config, auth):
    import anyio

    entered, release = anyio.Event(), anyio.Event()
    config.server.max_inflight = 1

    async def handle(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"id": "r1", "output": []})

    app = create_app(config, transport=httpx.MockTransport(handle))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            async def first():
                response = await client.post("/v1/responses", headers=auth, json={"input": "test"})
                assert response.status_code == 200

            async with anyio.create_task_group() as group:
                group.start_soon(first)
                await entered.wait()
                second = await client.post("/v1/responses", headers=auth, json={"input": "test"})
                assert second.status_code == 429
                release.set()
            assert (await client.get("/v1/models", headers=auth)).status_code == 200
