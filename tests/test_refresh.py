"""Refresh requests: approved -> claimed once -> completed | failed; the
7-day expiry; unknown ids; validation."""

import re

import pytest

from .conftest import data, items, scope

DAY = 86400


def request(perform, **params):
    body = {"company": "all", "start": "2026-10-01", "end": "2026-10-31",
            "reason": "October review", **params}
    return data(perform("request_refresh", body))


def test_a_delivered_request_is_approved(perform):
    out = request(perform)
    assert re.fullmatch(r"rr_[0-9a-f]{16}", out["refresh_id"])
    assert out["status"] == "approved" and out["expires_at"] == "2026-10-13T09:00:00Z"
    [listed] = items(perform("list_refresh_requests"))           # default: approved
    assert listed["refresh_id"] == out["refresh_id"] and listed["reason"] == "October review"
    assert (listed["start"], listed["end"], listed["company"]) == ("2026-10-01", "2026-10-31",
                                                                   "all")


def test_claim_once_then_complete(perform):
    rid = request(perform)["refresh_id"]
    claimed = data(perform("report_refresh", {"refresh_id": rid, "status": "running"}))
    assert claimed["status"] == "running" and claimed["claimed_at"]
    again = perform("report_refresh", {"refresh_id": rid, "status": "running"})
    assert again.status_code == 409 and "running" in again.json()["error"]
    assert items(perform("list_refresh_requests")) == []            # no longer approved
    done = data(perform("report_refresh", {"refresh_id": rid, "status": "completed",
                                           "message": "3 companies",
                                           "run_ids": ["run-0000000001", "run-0000000002"]}))
    assert done["status"] == "completed" and done["run_ids"] == ["run-0000000001",
                                                                 "run-0000000002"]
    [listed] = items(perform("list_refresh_requests", {"status": "all"}))
    assert listed["status"] == "completed" and listed["message"] == "3 companies"
    late = perform("report_refresh", {"refresh_id": rid, "status": "failed"})
    assert late.status_code == 409


def test_a_failed_run(perform):
    rid = request(perform)["refresh_id"]
    perform("report_refresh", {"refresh_id": rid, "status": "running"})
    out = data(perform("report_refresh", {"refresh_id": rid, "status": "failed",
                                          "message": "one-time code timed out"}))
    assert out["status"] == "failed" and out["finished_at"]


def test_finishing_without_a_claim_is_409(perform):
    rid = request(perform)["refresh_id"]
    assert perform("report_refresh", {"refresh_id": rid, "status": "completed"}).status_code \
        == 409


def test_an_unknown_id_is_404(perform):
    assert perform("report_refresh", {"refresh_id": "rr_0000000000000000",
                                      "status": "running"}).status_code == 404


def test_approved_requests_expire_after_seven_days(perform, fake_now):
    rid = request(perform)["refresh_id"]
    fake_now.advance(7 * DAY + 1)
    assert items(perform("list_refresh_requests")) == []
    [expired] = items(perform("list_refresh_requests", {"status": "expired"}))
    assert expired["refresh_id"] == rid
    r = perform("report_refresh", {"refresh_id": rid, "status": "running"})
    assert r.status_code == 409 and "expired" in r.json()["error"]


def test_a_running_request_does_not_expire(perform, fake_now):
    rid = request(perform)["refresh_id"]
    perform("report_refresh", {"refresh_id": rid, "status": "running"})
    fake_now.advance(30 * DAY)
    assert items(perform("list_refresh_requests", {"status": "running"}))[0]["refresh_id"] == rid


def test_snapshot_info_counts_open_requests(perform, fake_now):
    request(perform)
    rid = request(perform)["refresh_id"]
    perform("report_refresh", {"refresh_id": rid, "status": "running"})
    assert data(perform("snapshot_info"))["pending_refreshes"] == 2
    fake_now.advance(8 * DAY)
    assert data(perform("snapshot_info"))["pending_refreshes"] == 1       # one expired


def test_filter_by_id_and_limit(perform):
    first = request(perform)["refresh_id"]
    request(perform)
    assert [r["refresh_id"] for r in items(perform("list_refresh_requests",
                                                   {"refresh_id": first}))] == [first]
    assert len(items(perform("list_refresh_requests", {"limit": 1}))) == 1


def test_company_requests_answer_to_the_company_visibility(perform):
    request(perform, company="max")
    request(perform, company="all")
    seen = items(perform("list_refresh_requests", {}, scope(company_deny=["max"])))
    assert [r["company"] for r in seen] == ["all"]


@pytest.mark.parametrize("params", [
    {"start": "2026-10-31", "end": "2026-10-01"},
    {"company": "visa"},
    {"reason": ""},
    {"reason": "   "},
    {"start": "2026-13-01"},
])
def test_request_validation(perform, params):
    body = {"company": "all", "start": "2026-10-01", "end": "2026-10-31", "reason": "r",
            **params}
    assert perform("request_refresh", body).status_code == 400


@pytest.mark.parametrize("params", [
    {"status": "approved"},
    {"run_ids": "run-0000000001"},
    {"run_ids": ["bad id"]},
    {"run_ids": ["run-0000000001"] * 201},
    {"message": "x" * 501},
])
def test_report_validation(perform, params):
    rid = request(perform)["refresh_id"]
    body = {"refresh_id": rid, "status": "running", **params}
    assert perform("report_refresh", body).status_code == 400
