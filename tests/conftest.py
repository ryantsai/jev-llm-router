import pytest

from jev_llm_router.config import Config


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_ROUTER_TOKEN", "test-gateway-token-not-for-production")
    monkeypatch.setenv("JEV_UPSTREAM_API_KEY", "test-upstream-credential")
    cfg = Config()
    cfg.server.state_path = str(tmp_path / "state.sqlite3")
    return cfg


@pytest.fixture
def auth():
    return {"Authorization": "Bearer test-gateway-token-not-for-production"}
