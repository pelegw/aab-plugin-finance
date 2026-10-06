"""The finance plugin inside the gateway's broker (needs a gateway checkout).

The broker comes from AAB_SRC (default ../agent-authority-broker; see
tests/gateway.py). Without one these tests are skipped, unless
AAB_REQUIRE_GATEWAY=1 (CI), where a missing gateway is an error.

The fixtures re-implement the few the gateway's broker/tests/conftest.py
provides (`env`, `owner`, `admin_headers`, `make_agent`, `enable_plugin`,
`runtime_factory`) instead of importing them: that module is the gateway's
`tests` package, which would collide with this repository's own `tests`
package. They use the same broker APIs, so they track the gateway's.

Registration uses the gateway's test seam: the registry is built with
`Registry(vendored_dirs=(<tmp>,))` where <tmp>/finance/manifest.yaml is this
plugin's manifest (standing in for the owner's pin), and the adapter is
registered either in process (`register_in_process`) or over the plugin API
(`discover` with the runtime app behind a TestClient, as production does
through RemoteAdapter). Every test using `finance` runs on both transports.
"""

import os
import shutil
import types

import pytest

from ..gateway import import_broker

broker, WHY = import_broker()
if broker is None and os.environ.get("AAB_REQUIRE_GATEWAY") == "1":
    raise RuntimeError(f"AAB_REQUIRE_GATEWAY=1 but {WHY}")

OWNER_USERNAME = "owner"
OWNER_PASSWORD = "correct horse battery staple"
FINANCE_TOKEN = "finance-plugin-token-for-broker-tests-0123"
# Exposure settings a developer's shell might carry (the gateway conftest's list).
_EXPOSURE_VARS = (
    "ORIGIN_SECRET", "ORIGIN_SECRET_HEADER", "CF_ACCESS_ENABLED", "CF_ACCESS_TEAM_DOMAIN",
    "CF_ACCESS_AUD", "CF_ACCESS_ALLOWED_EMAILS", "ALLOW_INSECURE_ADMIN",
    "TRUST_CF_CONNECTING_IP", "DECISION_SIGNING_KEY", "BROKER_SECRETS_KEY",
)


def pytest_collection_modifyitems(config, items):
    if broker is not None:
        return
    here = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.skip(reason=WHY))


@pytest.fixture(scope="session")
def vendored_dir(tmp_path_factory):
    """<tmp>/finance/manifest.yaml: the pinned copy the registry checks against."""
    from aab_plugin_finance.adapter import MANIFEST_PATH
    root = tmp_path_factory.mktemp("vendored")
    (root / "finance").mkdir()
    shutil.copyfile(MANIFEST_PATH, root / "finance" / "manifest.yaml")
    return root


@pytest.fixture()
def env(tmp_path, monkeypatch, vendored_dir):
    """A fresh broker database, clean settings, a registry pinned to this
    plugin's manifest, and no process-global state from earlier tests."""
    for var in _EXPOSURE_VARS:
        monkeypatch.delenv(var, raising=False)
    for var in list(os.environ):
        if var.startswith(("PLUGIN_URL_", "PLUGIN_TOKEN_")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BROKER_DB", str(tmp_path / "broker.db"))
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,testserver")
    from broker import db, ledger, notify
    from broker.config import get_settings
    from broker.plugins import registry
    get_settings.cache_clear()
    db.init()
    registry.reset_registry(registry.Registry(vendored_dirs=(vendored_dir,)))
    ledger.rate_limiter.reset()
    monkeypatch.setattr(notify, "_PROVIDERS", [])
    yield
    registry.reset_registry()
    get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from broker.main import app
    return TestClient(app)


@pytest.fixture()
def live(env):
    """A TestClient with the app lifespan running (MCP serves only inside it)."""
    from fastapi.testclient import TestClient

    from broker.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def owner(env):
    from broker.identity import principals
    p = principals.create_owner(OWNER_USERNAME, OWNER_PASSWORD)
    return types.SimpleNamespace(id=p.id, username=p.username)


@pytest.fixture()
def admin_headers(owner):
    from broker.identity import admin_tokens
    return {"Authorization": f"Bearer {admin_tokens.create(owner.id, 'test')['token']}"}


def enable_plugin(plugin_id: str = "finance") -> None:
    from broker.plugins import settings
    settings.set_enabled(plugin_id, True)
    settings.set_health(plugin_id, {"connected": True, "healthy": True}, True)


def runtime_factory(runtime_app):
    """A RemoteAdapter client factory talking to the runtime app in process."""
    from fastapi.testclient import TestClient

    def factory(base_url, headers, timeout):
        return TestClient(runtime_app, base_url=base_url, headers=headers,
                          raise_server_exceptions=False)
    return factory


def serve_finance(adapter, tmp_path):
    from aab_plugin_runtime import serve
    from cryptography.fernet import Fernet
    return serve([adapter], FINANCE_TOKEN, tmp_path / "finance-secrets",
                 Fernet.generate_key().decode(), service="finance")


def discover(runtime):
    from broker.plugins.registry import get_registry
    get_registry().discover({"finance": ("http://plugin-finance:8090", FINANCE_TOKEN)},
                            client_factory=runtime_factory(runtime))


@pytest.fixture(params=["inprocess", "remote"])
def finance(request, env, owner, tmp_path, fake_now):
    """The real finance adapter on a fresh database, registered (pinned) and
    enabled, over BOTH transports."""
    from aab_plugin_finance.adapter import FinanceAdapter
    from aab_plugin_finance.clock import Clock
    from aab_plugin_finance.store import Store
    from broker.plugins.registry import get_registry

    adapter = FinanceAdapter(Store(str(tmp_path / "finance.db")),
                             Clock("Asia/Jerusalem", fake_now))
    reg = get_registry()
    if request.param == "inprocess":
        assert reg.register_in_process(adapter, reg.vendored("finance"), service="finance")
    else:
        discover(serve_finance(adapter, tmp_path))
        assert "finance" in reg.entries(), reg.refused
    enable_plugin()
    return types.SimpleNamespace(adapter=adapter, transport=request.param)


@pytest.fixture()
def make_agent(owner):
    """make_agent(caps, role="full", rate=60, denies=None) -> namespace(key_id,
    plaintext, headers, auth, grant_id); `caps` become an active root grant."""
    from broker import auth
    from broker.authority import store
    from broker.authority.capability import from_json, normalize_all
    from broker.plugins.registry import get_registry

    counter = iter(range(1, 10_000))

    def make(caps=None, role="full", rate=60, denies=None, name=None):
        new = auth.create_key(owner.id, name or f"agent-{next(counter)}", role, rate, None,
                              denies=denies)
        grant_id = None
        if caps:
            normalized = normalize_all([from_json(c) for c in caps], get_registry().manifests())
            grant_id = store.insert_root_grant(owner.id, new.key_id, normalized, "active",
                                               "test", None, owner.username,
                                               decided_via="token").id
        return types.SimpleNamespace(
            key_id=new.key_id, plaintext=new.plaintext, grant_id=grant_id,
            headers={"Authorization": f"Bearer {new.plaintext}"},
            auth=auth.authenticate_bearer(f"Bearer {new.plaintext}"))
    return make


# finance-plugin-plan.md section 2: the scraper (A) and the analysis agent (B).
SCRAPER_CAPS = [{"target": "finance", "mode": "direct", "budget": {"per_day": 2000},
                 "actions": ["ingest_snapshot", "report_refresh", "list_refresh_requests",
                             "list_runs"]}]
ANALYST_CAPS = [{"target": "finance", "actions": ["read_*", "set_note"], "mode": "direct"},
                {"target": "finance", "actions": ["request_refresh"], "mode": "draft",
                 "budget": {"per_day": 3}}]


@pytest.fixture()
def key_a(finance, make_agent):
    return make_agent(SCRAPER_CAPS, role="read-act", name="mac-mini-scraper")


@pytest.fixture()
def key_b(finance, make_agent):
    return make_agent(ANALYST_CAPS, role="read-act", name="analysis-agent")
