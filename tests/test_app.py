"""The service as the container runs it: built from env, token-gated, its
manifest intact, nothing configurable over the network, the connection a
no-op, and failures mapped onto the broker's 503/502 contract."""

import sqlite3

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_finance import main
from aab_plugin_finance.adapter import MANIFEST_PATH
from aab_plugin_finance.store import Store

from .conftest import PLUGIN_TOKEN, data, items
from .fakes import CAL_TXNS, ingest, seed


@pytest.fixture()
def environ(tmp_path):
    return {"PLUGIN_TOKEN": PLUGIN_TOKEN, "PLUGIN_SECRETS_KEY": Fernet.generate_key().decode(),
            "PLUGIN_SECRETS_DIR": str(tmp_path / "secrets"),
            "FINANCE_DB": str(tmp_path / "finance.db")}


def runtime(environ, **kw) -> TestClient:
    return TestClient(main.create_app(environ), headers={"X-Plugin-Token": PLUGIN_TOKEN},
                      raise_server_exceptions=False, **kw)


def perform(c, action, params=None):
    return c.post("/perform", json={"action": action, "params": params or {}, "scope": {}})


def test_create_app_from_env(environ):
    c = runtime(environ)
    assert [m["id"] for m in c.get("/manifests").json()["manifests"]] == ["finance"]
    ingest(lambda a, p=None, s=None: perform(c, a, p), "cal", "1234", CAL_TXNS)
    assert len(perform(c, "list_transactions").json()["data"]["items"]) == 3


def test_build_adapter_uses_env_values(environ):
    a = main.build_adapter({**environ, "TZ": "Europe/London"})
    assert a.store.path == environ["FINANCE_DB"] and str(a.clock.tz) == "Europe/London"
    b = main.build_adapter({**environ, "TZ": "Europe/London", "FINANCE_TZ": "UTC"})
    assert str(b.clock.tz) == "UTC"


def test_defaults_are_the_descriptor_values():
    a = main.build_adapter({})
    assert a.store.path == "/data/finance.db" == main.DEFAULT_DB
    assert str(a.clock.tz) == "Asia/Jerusalem"


def test_an_unknown_time_zone_falls_back_to_the_default(environ):
    assert str(main.build_adapter({**environ, "TZ": "Mars/Olympus"}).clock.tz) == "Asia/Jerusalem"


def test_the_schema_is_created_at_boot(environ):
    main.build_adapter(environ)
    with sqlite3.connect(environ["FINANCE_DB"]) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert {"meta", "sources", "transactions", "notes", "note_history", "ingest_runs",
            "ingest_chunks", "refresh_requests"} <= tables
    assert mode == "wal"


def test_boot_refuses_without_a_token(environ):
    environ["PLUGIN_TOKEN"] = ""
    with pytest.raises(RuntimeError):
        main.create_app(environ)


@pytest.mark.parametrize("path", ["", "file:/data/x.db", "/data/x.db?mode=memory", "/x#y"])
def test_unsafe_database_paths_refuse_to_boot(path):
    with pytest.raises(ValueError):
        Store(path)


def test_every_endpoint_requires_the_plugin_token(environ):
    anon = TestClient(main.create_app(environ), raise_server_exceptions=False)
    for method, path in [("GET", "/manifests"), ("GET", "/status"), ("POST", "/perform"),
                         ("POST", "/configure"), ("POST", "/normalize"), ("POST", "/label"),
                         ("POST", "/resolve"), ("GET", "/connect/qr.png")]:
        assert anon.request(method, path, json={}).status_code == 401, path


def test_configure_cannot_redirect_the_plugin(client, adapter, db_path):
    r = client.post("/configure", json={"config": {"finance_db": "/etc/passwd"},
                                        "secrets": {"anything": "x"}})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert adapter.store.path == str(db_path)


def test_status(client, perform):
    seed(perform)
    s = client.get("/status").json()
    assert s["connected"] is True and s["healthy"] is True and s["enforcement"] == "proxy"
    assert (s["sources"], s["transactions"]) == (3, 7) and s["last_ingest_at"]
    assert s["connection"] == {"kind": "none", "connected": True}


def test_status_is_503_when_the_database_cannot_open(tmp_path):
    environ = {"PLUGIN_TOKEN": PLUGIN_TOKEN, "PLUGIN_SECRETS_KEY": Fernet.generate_key().decode(),
               "PLUGIN_SECRETS_DIR": str(tmp_path / "secrets"),
               "FINANCE_DB": str(tmp_path / "missing-volume" / "finance.db")}
    c = runtime(environ)                                  # boots anyway
    assert c.get("/status").status_code == 503
    assert perform(c, "list_sources").status_code == 503
    (tmp_path / "missing-volume").mkdir()                 # the volume appears
    assert c.get("/status").status_code == 200            # schema created lazily
    assert perform(c, "list_sources").json()["data"] == {"items": []}


def test_the_connection_is_a_no_op(client):
    assert client.post("/connect/start", json={}).json() == {"kind": "none"}
    assert client.get("/connect/qr.png").status_code == 404
    assert client.post("/disconnect").status_code == 409


def test_normalize_endpoint(client):
    def norm(kind, value):
        return client.post("/normalize", json={"kind": kind, "value": value})
    assert norm("card", "Cal 1234").json() == {"id": "cal:1234"}
    assert norm("account", "LEUMI-123456").json() == {"id": "leumi:123456"}
    assert norm("company", " Max ").json() == {"id": "max"}
    assert norm("transaction", "TX_0123456789ABCDEF").json() == {"id": "tx_0123456789abcdef"}
    assert norm("card", "cal1234").status_code == 400
    assert norm("wallet", "x").status_code == 400


def test_resolve_and_label(client, perform):
    seed(perform)
    found = client.post("/resolve", json={"kind": "card", "query": "cal"}).json()["items"]
    assert found == [{"id": "cal:1234", "label": "Cal 1234", "kind": "card"}]
    assert client.post("/resolve", json={"kind": "transaction", "query": "NETFLIX"}).json() == {
        "items": []}
    tx = items(perform("list_transactions", {"source": "cal:1234"}))[0]
    labels = client.post("/label", json={"kind": "transaction", "ids": [tx["id"]]}).json()
    assert labels["labels"][tx["id"]] == f"{tx['date']} Cal 1234"      # no merchant, no amount
    assert client.post("/label", json={"kind": "company", "ids": ["cal", "leumi"]}).json() == {
        "labels": {"cal": "Cal", "leumi": "leumi"}}
    assert client.post("/label", json={"kind": "card", "ids": ["cal:1234", "nope:1"]}).json() \
        == {"labels": {"cal:1234": "Cal 1234"}}


def test_an_unknown_action_is_404_and_bad_params_400(perform):
    assert perform("delete_everything").status_code == 404
    assert perform("list_transactions", {"limit": "ten"}).status_code == 400
    assert perform("list_transactions", {"limit": True}).status_code == 400
    assert perform("list_transactions", {"status": "settled"}).status_code == 400
    assert perform("list_transactions", {"month": "2026-13"}).status_code == 400
    assert perform("list_transactions", {"month": "2026-09", "start": "2026-09-01"}
                   ).status_code == 400
    assert perform("list_transactions", {"cursor": "garbage"}).status_code == 400
    assert perform("list_transactions", {"min_x100": 10 ** 30}).status_code == 400


def test_a_store_failure_is_503(perform, adapter, monkeypatch):
    seed(perform)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr("aab_plugin_finance.store.transactions_page", locked)
    r = perform("list_transactions")
    assert r.status_code == 503 and r.json() == {"error": "finance store temporarily unavailable"}


def test_an_unexpected_failure_is_502_without_internals(perform, monkeypatch):
    seed(perform)

    def boom(*a, **k):
        raise RuntimeError("secret-internal-detail")
    monkeypatch.setattr("aab_plugin_finance.store.transactions_page", boom)
    r = perform("list_transactions")
    assert r.status_code == 502 and "secret-internal-detail" not in r.text


def test_keyset_paging_walks_every_row_once(perform):
    seed(perform)
    seen, cursor = [], None
    while True:
        out = data(perform("list_transactions", {"limit": 2, **({"cursor": cursor}
                                                                if cursor else {})}))
        seen += [t["id"] for t in out["items"]]
        cursor = out["next_cursor"]
        if cursor is None:
            break
    everything = [t["id"] for t in items(perform("list_transactions", {"limit": 500}))]
    assert seen == everything and len(set(seen)) == 7


def test_list_filters(perform):
    seed(perform)
    def descs(**params):
        return {t["description"] for t in items(perform("list_transactions", params))}
    assert descs(month="2026-09") == {"SUPER-PHARM", "NETFLIX", "העברה לחשבון"}
    assert descs(kind="refunds") == {"REFUND SHOP"}
    assert descs(kind="transfers") == {"העברה לחשבון"}
    assert descs(min_x100=50000) == {"SECRET CLINIC", "העברה לחשבון"}
    assert descs(source="max:5678", kind="purchases") == {"AMAZON MKTPLACE", "SECRET CLINIC"}
    assert descs(start="2026-10-01", end="2026-10-31") == {"REFUND SHOP", "SECRET CLINIC"}


def test_amounts_and_shape(perform):
    seed(perform)
    amazon = next(t for t in items(perform("list_transactions"))
                  if t["description"] == "AMAZON MKTPLACE")
    assert (amazon["original_amount"], amazon["original_currency"], amazon["charged_amount"],
            amazon["charged_currency"]) == (-55.0, "USD", -200.0, "ILS")
    assert amazon["installment"] is None and amazon["source"] == {"kind": "card",
                                                                  "id": "max:5678"}
    assert set(amazon) == {"id", "source", "company", "date", "processed_date", "description",
                           "category", "original_amount", "original_currency",
                           "charged_amount", "charged_currency", "type", "installment",
                           "status", "identifier", "memo", "note", "first_seen", "last_seen",
                           "resource_ref"}


def test_snapshot_info(perform):
    seed(perform)
    info = data(perform("snapshot_info"))
    assert info["today"] == "2026-10-06" and info["transactions"] == 7
    assert (info["first_date"], info["last_date"]) == ("2025-01-05", "2026-10-02")
    assert [c["company"] for c in info["companies"]] == ["cal", "leumi", "max"]
    assert info["last_ingest_at"] == "2026-10-06T09:00:00Z" and info["pending_refreshes"] == 0


def test_the_manifest_is_the_packaged_file(client):
    served = client.get("/manifests").json()["manifests"][0]
    assert served == yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
