import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from jev_llm_router.app import create_app
from jev_llm_router.cli import main


def test_auth_origin_health_models_preview(config, auth):
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500))
    with TestClient(create_app(config, transport=transport)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/v1/models", headers={**auth, "Origin": "https://evil.invalid"}).status_code == 403
        assert client.get("/v1/models", headers=auth).json()["object"] == "list"
        response = client.post("/v1/router/preview", headers=auth, json={"input": "Translate this"})
        assert response.json()["tier"] == "luna"
        assert response.json()["classifier_called"] is False
        assert client.get("/docs", headers=auth).status_code == 404
    assert not calls


@pytest.mark.parametrize("raw,status", [
    ('[]', 400), ('{bad', 400), ('{"x": NaN}', 400),
    ('{"input":123}', 400), ('{"stream":"true"}', 400), ('{"conversation":"c1"}', 400),
])
def test_invalid_bodies(config, auth, raw, status):
    with TestClient(create_app(config, transport=httpx.MockTransport(lambda request: httpx.Response(500)))) as client:
        response = client.post("/v1/responses", headers={**auth, "Content-Type": "application/json"}, content=raw)
        assert response.status_code == status


def test_body_limit_and_content_encoding(config, auth):
    config.server.max_body_bytes = 1024
    with TestClient(create_app(config, transport=httpx.MockTransport(lambda request: httpx.Response(500)))) as client:
        assert client.post("/v1/responses", headers=auth, json={"input": "x" * 1024}).status_code == 413
        assert client.post("/v1/responses", headers=auth, content="{}").status_code == 415
        assert client.post("/v1/responses", headers={**auth, "Content-Encoding": "gzip"}, json={}).status_code == 415


def test_missing_credentials(config, monkeypatch):
    monkeypatch.delenv("JEV_ROUTER_TOKEN")
    with pytest.raises(ValueError):
        create_app(config)


def test_identical_credentials(config, monkeypatch):
    monkeypatch.setenv("JEV_UPSTREAM_API_KEY", "test-gateway-token-not-for-production")
    with pytest.raises(ValueError):
        create_app(config)


def test_cli_route_no_credentials(monkeypatch, capsys):
    monkeypatch.delenv("JEV_ROUTER_TOKEN", raising=False)
    monkeypatch.delenv("JEV_UPSTREAM_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["jev-router", "route", "Find a deadlock"])
    main()
    assert '"tier": "sol"' in capsys.readouterr().out


def test_cli_doctor(config, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["jev-router", "doctor"])
    main()
    output = capsys.readouterr().out
    assert "OK" in output and "test-upstream-credential" not in output


def test_cli_missing_file(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["jev-router", "--config", "missing-file.toml", "doctor"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "failed" in capsys.readouterr().err
