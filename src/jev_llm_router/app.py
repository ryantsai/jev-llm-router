"""Authenticated HTTP/SSE Responses gateway. Never replay a generation automatically."""
from __future__ import annotations

import hmac
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .config import Config, load_config
from .protocol import SSEObserver, output_keys
from .routing import Router
from .state import AffinityStore, Decision, RouteError

log = logging.getLogger(__name__)


def error_response(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "router_error", "code": code}},
                        status_code=status, headers={"Cache-Control": "no-store"})


class ClosingStreamingResponse(StreamingResponse):
    """Close upstream even when the downstream fails before consuming the iterator."""

    def __init__(self, *args, close, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_upstream = close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.close_upstream()


class GatewayGuard:
    """Authenticate before reading bodies; reject browser origins and bound active requests."""

    def __init__(self, app, token: str, limit: int):
        self.app, self.token, self.limit = app, token.encode(), limit
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"")
        scheme, _, credential = auth.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(credential, self.token):
            return await error_response("Invalid gateway credential", 401, "unauthorized")(scope, receive, send)
        if b"origin" in headers:
            return await error_response("Browser-origin requests are not enabled", 403, "origin_denied")(scope, receive, send)
        if self.active >= self.limit:
            response = error_response("Gateway concurrency limit reached", 429, "gateway_busy")
            response.headers["Retry-After"] = "1"
            return await response(scope, receive, send)
        self.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.active -= 1


def upstream_headers(config: Config, key: str, request: Request | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept-Encoding": "identity", "User-Agent": f"jev-llm-router/{__version__}"}
    if config.upstream.organization:
        headers["OpenAI-Organization"] = config.upstream.organization
    if config.upstream.project:
        headers["OpenAI-Project"] = config.upstream.project
    # Never forward client credentials, cookies, project overrides, or proxy/routing headers.
    if request:
        for name in ("openai-beta", "x-client-request-id"):
            if name in request.headers:
                headers[name] = request.headers[name]
    return headers


def downstream_headers(response: httpx.Response, request_id: str,
                       decision: Decision | None) -> dict[str, str]:
    allowed = {"content-type", "x-request-id", "retry-after", "openai-processing-ms"}
    headers = {k: v for k, v in response.headers.items()
               if k in allowed or k.startswith("x-ratelimit-")}
    # httpx yields decompressed entity bytes: do not copy length or content-encoding.
    headers.update({"Cache-Control": "no-store", "X-Jev-Request-Id": request_id})
    if decision:
        headers.update({"X-Jev-Model": decision.model, "X-Jev-Effort": decision.effort,
                        "X-Jev-Route": decision.reason})
    return headers


async def read_payload(request: Request, limit: int) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise RouteError("Content-Type must be application/json", 415, "unsupported_media_type")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise RouteError("Compressed request bodies are not supported", 415, "unsupported_encoding")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise RouteError("Request exceeds configured max_body_bytes", 413, "body_too_large")
        body.extend(chunk)

    def invalid_constant(value):
        raise ValueError("Non-finite JSON number")

    try:
        payload = json.loads(body, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise RouteError("Invalid JSON request", 400, "invalid_json") from None
    if not isinstance(payload, dict):
        raise RouteError("Request body must be a JSON object")
    if "input" in payload and not isinstance(payload["input"], (str, list)):
        raise RouteError("input must be text or a Responses input array")
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise RouteError("stream must be a boolean")
    if payload.get("conversation") is not None:
        raise RouteError("Conversations API state is not supported; use input or previous_response_id")
    return payload


def create_app(config: Config | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    config = config or load_config()
    token, key = config.credentials()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = AffinityStore(config.server.state_path, config.server.affinity_ttl_seconds,
                              config.server.affinity_max_entries)
        timeout = httpx.Timeout(config.upstream.read_timeout,
                                connect=config.upstream.connect_timeout, pool=10)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, trust_env=config.upstream.trust_env,
                transport=transport,
                limits=httpx.Limits(max_connections=config.server.max_inflight + 2,
                                   max_keepalive_connections=config.server.max_inflight),
            ) as client:
                app.state.client = client
                app.state.store = store
                app.state.router = Router(config, store)
                yield
        finally:
            store.close()

    app = FastAPI(title="JEV LLM Router", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GatewayGuard, token=token, limit=config.server.max_inflight)

    @app.exception_handler(RouteError)
    async def routing_error(request: Request, exc: RouteError):
        return error_response(str(exc), exc.status, exc.code)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/v1/models")
    async def models():
        ids = list(dict.fromkeys([*config.routing.auto_models,
                                 *(spec.id for spec in config.models.values())]))
        return {"object": "list", "data": [{"id": model, "object": "model", "created": 0,
                "owned_by": "jev-configured-not-verified"} for model in ids]}

    @app.post("/v1/router/preview")
    async def preview(request: Request):
        payload = await read_payload(request, config.server.max_body_bytes)
        chosen = await app.state.router.route(payload, request.headers, preview=True)
        return {**Router.describe(chosen), "preview": True, "classifier_called": False}

    def remember(data: Any, decision: Decision) -> None:
        if not isinstance(data, dict):
            return
        keys = []
        response = data.get("response", data)
        if isinstance(response, dict):
            identifier = response.get("id")
            if isinstance(identifier, str):
                keys.append("response:" + identifier)
            output = response.get("output", [])
            if isinstance(output, list):
                for item in output:
                    keys.extend(output_keys(item))
        keys.extend(output_keys(data.get("item")))
        if keys:
            app.state.store.bind(keys, decision)

    async def proxy(request: Request, path: str, payload: dict | None = None,
                    decision: Decision | None = None):
        request_id = uuid.uuid4().hex
        if decision:
            # Fixed labels only: no prompt, session, response content, or credentials.
            log.info("route request_id=%s model=%s effort=%s reason=%s",
                     request_id, decision.model, decision.effort, decision.reason)
        client = app.state.client
        upstream_request = client.build_request(
            request.method, config.upstream.base_url + path,
            headers=upstream_headers(config, key, request), json=payload,
            params=request.query_params if request.method == "GET" else None,
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            return error_response("Upstream timed out; the request was not retried", 504, "upstream_timeout")
        except httpx.HTTPError:
            return error_response("Upstream connection failed; the request was not retried", 502, "upstream_connection")

        async def close():
            with anyio.CancelScope(shield=True):
                await upstream.aclose()

        headers = downstream_headers(upstream, request_id, decision)
        if 300 <= upstream.status_code < 400:
            await close()
            return error_response("Upstream redirects are not followed", 502, "upstream_redirect")
        is_sse = "text/event-stream" in upstream.headers.get("content-type", "").lower()
        if is_sse and upstream.is_success:
            headers["X-Accel-Buffering"] = "no"
            observer = SSEObserver(lambda event: remember(event, decision)) if decision else None

            async def stream():
                try:
                    async for chunk in upstream.aiter_bytes():
                        if observer:
                            observer.feed(chunk)
                        yield chunk
                except (httpx.HTTPError, RouteError):
                    log.warning("upstream_stream_failed request_id=%s", request_id)
                    # HTTP status has already been sent. Explicitly fail the SSE stream,
                    # rather than silently retrying or pretending a partial response completed.
                    yield (b'\n\nevent: error\ndata: {"type":"error","code":"router_stream_error",'
                           b'"message":"Upstream stream interrupted; not retried."}\n\n')
                finally:
                    await close()
            return ClosingStreamingResponse(stream(), status_code=upstream.status_code, headers=headers, close=close)
        try:
            content = await upstream.aread()
            if upstream.is_success and decision:
                try:
                    remember(json.loads(content), decision)
                except (ValueError, UnicodeError, RecursionError):
                    # Preserve an unexpected upstream body; never pretend its state is known.
                    log.warning("upstream_non_json request_id=%s", request_id)
            return Response(content, status_code=upstream.status_code, headers=headers)
        except httpx.TimeoutException:
            return error_response("Upstream response timed out; not retried", 504, "upstream_timeout")
        except httpx.HTTPError:
            return error_response("Upstream response was interrupted; not retried", 502, "upstream_connection")
        finally:
            await close()

    @app.post("/v1/responses")
    @app.post("/v1/responses/compact")
    async def responses(request: Request):
        payload = await read_payload(request, config.server.max_body_bytes)
        chosen = await app.state.router.route(payload, request.headers, app.state.client,
                                              upstream_headers(config, key))
        outgoing = dict(payload)
        outgoing["model"] = chosen.model
        compact = request.url.path.endswith("/compact")
        if not compact:
            outgoing["reasoning"] = {**(payload.get("reasoning") or {}), "effort": chosen.effort}
        return await proxy(request, "/responses/compact" if compact else "/responses", outgoing, chosen)

    @app.api_route("/v1/responses/{response_id}", methods=["GET", "DELETE"])
    @app.get("/v1/responses/{response_id}/input_items")
    @app.post("/v1/responses/{response_id}/cancel")
    async def response_lifecycle(request: Request, response_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", response_id):
            raise RouteError("Invalid response ID")
        decision, _ = app.state.store.resolve(["response:" + response_id])
        # One upstream account per gateway: unknown IDs may be retrieved but are not rerouted.
        path = request.url.path.removeprefix("/v1")
        return await proxy(request, path, decision=decision)

    @app.websocket("/v1/responses")
    async def no_websocket(websocket: WebSocket):
        # Deliberately unsupported; the shipped Codex provider disables this transport.
        await websocket.close(code=1008, reason="Use HTTP/SSE; supports_websockets=false")

    return app
