"""The packaged manifest: the properties the broker and this adapter rely on.

The broker validates the manifest with its own loader when the owner pins
it; `test_the_gateway_loader_accepts_the_manifest` runs that loader here (from
the gateway checkout, test-only) so a manifest the broker would refuse never
ships.
"""

import re

import pytest
import yaml

from aab_plugin_finance.adapter import MANIFEST_PATH, FinanceAdapter

from .gateway import import_broker

MANIFEST = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
ACTIONS = {a["name"]: a for a in MANIFEST["actions"]}


def test_every_manifest_action_has_a_handler_and_back(adapter):
    assert set(ACTIONS) == set(adapter._actions)


def test_a_manifest_adapter_mismatch_refuses_to_boot(tmp_path, store):
    m = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    m["actions"].append({"name": "delete_everything", "side_effect": "destructive"})
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(m), encoding="utf-8")
    with pytest.raises(RuntimeError, match="mismatch"):
        FinanceAdapter(store, manifest_path=path)


def test_flags_are_phrased_so_true_is_permissive():
    """The algebra treats an absent flag as true and DROPS true as top, so a
    flag whose true value restricts would silently vanish from every grant."""
    flags = [c["name"] for c in MANIFEST["constraints"] if c["form"] == "flag"]
    assert flags == ["notes", "merchant_names"]
    for name in flags:
        assert not re.search(r"^(hide|no|block|deny|only)_|_only$|^metadata_only$", name), name


def test_modes():
    assert ACTIONS["ingest_snapshot"]["modes"] == ["direct"]       # never queued with its payload
    assert ACTIONS["report_refresh"]["modes"] == ["direct"]
    assert ACTIONS["request_refresh"]["modes"] == ["draft"]        # always a human
    assert ACTIONS["set_note"]["modes"] == ["direct", "draft"]
    for a in ACTIONS.values():
        if a["side_effect"] == "read":
            assert a.get("modes") in (None, ["direct"]), a["name"]
    assert {a["side_effect"] for a in ACTIONS.values()} == {"read", "write"}     # nothing destructive


def test_nothing_to_configure_and_no_credential():
    assert MANIFEST["config_schema"] == []
    assert MANIFEST["connection"] == {"kind": "none", "enforcement": "proxy"}


def test_params_stay_inside_the_broker_subset():
    """No number type, no pattern, no maxItems (finance-plugin-plan.md 1):
    amounts are integers in hundredths, lengths are capped by the adapter."""
    def walk(node, path):
        if isinstance(node, dict):
            assert node.get("type") != "number", path
            assert not {"pattern", "maxItems", "minItems", "additionalProperties",
                        "format"} & set(node), path
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    for a in ACTIONS.values():
        walk(a.get("params", {}), a["name"])
    amounts = ACTIONS["ingest_snapshot"]["params"]["properties"]["transactions"]["items"]
    assert {k for k, v in amounts["properties"].items() if k.endswith("_x100")} == {
        "original_x100", "charged_x100"}
    assert all(amounts["properties"][k]["type"] == "integer"
               for k in ("original_x100", "charged_x100"))


def test_per_source_reads_are_narrowable_by_card_and_account():
    per_source = {"snapshot_info", "list_sources", "list_transactions", "get_transaction",
                  "search_transactions", "monthly_summary", "by_category", "top_merchants",
                  "recurring_merchants", "subscriptions", "installments", "foreign_currency",
                  "largest_purchases", "set_note", "list_notes", "list_runs"}
    by_dim = {n["dimension"]: n for n in MANIFEST["narrowings"]}
    assert set(by_dim["card"]["applies_to"]) == per_source
    assert set(by_dim["account"]["applies_to"]) == per_source
    assert by_dim["company"]["applies_to"] == ["ingest_snapshot"]
    assert ACTIONS["ingest_snapshot"]["resource"] == "company"


def test_merchant_names_covers_every_action_returning_descriptions():
    names = next(c for c in MANIFEST["constraints"] if c["name"] == "merchant_names")
    assert set(names["applies_to"]) == {"list_transactions", "get_transaction",
                                        "search_transactions", "top_merchants",
                                        "recurring_merchants", "subscriptions", "installments",
                                        "largest_purchases", "list_notes"}


def test_transactions_are_not_resolvable_by_name():
    # The broker's agent-facing resolve filters by the transaction kind only,
    # so a name lookup would list transactions of hidden cards.
    assert MANIFEST["resources"]["transaction"]["resolve"] is False


def test_summary_templates_name_only_params():
    assert ACTIONS["set_note"]["summary_template"] == "Note on transaction {id}: {note}"
    assert ACTIONS["request_refresh"]["summary_template"] == \
        "Refresh {company} transactions {start}..{end}: {reason}"


def test_the_gateway_loader_accepts_the_manifest():
    broker, why = import_broker()
    if broker is None:
        pytest.skip(why)
    from broker.plugins.manifest import load_manifest
    m = load_manifest(MANIFEST_PATH)
    assert m.id == "finance" and m.version == MANIFEST["version"]
    assert m.action("request_refresh").effective_modes == ("draft",)
    assert m.action("ingest_snapshot").effective_modes == ("direct",)
    assert m.action("set_note").effective_modes == ("direct", "draft")
    assert {a.name for a in m.actions} == set(ACTIONS)
