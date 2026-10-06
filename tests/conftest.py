"""Fixtures for the finance plugin: a fresh database, a fake clock, the
adapter, and the adapter served by the real plugin runtime.

Mirrors the gateway's plugins/whatsapp/tests/conftest.py: most tests go
through the runtime over `TestClient` (`perform(...)`), so the JSON scope,
the error mapping and the token check are the ones the broker actually sees.
"""

from datetime import datetime, timezone

import pytest
from aab_plugin_runtime import logging_setup, serve
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_finance.adapter import FinanceAdapter
from aab_plugin_finance.clock import Clock
from aab_plugin_finance.store import Store

# Logging is configured once, at collection, as the plugin process does when
# it builds its app: serve() inside a test then leaves the handlers alone
# instead of swapping them under a running log capture.
logging_setup.configure("plugin-finance")

PLUGIN_TOKEN = "finance-plugin-token-0123456789abcdef"
# 2026-10-06 12:00 in Jerusalem (09:00 UTC): "today" for every test.
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc).timestamp()


class FakeClock:
    """A settable epoch; `advance(seconds)` moves time forward."""

    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def fake_now():
    return FakeClock()


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "finance.db"


@pytest.fixture()
def store(db_path):
    return Store(str(db_path))


@pytest.fixture()
def adapter(store, fake_now):
    return FinanceAdapter(store, Clock("Asia/Jerusalem", fake_now))


@pytest.fixture()
def app(adapter, tmp_path):
    return serve([adapter], PLUGIN_TOKEN, tmp_path / "secrets", Fernet.generate_key().decode(),
                 service="finance")


@pytest.fixture()
def client(app):
    # raise_server_exceptions=False: a 5xx must come back as a response, as
    # it would over the network, so the error mapping can be asserted.
    return TestClient(app, headers={"X-Plugin-Token": PLUGIN_TOKEN},
                      raise_server_exceptions=False)


def scope(*, card_deny=(), card_allow=None, account_deny=(), account_allow=None,
          tx_deny=(), company_deny=(), company_allow=None, request_id="test-request",
          **constraints) -> dict:
    """A CallScope as the broker sends it. Only restricted kinds appear,
    as in policy._visibility."""
    vis = {}
    for kind, deny, allow in (("card", card_deny, card_allow),
                              ("account", account_deny, account_allow),
                              ("transaction", tx_deny, None),
                              ("company", company_deny, company_allow)):
        if deny or allow is not None:
            vis[kind] = {"deny": sorted(deny), "allow_only": None if allow is None
                         else sorted(allow)}
    return {"request_id": request_id, "visibility": vis, "constraints": constraints,
            "credential": {}}


@pytest.fixture()
def perform(client):
    """perform(action, params, call_scope=None) -> the runtime's HTTP response."""
    def _perform(action, params=None, call_scope=None):
        return client.post("/perform", json={"action": action, "params": params or {},
                                             "scope": call_scope or scope()})
    return _perform


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def items(response) -> list[dict]:
    return data(response)["items"]
