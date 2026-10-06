"""CallScope parsing fails closed: malformed means 400, `[]` means nothing,
absent constraints are the top, and the parser rejects unknown constraints."""

import pytest
import yaml
from aab_plugin_runtime import AdapterError

from aab_plugin_finance.adapter import MANIFEST_PATH
from aab_plugin_finance.scope import CallScope, View, Visibility, constraint_forms

from .conftest import items, scope
from .fakes import CAL, seed

FORMS = constraint_forms(yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))


def parse(raw):
    return CallScope(raw, FORMS)


def status(raw) -> int:
    with pytest.raises(AdapterError) as e:
        parse(raw)
    return e.value.status


def test_forms_come_from_the_manifest():
    assert FORMS == {"date_window_days": ("range", ()),
                     "detail": ("level", ("aggregate", "line_items")),
                     "notes": ("flag", ()), "merchant_names": ("flag", ())}


@pytest.mark.parametrize("raw", [
    "not a dict", ["list"],
    {"visibility": "card"},
    {"visibility": {"card": ["cal:1234"]}},
    {"visibility": {"card": {"deny": "cal:1234"}}},             # a string is not a list
    {"visibility": {"card": {"deny": [1]}}},
    {"visibility": {"card": {"allow_only": "cal:1234"}}},
    {"constraints": ["detail"]},
    {"constraints": {"unknown": 1}},
    {"constraints": {"date_window_days": "30"}},
    {"constraints": {"detail": "all"}},
    {"constraints": {"notes": 1}},
    {"credential": "x"},
])
def test_malformed_scopes_are_400(raw):
    assert status(raw) == 400


def test_absent_means_unrestricted():
    s = parse({})
    assert s.vis("card") == Visibility() and not s.vis("card").restricted
    assert s.flag("notes") is True and s.bound("date_window_days") is None
    assert s.level("detail", "line_items") == "line_items"


def test_an_empty_allow_list_admits_nothing():
    v = parse({"visibility": {"card": {"deny": [], "allow_only": []}}}).vis("card")
    assert v.allow == frozenset() and not v.admits(CAL)


def test_deny_wins_over_allow():
    v = parse({"visibility": {"card": {"deny": [CAL], "allow_only": [CAL]}}}).vis("card")
    assert not v.admits(CAL)


def test_vis_pair_is_sorted():
    s = parse({"visibility": {"card": {"deny": ["max:2", "cal:1"], "allow_only": None}}})
    assert s.vis_pair("card") == (["cal:1", "max:2"], None)


def test_check_named_answers_403_outside_the_grant_before_404_hidden():
    both = Visibility(deny=frozenset({"cal"}), allow=frozenset({"max"}))
    with pytest.raises(AdapterError) as e:
        both.check_named("cal", "company")          # outside the grant AND hidden
    assert e.value.status == 403
    with pytest.raises(AdapterError) as e:
        Visibility(deny=frozenset({"cal"})).check_named("cal", "company")
    assert e.value.status == 404


def test_an_unknown_source_kind_sees_nothing():
    assert View().source("wallet").admits("x:1") is False
    assert View().admits_source("card", CAL, "cal") is True


def test_the_request_id_is_read_when_it_is_a_string():
    assert parse({"request_id": "abc"}).request_id == "abc"
    assert parse({"request_id": 5}).request_id == ""


def test_a_malformed_scope_over_the_runtime_is_400(perform):
    seed(perform)
    bad = {"visibility": {"card": {"deny": "cal:1234", "allow_only": None}}}
    assert perform("list_transactions", {}, bad).status_code == 400
    assert len(items(perform("list_transactions", {}, scope()))) > 0
