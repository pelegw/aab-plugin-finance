"""tools/replay_raw.py: the normalize.js port, and the upload protocol against
a fake broker (chunking, request ids, retries on 503/429, resends on 502,
stop on 4xx), end to end into the real plugin, never printing row content."""

import http.server
import io
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aab_plugin_runtime import serve
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_finance.adapter import FinanceAdapter
from aab_plugin_finance.clock import Clock
from aab_plugin_finance.store import Store
from tools import replay_raw as rr

from .conftest import PLUGIN_TOKEN

KEY = "aab_" + "ab" * 24
JERUSALEM = ZoneInfo("Asia/Jerusalem")


# ---- the JavaScript port -----------------------------------------------------------------

@pytest.mark.parametrize("value,text", [
    (-123.4, "-123.40"), (1.005, "1.00"),       # 1.005 is 1.00499999... in binary
    (0.125, "0.13"), (-0.125, "-0.13"),          # exact ties go away from zero
    (0, "0.00"), (-0.0, "0.00"), (-0.001, "-0.00"), (5, "5.00")])
def test_to_fixed2_is_javascripts(value, text):
    assert rr.to_fixed2(value) == text


@pytest.mark.parametrize("value,rounded", [(2.5, 3), (-2.5, -2), (-2.6, -3), (123.45 * 100, 12345),
                                           (-19.9 * 100, -1990), (0.0, 0)])
def test_js_round_is_math_round(value, rounded):
    assert rr.js_round(value) == rounded


def test_small_ports():
    assert rr.last4("4580-1234-5678-9012") == "9012" and rr.last4(None) == "unknown"
    assert rr.last4("") == "" and rr.last4(1234) == "1234"
    assert rr.normalize_currency(" € ") == "EUR" and rr.normalize_currency("nis") == "ILS"
    assert rr.normalize_currency("ש״ח") == "ILS" and rr.normalize_currency(None) == ""
    assert rr.normalize_currency("usd") == "USD"
    assert rr.iso_to_local_ymd("2026-03-03T22:30:00.000Z", JERUSALEM) == "2026-03-04"
    assert rr.iso_to_local_ymd("2026-03-03", JERUSALEM) == "2026-03-03"
    assert rr.iso_to_local_ymd("garbage-value", JERUSALEM) == "garbage-va"
    assert rr.num("12.5") == 12.5 and rr.num(None) == 0 and rr.num("x") == 0
    assert rr.num(float("nan")) == 0


def account() -> dict:
    """Edge cases for the key: whitespace, ties, duplicates, nulls, numbers."""
    return {"accountNumber": "4580-0000-0000-1234", "txns": [
        {"date": "2026-03-03T22:30:00.000Z", "processedDate": "2026-03-10T00:00:00.000Z",
         "originalAmount": -123.4, "originalCurrency": "₪", "chargedAmount": -123.4,
         "chargedCurrency": None, "description": "  Super  Pharm ", "memo": None,
         "category": None, "identifier": 9981, "status": "completed", "type": "normal",
         "installments": None},
        {"date": "2026-03-04T00:00:00.000Z", "processedDate": "", "originalAmount": 0.125,
         "originalCurrency": "€", "chargedAmount": 0.5, "chargedCurrency": "ILS",
         "description": "REFUND", "identifier": None, "installments": None},
        {"date": "2026-03-04T00:00:00.000Z", "processedDate": "", "originalAmount": 0.125,
         "originalCurrency": "€", "chargedAmount": 0.5, "chargedCurrency": "ILS",
         "description": "refund", "identifier": None, "installments": None},
        {"date": "2026-04-01T00:00:00.000Z", "originalAmount": -1200, "originalCurrency": "ILS",
         "chargedAmount": -400.0, "description": "TV", "type": "installments",
         "installments": {"number": 2, "total": 3}},
        {"date": "2025-12-31T21:00:00.000Z", "originalAmount": -1, "description": "OUT OF RANGE"},
    ]}


def test_normalize_account_keys():
    card, rows = rr.normalize_account("cal", account(), {"start": "2026-01-01",
                                                         "end": "2026-12-31"}, JERUSALEM)
    assert card == "1234" and len(rows) == 4                  # out-of-range row dropped
    assert rows[0]["key"] == "cal|1234|9981|2026-03-04|-123.40|ILS|super pharm|#1"
    assert rows[0]["description"] == "Super Pharm" and rows[0]["chargedCurrency"] == "ILS"
    # Same base key (descriptions differ only in case): the occurrence index separates them.
    assert rows[1]["key"] == "cal|1234||2026-03-04|0.13|EUR|refund|#1"
    assert rows[2]["key"] == "cal|1234||2026-03-04|0.13|EUR|refund|#2"
    assert rows[3]["key"] == "cal|1234||2026-04-01|-1200.00|ILS|tv|2#1"


def test_to_ingest_row():
    _, rows = rr.normalize_account("cal", account(), None, JERUSALEM)
    out = [rr.to_ingest_row(r) for r in rows]
    assert out[0]["original_x100"] == -12340 and out[0]["original_currency"] == "ILS"
    assert "installment_number" not in out[0]                        # nulls omitted
    assert (out[3]["installment_number"], out[3]["installment_total"]) == (2, 3)
    assert out[1]["original_x100"] == 13 and out[1]["charged_x100"] == 50
    assert all(rr.row_problem(r) is None for r in out)
    assert rr.row_problem({**out[0], "original_currency": "EURO"}) == "original_currency length"
    assert rr.row_problem({**out[0], "status": "authorized"}) == "status"


def _cred_analysis() -> Path | None:
    root = Path(os.environ.get("CRED_ANALYSIS_SRC")
                or Path(__file__).resolve().parents[2] / "cred-analysis")
    ok = (root / "src" / "normalize.js").is_file() and (root / "node_modules").is_dir()
    return root if ok and shutil.which("node") else None


@pytest.mark.skipif(_cred_analysis() is None, reason="needs node and a cred-analysis checkout")
def test_the_port_matches_normalize_js(tmp_path):
    root = _cred_analysis()
    (tmp_path / "account.json").write_text(json.dumps(account()), encoding="utf-8")
    script = ("import fs from 'node:fs';"
              f"const {{ normalizeAccount }} = await import("
              f"{json.dumps((root / 'src' / 'normalize.js').as_uri())});"
              "const a = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));"
              "console.log(JSON.stringify(normalizeAccount('cal', a,"
              " {start: '2026-01-01', end: '2026-12-31'})));")
    out = subprocess.run(["node", "--input-type=module", "-e", script,
                          str(tmp_path / "account.json")], capture_output=True, text=True,
                         encoding="utf-8", timeout=120, cwd=root,
                         env={**os.environ, "TZ": "Asia/Jerusalem"})
    assert out.returncode == 0, out.stderr[-2000:]
    js = json.loads(out.stdout.strip().splitlines()[-1])
    card, rows = rr.normalize_account("cal", account(), {"start": "2026-01-01",
                                                         "end": "2026-12-31"}, JERUSALEM)
    assert js["card"] == card
    assert js["rows"] == rows


# ---- a fake broker ---------------------------------------------------------------------------

class FakeBroker:
    """Answers POSTs from a script (status, body, headers) or "drop" (close
    the connection without an answer); then from `forward(params)`."""

    def __init__(self, forward=None):
        self.script: list = []
        self.requests: list[tuple[dict, bytes]] = []
        self.forward = forward or (lambda params: (200, {"status": "applied", "added": 0,
                                                         "updated": 0, "unchanged": 0,
                                                         "removed_pending": 0}))
        broker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                broker.requests.append((dict(self.headers), body))
                step = broker.script.pop(0) if broker.script else None
                if step == "drop":
                    self.close_connection = True
                    return
                if step is None:
                    status, payload = broker.forward(json.loads(body)["params"])
                    headers = {}
                else:
                    status, payload, headers = (list(step) + [{}])[:3]
                raw = json.dumps(payload).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def bodies(self) -> list[dict]:
        return [json.loads(b) for _, b in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def broker():
    b = FakeBroker()
    yield b
    b.close()


def run_with(rows: int) -> rr.Run:
    return rr.Run("cal-test.json", "cal", "cal:1234", "Cal 1234", "2026-01-01", "2026-12-31",
                  "2026-10-05T08:00:00.000Z",
                  [{"key": f"k{i}", "date": "2026-09-01", "description": "D", "category": "",
                    "original_x100": -100, "original_currency": "ILS", "charged_x100": -100,
                    "charged_currency": "ILS", "type": "normal", "status": "completed",
                    "identifier": "", "memo": "", "processed_date": ""} for i in range(rows)],
                  run_id="0f8fad5b-d9cb-469f-a165-70867728950e")


def client(broker, delays, **kw) -> rr.Client:
    return rr.Client(broker.url, KEY, sleep=delays.append, **kw)


def test_chunks_of_400_in_order_with_request_ids(broker):
    answers = iter([(200, {"status": "staged"}),
                    (200, {"status": "applied", "added": 450, "updated": 0, "unchanged": 0,
                           "removed_pending": 0})])
    broker.forward = lambda params: next(answers)
    out = rr.upload_run(client(broker, []), run_with(450))
    assert out["added"] == 450
    sent = broker.bodies()
    assert [len(b["params"]["transactions"]) for b in sent] == [400, 50]
    assert [b["params"]["chunk"] for b in sent] == [{"index": 1, "total": 2},
                                                    {"index": 2, "total": 2}]
    headers = [h for h, _ in broker.requests]
    assert [h["X-Request-Id"] for h in headers] == [
        "0f8fad5b-d9cb-469f-a165-70867728950e-1", "0f8fad5b-d9cb-469f-a165-70867728950e-2"]
    assert all(h["Authorization"] == f"Bearer {KEY}" for h in headers)
    assert sent[0]["params"]["source"] == {"kind": "card", "id": "cal:1234", "label": "Cal 1234",
                                           "last4": "1234"}


def test_503_and_429_wait_and_retry_honouring_retry_after(broker):
    delays = []
    broker.script = [(503, {"error": "x"}), (429, {"error": "slow"}, {"Retry-After": "3"})]
    rr.upload_run(client(broker, delays), run_with(1))
    assert delays == [1, 3.0] and len(broker.requests) == 3
    assert len({b for _, b in broker.requests}) == 1          # the same chunk each time


def test_502_resends_the_same_chunk(broker):
    broker.script = [(502, {"error": "unknown"}), (504, {"error": "gateway"})]
    rr.upload_run(client(broker, []), run_with(2))
    assert len(broker.requests) == 3 and len({b for _, b in broker.requests}) == 1


def test_a_dropped_connection_is_retried(broker):
    broker.script = ["drop"]
    rr.upload_run(client(broker, []), run_with(1))
    assert len(broker.requests) == 2


def test_a_4xx_stops_at_once(broker):
    broker.script = [(400, {"error": "transactions[0]: date must be inside the range",
                            "code": "invalid_params"})]
    with pytest.raises(rr.UploadError) as e:
        rr.upload_run(client(broker, []), run_with(3))
    assert e.value.status == 400 and e.value.detail.startswith("invalid_params")
    assert len(broker.requests) == 1


def test_it_gives_up_after_the_last_attempt(broker):
    delays = []
    broker.script = [(503, {})] * 3
    with pytest.raises(rr.UploadError) as e:
        rr.upload_run(client(broker, delays, attempts=3), run_with(1))
    assert e.value.status is None and delays == [1, 2] and len(broker.requests) == 3


def test_the_key_is_not_in_the_clients_repr(broker):
    assert KEY not in repr(client(broker, []))


# ---- end to end into the real plugin -----------------------------------------------------------

def snapshot(company, scraped_at, accounts, end="2026-12-31") -> dict:
    return {"company": company, "scrapedAt": scraped_at,
            "range": {"start": "2026-01-01", "end": end, "label": "2026"}, "accounts": accounts}


SECRET = "PRIVATE-MERCHANT-3e1f"


@pytest.fixture()
def raw_dir(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    cal = snapshot("cal", "2026-10-05T08:00:00.000Z", [
        {"accountNumber": "4580000000001234", "txns": [
            {"date": "2026-09-0%dT21:00:00.000Z" % d, "originalAmount": -7654.32,
             "originalCurrency": "ILS", "chargedAmount": -7654.32, "chargedCurrency": "ILS",
             "description": SECRET, "status": "completed"} for d in range(1, 6)]},
        {"accountNumber": "", "txns": []}])                       # no card number
    later = snapshot("cal", "2026-10-06T08:00:00.000Z", [
        {"accountNumber": "4580000000001234", "txns": [
            {"date": "2026-09-01T21:00:00.000Z", "originalAmount": -7654.32,
             "originalCurrency": "ILS", "chargedAmount": -7654.32, "chargedCurrency": "ILS",
             "description": SECRET, "status": "completed"}]}], end="2026-10-06")
    (raw / "cal-b.json").write_text(json.dumps(later), encoding="utf-8")
    (raw / "cal-a.json").write_text(json.dumps(cal), encoding="utf-8")
    (raw / "notes.txt").write_text("ignored", encoding="utf-8")
    (raw / "other.json").write_text(json.dumps({"company": "visa", "accounts": []}),
                                    encoding="utf-8")
    return raw


@pytest.fixture()
def plugin_broker(tmp_path, fake_now):
    """A fake broker in front of the real plugin (served by the runtime)."""
    adapter = FinanceAdapter(Store(str(tmp_path / "e2e.db")), Clock("Asia/Jerusalem", fake_now))
    app = TestClient(serve([adapter], PLUGIN_TOKEN, tmp_path / "e2e-secrets",
                           Fernet.generate_key().decode(), service="finance"),
                     headers={"X-Plugin-Token": PLUGIN_TOKEN})

    def forward(params):
        r = app.post("/perform", json={"action": "ingest_snapshot", "params": params,
                                       "scope": {}})
        return r.status_code, r.json().get("data", r.json())

    b = FakeBroker(forward)
    b.app = app
    yield b
    b.close()


def test_main_end_to_end(raw_dir, plugin_broker, monkeypatch):
    monkeypatch.setenv("AAB_KEY", KEY)
    out = io.StringIO()
    argv = ["--raw", str(raw_dir), "--url", plugin_broker.url, "--tz", "Asia/Jerusalem"]
    assert rr.main(argv, out=out) == 0
    text = out.getvalue()
    assert "cal-a.json: account #2 has no card number; skipped" in text
    assert "cal-a.json: cal:1234 run " in text and "applied added=5" in text
    # cal-b re-sends one row and its range ends 2026-10-06: one unchanged.
    assert "cal-b.json: cal:1234 run " in text and "unchanged=1" in text
    assert text.strip().endswith("2 runs applied: added=5 updated=0 unchanged=1 removed_pending=0")
    for secret in (SECRET, "7654.32", "765432", KEY, "cal|1234|"):
        assert secret not in text
    listed = plugin_broker.app.post("/perform", json={"action": "list_transactions",
                                                      "params": {}, "scope": {}}).json()
    assert len(listed["data"]["items"]) == 5
    # Sent oldest scrape first, whatever the file names say.
    sent = [b["params"]["scraped_at"] for b in plugin_broker.bodies()]
    assert sent == ["2026-10-05T08:00:00.000Z", "2026-10-06T08:00:00.000Z"]
    # Replaying again changes nothing.
    again = io.StringIO()
    assert rr.main(argv, out=again) == 0
    assert "added=0 updated=0 unchanged=6" in again.getvalue()


def test_main_stops_on_a_refusal(raw_dir, broker, monkeypatch):
    monkeypatch.setenv("AAB_KEY", KEY)
    broker.script = [(403, {"error": "not covered by any of your grants", "code": "out_of_grant"})]
    out = io.StringIO()
    assert rr.main(["--raw", str(raw_dir), "--url", broker.url], out=out) == 1
    assert "stopped, HTTP 403 out_of_grant" in out.getvalue() and len(broker.requests) == 1


def test_dry_run_sends_nothing(raw_dir):
    out = io.StringIO()
    assert rr.main(["--raw", str(raw_dir), "--dry-run", "--tz", "Asia/Jerusalem"], out=out) == 0
    assert "2 runs planned, nothing sent" in out.getvalue() and SECRET not in out.getvalue()


@pytest.mark.parametrize("url", ["http://aab.example.com", "ftp://x"])
def test_plain_http_is_refused_off_loopback(raw_dir, monkeypatch, url):
    monkeypatch.setenv("AAB_KEY", KEY)
    with pytest.raises(SystemExit):
        rr.main(["--raw", str(raw_dir), "--url", url])


def test_a_key_is_required(raw_dir, broker, monkeypatch):
    monkeypatch.delenv("AAB_KEY", raising=False)
    with pytest.raises(SystemExit):
        rr.main(["--raw", str(raw_dir), "--url", broker.url])
    assert broker.requests == []


def test_the_key_can_come_from_a_file(raw_dir, broker, tmp_path, monkeypatch):
    monkeypatch.delenv("AAB_KEY", raising=False)
    (tmp_path / "key").write_text(KEY + "\n", encoding="utf-8")
    assert rr.main(["--raw", str(raw_dir), "--url", broker.url, "--key-file",
                    str(tmp_path / "key")], out=io.StringIO()) == 0
    assert broker.requests[0][0]["Authorization"] == f"Bearer {KEY}"
