"""续费账期：合法续期 vs 重复记录、变更落账期、幂等、页面与导出一致。

样例：`subscriptions_renewal.csv` + `changes_renewal.csv`
- SUB-2001 三个连续账期（1/2/3 月），另带一条完全相同与一条取值冲突的重复导出；
- SUB-2002 第二条账期与第一条重叠；
- SUB-2003 两个账期落在同一账单月（账单号会撞号）；
- CHG-2001/2002 分别落在 2/3 月账期，CHG-2003 落在所有账期之外。

手算基准（2026 年，1 月 31 天、2 月 28 天、3 月 31 天）：
- INV-202601-SUB-2001  900.00                 → 90000
- INV-202602-SUB-2001  900×9/28 + 1800×19/28 = 1510.714285… → 151071
- INV-202603-SUB-2001  1800×4/31 + 300×27/31 = 493.548387…  → 49355
- INV-202602-SUB-2002  600.00（重叠行被隔离）  → 60000
- INV-202602-SUB-2003  100.00（同月第二期被隔离）→ 10000
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from app.modules.m1_ingest import service as ingest_service
from app.modules.m5_changes import service as change_service

SUBS = "subscriptions_renewal.csv"
CHANGES = "changes_renewal.csv"

EXPECTED_CENTS = {
    "INV-202601-SUB-2001": 90000,
    "INV-202602-SUB-2001": 151071,
    "INV-202603-SUB-2001": 49355,
    "INV-202602-SUB-2002": 60000,
    "INV-202602-SUB-2003": 10000,
}
TOTAL_CENTS = sum(EXPECTED_CENTS.values())


@pytest.fixture()
def renewal(store, settings) -> dict:
    return ingest_service.import_sample(store, settings, SUBS, CHANGES)


@pytest.fixture()
def renewal_client(client) -> object:
    response = client.post("/api/subscriptions/import",
                           json={"subscriptions": SUBS, "changes": CHANGES})
    assert response.status_code == 200
    return client


def _invoices(store, run_id):
    return {row["invoice_id"]: row for row in store.list_invoices(run_id)}


def test_consecutive_periods_are_renewals_not_duplicates(renewal, store):
    # 同一订阅的连续账期是合法续期：三条都保留，各出一张账单，续费月不漏账
    run_id = renewal["run_id"]
    subs = [row for row in store.list_subscriptions(run_id) if row["sub_id"] == "SUB-2001"]
    assert [row["period_start"] for row in subs] == ["2026-01-01", "2026-02-01",
                                                     "2026-03-01"]
    invoices = _invoices(store, run_id)
    assert set(invoices) == set(EXPECTED_CENTS)
    for invoice_id, cents in EXPECTED_CENTS.items():
        assert invoices[invoice_id]["total_cents"] == cents, invoice_id
    assert renewal["counters"]["subs_kept"] == 5


def test_identical_reexport_dropped_without_second_invoice(renewal, store):
    # 同一账期重复导出（取值完全相同）：丢弃后行记 info，不出第二张账单
    run_id = renewal["run_id"]
    assert renewal["counters"]["dropped_dup"] == 1
    entries = [row for row in store.list_quarantine(run_id)
               if row["code"] == "R_DUP_SUB_ROW"]
    assert len(entries) == 1 and entries[0]["row_no"] == 5
    feb = [row for row in store.list_invoices(run_id)
           if row["invoice_id"] == "INV-202602-SUB-2001"]
    assert len(feb) == 1 and feb[0]["total_cents"] == 151071


def test_conflicting_reexport_quarantined_record_not_lost(renewal, store):
    # 同一账期重复导出（取值冲突）：保留行号小的，冲突行进隔离区留证
    run_id = renewal["run_id"]
    conflicts = [row for row in store.list_quarantine(run_id)
                 if row["code"] == "R_DUP_SUB_KEY"]
    assert len(conflicts) == 1
    assert conflicts[0]["row_no"] == 6
    # 冲突行完整内容留在隔离区（文件 + 行号 + 原值），不丢记录
    assert "999.00" in conflicts[0]["raw_value"]
    kept = [row for row in store.list_subscriptions(run_id)
            if row["sub_id"] == "SUB-2001" and row["period_start"] == "2026-03-01"]
    assert len(kept) == 1
    assert kept[0]["monthly"] == "1800.00"
    assert "R_DUP_SUB_KEY" in kept[0]["flags"]
    # 账单按保留行出账，冲突值不影响金额
    assert _invoices(store, run_id)["INV-202603-SUB-2001"]["total_cents"] == 49355


def test_overlapping_period_quarantined(renewal, store):
    # 账期重叠不是续期：同一批天不能计两次费，后到的行进隔离区
    run_id = renewal["run_id"]
    overlaps = [row for row in store.list_quarantine(run_id)
                if row["code"] == "R_PERIOD_OVERLAP"]
    assert len(overlaps) == 1
    assert overlaps[0]["row_no"] == 8
    assert "2026-02-15" in overlaps[0]["raw_value"]
    subs = [row for row in store.list_subscriptions(run_id) if row["sub_id"] == "SUB-2002"]
    assert len(subs) == 1
    assert "R_PERIOD_OVERLAP" in subs[0]["flags"]
    assert _invoices(store, run_id)["INV-202602-SUB-2002"]["total_cents"] == 60000


def test_same_invoice_month_blocked(renewal, store):
    # 两个不同账期落在同一账单月：账单号撞号，后到的行隔离，防止一号两账
    run_id = renewal["run_id"]
    entries = [row for row in store.list_quarantine(run_id)
               if row["code"] == "R_DUP_INVOICE"]
    assert len(entries) == 1 and entries[0]["row_no"] == 10
    subs = [row for row in store.list_subscriptions(run_id) if row["sub_id"] == "SUB-2003"]
    assert len(subs) == 1
    invoices = [row for row in store.list_invoices(run_id) if row["sub_id"] == "SUB-2003"]
    assert len(invoices) == 1
    assert invoices[0]["total_cents"] == 10000


def test_change_lands_only_in_covering_period(renewal, store):
    # 变更只写订阅号与生效时间：落入覆盖该本地日的账期，且只进那一个账期
    run_id = renewal["run_id"]
    segments = {}
    for row in store.list_segments(run_id):
        segments.setdefault(row["invoice_id"], []).append(row)
    assert [row["days"] for row in segments["INV-202601-SUB-2001"]] == [31]
    assert [row["days"] for row in segments["INV-202602-SUB-2001"]] == [9, 19]
    assert [row["days"] for row in segments["INV-202603-SUB-2001"]] == [4, 27]
    rows = {row["change_id"]: row
            for row in change_service.change_rows(store, run_id)}
    assert rows["CHG-2001"]["invoice_id"] == "INV-202602-SUB-2001"
    assert rows["CHG-2002"]["invoice_id"] == "INV-202603-SUB-2001"
    # 抵扣行挂在变更落入的账单上，不跨账期
    sources = {row["invoice_id"] for row in rows["CHG-2001"]["components"]}
    assert sources == {"INV-202602-SUB-2001"}
    sources = {row["invoice_id"] for row in rows["CHG-2002"]["components"]}
    assert sources == {"INV-202603-SUB-2001"}


def test_change_outside_all_periods_kept_but_not_applied(renewal, store):
    run_id = renewal["run_id"]
    rows = {row["change_id"]: row
            for row in change_service.change_rows(store, run_id)}
    chg = rows["CHG-2003"]
    assert chg["applied"] is False
    assert "R_CHANGE_OUT_OF_PERIOD" in chg["flags"]
    assert chg["invoice_id"] == ""
    assert chg["components"] == []


def test_ledgers_balanced_with_renewals(renewal, store):
    assert [row["status"] for row in renewal["ledgers"]] == ["ok"] * 4
    counters = renewal["counters"]
    assert counters["rows_read"] == 12
    right = (counters["subs_kept"] + counters["changes_kept"]
             + counters["quarantined_rows"] + counters["dropped_dup"]
             + counters["blank_rows"] + counters["repeated_headers"])
    assert counters["rows_read"] == right
    totals = store.list_totals(renewal["run_id"])
    assert totals["invoice_cents"] == str(TOTAL_CENTS)


def test_reimport_reuses_run_and_adds_no_invoices(renewal, store, settings):
    run_id = renewal["run_id"]
    before = [(row["invoice_id"], row["total_cents"])
              for row in store.list_invoices(run_id)]
    again = ingest_service.import_sample(store, settings, SUBS, CHANGES)
    assert again["reused"] is True
    assert again["run_id"] == run_id
    assert len(store.list_runs()) == 1
    after = [(row["invoice_id"], row["total_cents"])
             for row in store.list_invoices(run_id)]
    assert before == after


def test_page_and_export_give_same_periods_and_amounts(renewal_client):
    api = renewal_client.get("/api/invoices").json()
    text = renewal_client.get("/api/export/invoices.csv").text
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    from_api = {(row["invoice_id"], row["period_start"], row["period_end"],
                 Decimal(row["total_cents"]) / 100)
                for row in api["invoices"]}
    from_csv = {(row["账单号"], row["期间起"], row["期间止"],
                 Decimal(row["账单额(元)"]))
                for row in rows}
    assert from_api == from_csv
    assert len(rows) == len(EXPECTED_CENTS)
    page = renewal_client.get("/invoices")
    assert page.status_code == 200
    for invoice_id in EXPECTED_CENTS:
        assert invoice_id in page.text


def test_pages_render_with_renewals(renewal_client):
    for path in ("/", "/subscriptions", "/calendar", "/proration", "/changes",
                 "/invoices"):
        page = renewal_client.get(path)
        assert page.status_code == 200, path
    changes = renewal_client.get("/api/changes").json()
    by_id = {row["change_id"]: row for row in changes["changes"]}
    assert by_id["CHG-2001"]["invoice_id"] == "INV-202602-SUB-2001"
    assert by_id["CHG-2002"]["invoice_id"] == "INV-202603-SUB-2001"


def test_recompute_endpoints_idempotent_with_renewals(renewal_client):
    before = renewal_client.get("/api/invoices").json()["totals"]
    assert renewal_client.post("/api/changes").status_code == 200
    assert renewal_client.post("/api/proration").status_code == 200
    after = renewal_client.get("/api/invoices").json()["totals"]
    assert after["invoice_cents"] == before["invoice_cents"] == str(TOTAL_CENTS)
    assert after["invoices"] == before["invoices"] == str(len(EXPECTED_CENTS))
    ledgers = renewal_client.get("/api/reconcile").json()["ledgers"]
    assert all(row["status"] == "ok" for row in ledgers)
