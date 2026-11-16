"""M3 质量隔离：逐行归一、判定、落隔离区、给出原因分布。

行级规则只在这里跑一次：订阅行先落库，变更行再按订阅时区定位生效时刻。
隔离区与标记的区别：``error`` 行不可用；``warn``/``info`` 行可用但留下事实。
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from ... import config
from ... import reasons
from ...reasons import level_of
from ..m1_ingest import columns as column_mod
from ..m2_calendar import service as calendar_service
from ..m6_invoices import invoice as invoice_mod
from . import quarantine, rules

REQUIRED_SUB = column_mod.SUB_REQUIRED
REQUIRED_CHANGE = column_mod.CHANGE_REQUIRED


def _entry(file: str, row_no: int, code: str, *, field: str = "", raw: str = "",
           detail: str = "") -> dict:
    return quarantine.entry(file=file, row_no=row_no, code=code, field=field,
                            raw_value=raw, detail=detail)


def _file_entries(table, mapping, required: tuple[str, ...]) -> list[dict]:
    entries: list[dict] = []
    if table.state_code:
        entries.append(_entry(table.name, 0, table.state_code,
                              detail=f"编码 {table.encoding}，字节 {table.size}"))
        return entries
    missing = column_mod.required_missing(mapping, required)
    if missing:
        entries.append(_entry(table.name, 0, "R_HEADER_MISSING",
                              field=",".join(missing),
                              raw=",".join(table.header)))
        return entries
    if mapping.unknown:
        entries.append(_entry(table.name, 0, "R_HEADER_UNKNOWN",
                              field=",".join(mapping.unknown),
                              detail="未登记列已忽略"))
    if table.repeated_headers:
        entries.append(_entry(table.name, 0, "R_HEADER_REPEAT",
                              detail=f"重复表头 {table.repeated_headers} 行已剔除"))
    if table.truncated_rows:
        entries.append(_entry(table.name, table.truncated_row_no, "R_ROW_TRUNCATED",
                              detail="引号未闭合，半行不参与计算"))
    if table.blank_rows and table.rows:
        entries.append(_entry(table.name, 0, "R_BLANK_ROWS",
                              detail=f"空白行 {table.blank_rows} 行"))
    return entries


FATAL_STATES = ("R_EMPTY_FILE", "R_HEADER_ONLY", "R_BLANK_ROWS")
REVIEW_FLAGS = ("R_LATE_CHANGE", "R_TIME_TRUNCATED", "R_DST_AMBIGUOUS", "R_DST_GAP",
                "R_CHANGE_OUT_OF_PERIOD", "R_CHANGE_ON_PERIOD_START")


def _signature(record: dict) -> tuple:
    return (
        record["account"], record["plan"], str(record["monthly"]),
        record["period_start"], record["period_end"], record["tz"], record["state"],
    )


def _period_key(record: dict) -> tuple[str, str, str]:
    """订阅行业务键：订阅号 + 账期。同一订阅的不同账期是合法续期，各自出账。"""
    return (record["sub_id"], record["period_start"], record["period_end"])


def _period_conflict(periods: list[dict], record: dict) -> tuple[dict, str] | None:
    """同一订阅的既有账期里，与新行冲突的第一条（重叠或同一账单月）。

    半开区间重叠 ⇒ 同一批天会被计两次费；同一账单月 ⇒ 账单号撞号，
    两种都不能各自出账，只能保留先到的行、把后到的行隔离留证。
    """
    start = dt.date.fromisoformat(record["period_start"])
    end = dt.date.fromisoformat(record["period_end"])
    month = invoice_mod.invoice_month(record["period_start"])
    for kept in periods:
        kept_start = dt.date.fromisoformat(kept["period_start"])
        kept_end = dt.date.fromisoformat(kept["period_end"])
        if start < kept_end and kept_start < end:
            return kept, "R_PERIOD_OVERLAP"
        if invoice_mod.invoice_month(kept["period_start"]) == month:
            return kept, "R_DUP_INVOICE"
    return None


def _normalize_subscriptions(table, settings) -> tuple[list[dict], list[dict], dict]:
    mapping = column_mod.sub_header(table.header)
    entries = _file_entries(table, mapping, REQUIRED_SUB)
    counts = {
        "rows": table.data_rows,
        "kept": 0,
        "quarantined_rows": 0,
        "dropped_dup": 0,
        "blank_rows": table.blank_rows,
        "repeated_headers": table.repeated_headers,
        "truncated": table.truncated_rows,
    }
    if table.state_code in FATAL_STATES or not mapping.complete:
        counts["quarantined_rows"] = len(table.rows) + table.truncated_rows
        return [], entries, counts

    header_len = len(table.header)
    records: list[dict] = []
    seen: dict[tuple[str, str, str], dict] = {}
    periods_by_sub: dict[str, list[dict]] = {}
    error_rows: set[int] = set()

    def reject(row_no: int, code: str, field: str = "", raw: str = "", detail: str = ""):
        entries.append(_entry(table.name, row_no, code, field=field,
                              raw=raw, detail=detail))
        error_rows.add(row_no)

    for row in table.rows:
        if not column_mod.column_count_ok(row.fields, header_len):
            reject(row.row_no, "R_COLUMN_COUNT", field="列数",
                   raw=f"{len(row.fields)}列/表头{header_len}列")
            continue
        sub_id = mapping.value(row.fields, "sub_id").strip()
        if not sub_id:
            reject(row.row_no, "R_KEY_MISSING", field="订阅号", raw=row.raw)
            continue
        price = rules.normalize_money(mapping.value(row.fields, "price"))
        if price.code:
            reject(row.row_no, price.code, field="月价",
                   raw=mapping.value(row.fields, "price"))
            continue
        start = rules.normalize_date(mapping.value(row.fields, "period_start"))
        end = rules.normalize_date(mapping.value(row.fields, "period_end"))
        if start.code or end.code:
            reject(row.row_no, start.code or end.code, field="期间",
                   raw=f"{mapping.value(row.fields, 'period_start')}~"
                       f"{mapping.value(row.fields, 'period_end')}")
            continue
        if start.value is None or end.value is None:
            reject(row.row_no, "R_PERIOD_MISSING", field="期间", raw=row.raw)
            continue
        if end.value < start.value:
            reject(row.row_no, "R_PERIOD_REVERSED", field="期间止",
                   raw=f"{start.value}~{end.value}")
            continue
        if end.value == start.value:
            reject(row.row_no, "R_PERIOD_ZERO", field="期间", raw=f"{start.value}")
            continue
        tz_text = mapping.value(row.fields, "tz").strip()
        tz_parsed = rules.parse_zone(tz_text)
        if tz_parsed.code:
            reject(row.row_no, tz_parsed.code, field="时区", raw=tz_text)
            continue
        state = rules.normalize_state(mapping.value(row.fields, "state"))
        if state.code:
            reject(row.row_no, state.code, field="状态",
                   raw=mapping.value(row.fields, "state"))
            continue
        record = {
            "sub_id": sub_id,
            "account": mapping.value(row.fields, "account").strip(),
            "plan": mapping.value(row.fields, "plan").strip(),
            "price_text": mapping.value(row.fields, "price").strip(),
            "monthly": price.value,
            "period_start": start.value.isoformat(),
            "period_end": end.value.isoformat(),
            "tz": tz_text or settings.display_tz,
            "state": state.value,
            "period_days": calendar_service.period_days(start.value.isoformat(),
                                                        end.value.isoformat()),
            "leap": int(calendar_service.leap_of(start.value.isoformat(),
                                                 end.value.isoformat())),
            "source_file": table.name,
            "row_no": row.row_no,
            "flags": list(price.flags),
        }
        key = _period_key(record)
        if key in seen:
            kept = seen[key]
            if _signature(kept) == _signature(record):
                entries.append(_entry(table.name, row.row_no, "R_DUP_SUB_ROW",
                                      field="订阅号", raw=sub_id,
                                      detail=f"账期 {record['period_start']}~{record['period_end']} "
                                             f"与第 {kept['row_no']} 行完全相同"))
                counts["dropped_dup"] += 1
            else:
                entries.append(_entry(table.name, row.row_no, "R_DUP_SUB_KEY",
                                      field="订阅号", raw=row.raw,
                                      detail=f"账期 {record['period_start']}~{record['period_end']} "
                                             f"与第 {kept['row_no']} 行取值冲突，保留前者"))
                error_rows.add(row.row_no)
                if "R_DUP_SUB_KEY" not in kept["flags"]:
                    kept["flags"].append("R_DUP_SUB_KEY")
            continue
        conflict = _period_conflict(periods_by_sub.get(sub_id, []), record)
        if conflict is not None:
            kept, code = conflict
            entries.append(_entry(table.name, row.row_no, code, field="期间",
                                  raw=row.raw,
                                  detail=f"账期 {record['period_start']}~{record['period_end']} "
                                         f"与第 {kept['row_no']} 行账期 "
                                         f"{kept['period_start']}~{kept['period_end']} 冲突，保留前者"))
            error_rows.add(row.row_no)
            if code not in kept["flags"]:
                kept["flags"].append(code)
            continue
        seen[key] = record
        periods_by_sub.setdefault(sub_id, []).append(record)
        records.append(record)

    counts["kept"] = len(records)
    counts["quarantined_rows"] = len(error_rows)
    return records, entries, counts


def _change_price(kind: str, text: str) -> rules.Parsed:
    return rules.change_price(kind, text)


def _normalize_changes(table, subs: list[dict], settings) -> tuple[list[dict], list[dict], dict]:
    mapping = column_mod.change_header(table.header)
    entries = _file_entries(table, mapping, REQUIRED_CHANGE)
    counts = {
        "rows": table.data_rows,
        "kept": 0,
        "quarantined_rows": 0,
        "dropped_dup": 0,
        "blank_rows": table.blank_rows,
        "repeated_headers": table.repeated_headers,
        "truncated": table.truncated_rows,
    }
    if table.state_code in FATAL_STATES or not mapping.complete:
        counts["quarantined_rows"] = len(table.rows) + table.truncated_rows
        return [], entries, counts

    header_len = len(table.header)
    by_sub: dict[str, list[dict]] = {}
    for sub in subs:
        by_sub.setdefault(sub["sub_id"], []).append(sub)
    records: list[dict] = []
    error_rows: set[int] = set()
    last_utc: dict[str, dt.datetime] = {}

    for seq, row in enumerate(table.rows):
        if not column_mod.column_count_ok(row.fields, header_len):
            entries.append(_entry(table.name, row.row_no, "R_COLUMN_COUNT", field="列数",
                                  raw=f"{len(row.fields)}列/表头{header_len}列"))
            error_rows.add(row.row_no)
            continue
        change_id = mapping.value(row.fields, "change_id").strip()
        sub_id = mapping.value(row.fields, "sub_id").strip()
        if not change_id or not sub_id:
            entries.append(_entry(table.name, row.row_no, "R_KEY_MISSING",
                                  field="变更号/订阅号", raw=row.raw))
            error_rows.add(row.row_no)
            continue
        periods = by_sub.get(sub_id)
        if not periods:
            entries.append(_entry(table.name, row.row_no, "R_CHANGE_UNKNOWN_SUB",
                                  field="订阅号", raw=sub_id))
            error_rows.add(row.row_no)
            continue
        parsed = rules.parse_instant(mapping.value(row.fields, "effective"))
        if parsed.code:
            entries.append(_entry(table.name, row.row_no, parsed.code, field="生效时间",
                                  raw=mapping.value(row.fields, "effective")))
            error_rows.add(row.row_no)
            continue
        kind = rules.normalize_kind(mapping.value(row.fields, "kind"))
        if kind.code:
            entries.append(_entry(table.name, row.row_no, kind.code, field="类型",
                                  raw=mapping.value(row.fields, "kind")))
            error_rows.add(row.row_no)
            continue
        price = _change_price(kind.value, mapping.value(row.fields, "new_price"))
        if price.code:
            entries.append(_entry(table.name, row.row_no, price.code, field="新月价",
                                  raw=mapping.value(row.fields, "new_price")))
            error_rows.add(row.row_no)
            continue

        zone = rules.parse_zone(periods[0]["tz"]).value or ZoneInfo(settings.display_tz)
        instant = parsed.value
        dst_flag = None
        if instant.offset is not None:
            utc = instant.naive.replace(tzinfo=instant.offset).astimezone(config.UTC)
        else:
            zone = (rules.parse_zone(instant.zone_name).value if instant.zone_name else zone) \
                or zone
            utc, dst_flag = calendar_service.resolve_effective(instant.naive, zone)
        local_dt = utc.astimezone(zone)
        local_day = local_dt.date()
        flags: list[str] = list(price.flags)
        if dst_flag:
            flags.append(dst_flag)
        if local_dt.time() != dt.time(0, 0):
            flags.append("R_TIME_TRUNCATED")
        # 变更只写订阅号与生效时间：本地化后落入覆盖该本地日的账期；
        # 账期互不重叠（M3 订阅侧保证），因此至多落入一个账期。
        covering = None
        for period in periods:
            period_start = dt.date.fromisoformat(period["period_start"])
            period_end = dt.date.fromisoformat(period["period_end"])
            if period_start <= local_day < period_end:
                covering = period_start
                break
        out_of_period = covering is None
        if out_of_period:
            flags.append("R_CHANGE_OUT_OF_PERIOD")
        elif local_day == covering:
            flags.append("R_CHANGE_ON_PERIOD_START")
        previous = last_utc.get(sub_id)
        if previous is not None and utc < previous:
            flags.append("R_LATE_CHANGE")
        if previous is None or utc > previous:
            last_utc[sub_id] = utc
        for flag in flags:
            if flag in REVIEW_FLAGS:
                entries.append(_entry(table.name, row.row_no, flag, field="标记",
                                      raw=mapping.value(row.fields, "effective"),
                                      detail=f"{change_id} {local_dt:%Y-%m-%d %H:%M:%S}"))
        records.append({
            "seq": seq,
            "change_id": change_id,
            "sub_id": sub_id,
            "effective_utc": utc.isoformat(),
            "effective_local": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "local_date": local_day.isoformat(),
            "kind": kind.value,
            "new_plan": mapping.value(row.fields, "new_plan").strip(),
            "new_price_text": mapping.value(row.fields, "new_price").strip(),
            "new_price": price.value if price.value is not None else "",
            "source_file": table.name,
            "row_no": row.row_no,
            "applied": 0 if out_of_period else 1,
            "superseded_by": "",
            "flags": flags,
        })

    _resolve_same_day(records, entries, table.name)
    counts["kept"] = len(records)
    counts["quarantined_rows"] = len(error_rows)
    return records, entries, counts


def _resolve_same_day(records: list[dict], entries: list[dict], file: str) -> None:
    """同一订阅同一本地日只允许一次变更生效；胜者按 (UTC, 变更号) 最小，结果与行序无关。"""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        grouped.setdefault((record["sub_id"], record["local_date"]), []).append(record)
    for (sub_id, day), group in sorted(grouped.items()):
        if len(group) == 1:
            continue
        winner = min(group, key=lambda item: (item["effective_utc"], item["change_id"],
                                              item["seq"]))
        same_instant = len({item["effective_utc"] for item in group}) < len(group)
        if same_instant and "R_CHANGE_OVERLAP" not in winner["flags"]:
            winner["flags"].append("R_CHANGE_OVERLAP")
        for record in group:
            if record is winner:
                continue
            if "R_CHANGE_OVERLAP" in record["flags"] or same_instant:
                record["flags"].append("R_CHANGE_OVERLAP")
            record["flags"].append("R_SAME_DAY_MULTI")
            record["applied"] = 0
            record["superseded_by"] = winner["change_id"]
            entries.append(_entry(file, record["row_no"], "R_SAME_DAY_MULTI",
                                  field="生效时间", raw=record["effective_local"],
                                  detail=f"{sub_id} 当日由 {winner['change_id']} 生效"))
            if same_instant:
                entries.append(_entry(file, record["row_no"], "R_CHANGE_OVERLAP",
                                      field="生效时间", raw=record["effective_local"],
                                      detail=f"{sub_id} 同一时刻多条变更"))


def prepare(store, run_id: str, sub_table, change_table, settings) -> dict:
    """把两张表归一成订阅与变更记录，落库并返回计数器。"""
    subs, sub_entries, sub_counts = _normalize_subscriptions(sub_table, settings)
    changes, change_entries, change_counts = _normalize_changes(change_table, subs, settings)
    entries = sorted(sub_entries + change_entries,
                     key=lambda item: (item["file"], item["row_no"], item["code"]))
    store.put_subscriptions(run_id, subs)
    store.put_changes(run_id, changes)
    store.put_quarantine(run_id, entries)

    counters = {
        "rows_read": sub_counts["rows"] + change_counts["rows"],
        "sub_rows": sub_counts["rows"],
        "change_rows": change_counts["rows"],
        "subs_kept": sub_counts["kept"],
        "subs_dropped_dup": sub_counts["dropped_dup"],
        "changes_kept": change_counts["kept"],
        "changes_superseded": sum(1 for item in changes if not item["applied"]),
        "blank_rows": sub_counts["blank_rows"] + change_counts["blank_rows"],
        "repeated_headers": sub_counts["repeated_headers"] + change_counts["repeated_headers"],
        "quarantined_rows": (sub_counts["quarantined_rows"]
                             + change_counts["quarantined_rows"]),
        "quarantine_entries": len(entries),
        "dropped_dup": sub_counts["dropped_dup"] + change_counts["dropped_dup"],
        "truncated_rows": sub_counts["truncated"] + change_counts["truncated"],
    }
    counters["quarantine_by_level"] = {
        level: sum(1 for item in entries if item["level"] == level)
        for level in ("error", "warn", "info")
    }
    counters["quarantine_by_code"] = quarantine.count_by_code(entries)
    return {
        "subscriptions": subs,
        "changes": changes,
        "entries": entries,
        "counters": counters,
        "distribution": quarantine.distribution(entries),
    }


def decorate(entry: dict) -> dict:
    """库里的隔离行只存事实；级别名与原因名按原因码表补齐。"""
    level = entry.get("level") or reasons.level_of(entry["code"])
    return {
        **entry,
        "level": level,
        "level_label": reasons.LEVEL_LABELS.get(level, level),
        "label": reasons.label_of(entry["code"]),
    }


def load_quarantine(store, run_id: str) -> dict:
    entries = [decorate(row) for row in store.list_quarantine(run_id)]
    return {
        "entries": entries,
        "distribution": quarantine.distribution(entries),
        "totals": {
            "entries": len(entries),
            "error": sum(1 for e in entries if e["level"] == "error"),
            "warn": sum(1 for e in entries if e["level"] == "warn"),
            "info": sum(1 for e in entries if e["level"] == "info"),
        },
    }


def reason_catalog() -> list[dict]:
    return quarantine.catalog()


__all__ = ["prepare", "load_quarantine", "reason_catalog", "level_of"]
