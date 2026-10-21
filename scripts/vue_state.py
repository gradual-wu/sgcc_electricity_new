"""Vue状态注入辅助工具，用于读取95598页面数据。

向浏览器注入JavaScript，扫描所有DOM元素查找__vue__属性，
从Vue组件实例中提取结构化数据。
"""

from __future__ import annotations

from typing import Any, Optional


SELECTED_VUE_DATA_SCRIPT = """
const clone = (value) => {
  try { return JSON.parse(JSON.stringify(value)); } catch (e) { return null; }
};
const wantedKeys = [
  'mixinGetYuEdata',
  'consInfoobj',
  'consInfo',
  'cons',
  'electric',
  'powerData',
  'mothData',
  'tableData',
  'tableData_t',
  'sevenEleList',
  'sevenEleList_t',
  'new_sevenEleList',
  'tariffC',
  'start',
  'end',
  'queryYear',
  'activeName',
  'billNumberList',
  'BillList',
  'billList',
  'billListS',
  'billMonthS',
  'nowBillList',
  'basicInfo',
  'pvQtyList',
  'prcGroupList',
  'setBilling',
  'billMonth',
  'NewtotalBillProvince',
  'optionalYearArray',
  'selectYear',
  'listData',
  'payLists',
  'payList',
  'paymentList',
  'stepObj',
  'electricParticulars',
  'other',
  'userD',
  'soc',
  'yearstep',
  'newyear',
  'newmonthdate',
  'billSettleType'
];
return Array.from(document.querySelectorAll('*'))
  .map((el, index) => {
    const vm = el.__vue__;
    if (!vm) return null;
    const data = {};
    wantedKeys.forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(vm, key)) {
        data[key] = clone(vm[key]);
      }
    });
    if (!Object.keys(data).length) return null;
    return {
      index,
      tag: el.tagName,
      id: el.id || '',
      className: String(el.className || '').slice(0, 160),
      text: (el.innerText || el.textContent || '').trim().slice(0, 500),
      data
    };
  })
  .filter(Boolean);
"""


def selected_vue_data(driver) -> list[dict[str, Any]]:
    """执行JS脚本，从当前页面提取Vue状态数据。"""
    return driver.execute_script(SELECTED_VUE_DATA_SCRIPT) or []


def normalize_user_info(components: list[dict[str, Any]]) -> dict[str, Any]:
    """从Vue状态中提取用户信息（姓名、地址）。"""
    cons = _first_data_value(components, "consInfoobj") or _first_data_value(components, "consInfo") or {}
    if not isinstance(cons, dict):
        cons = {}
    return {
        "user_name": cons.get("consName") or cons.get("custName") or "",
        "address": cons.get("elecAddr") or cons.get("addr") or "",
        "user_id": cons.get("consNo") or cons.get("consId") or "",
    }


def normalize_balance(components: list[dict[str, Any]]) -> dict[str, Any]:
    """从Vue状态中提取应交金额（应交金额）。"""
    raw = _first_data_value(components, "mixinGetYuEdata") or {}
    return {
        "as_of": raw.get("amtTime"),
        "amount_due": _safe_float(raw.get("historyOwe")),
        "user_id": raw.get("consNo"),
    }


def normalize_usage(components: list[dict[str, Any]]) -> dict[str, Any]:
    """从Vue状态中提取用电量数据（年度汇总、月度、每日分时）。"""
    power_data = _first_data_value(components, "powerData") or _first_data_value(components, "tableData_t") or {}
    info = power_data.get("dataInfo") or {}
    month_rows = power_data.get("mothEleList") or _first_data_value(components, "mothData") or []

    daily_rows = []
    for key in ("tableData", "new_sevenEleList", "sevenEleList"):
        for row in _data_values(components, key):
            if isinstance(row, list) and row and any(
                item.get("thisVPq") is not None for item in row if isinstance(item, dict)
            ):
                daily_rows = row
                break
        if daily_rows:
            break

    return {
        "year": str(info.get("year") or _first_data_value(components, "queryYear") or ""),
        "yearly_usage": _safe_float(info.get("totalEleNum")),
        "yearly_charge": _safe_float(info.get("totalEleCost")),
        "recent_total_usage": _safe_float(_first_data_value(components, "tariffC")),
        "daily_range": {
            "start": _first_data_value(components, "start"),
            "end": _first_data_value(components, "end"),
        },
        "months": [_normalize_usage_month(row) for row in month_rows if isinstance(row, dict)],
        "daily": [_normalize_daily_row(row) for row in daily_rows if isinstance(row, dict) and _normalize_daily_row(row)],
        "raw": power_data,
    }


def normalize_bill_detail(components: list[dict[str, Any]]) -> dict[str, Any]:
    """从Vue状态中提取月度账单详情，含分时电价明细。"""
    bill = (_first_data_value(components, "billList") or [{}])[0]
    if not isinstance(bill, dict):
        bill = {}
    basic = bill.get("basicInfo") or {}
    pv_qty = (bill.get("pvQtyList") or [{}])[0]
    return {
        "month": _normalize_ym(bill.get("ym")),
        "user_id": basic.get("consNo"),
        "begin_date": basic.get("begDate"),
        "end_date": basic.get("endDate"),
        "usage": _safe_float(basic.get("monthPq")),
        "charge": _safe_float(basic.get("monthAmt")),
        "year_usage": _safe_float(basic.get("yearPq")),
        "year_charge": _safe_float(basic.get("yearAmt")),
        "valley_usage": _safe_float(pv_qty.get("valQty")),
        "flat_usage": _safe_float(pv_qty.get("flatQty")),
        "peak_usage": _safe_float(pv_qty.get("peakQty")),
        "tip_usage": _safe_float(pv_qty.get("sharpQty")),
        "raw": bill,
    }


def normalize_bill_summary(components: list[dict[str, Any]]) -> dict[str, Any]:
    """从电费账单汇总页提取月份列表（用于判断哪些月份还没有官方账单）。"""
    bills = []
    seen = set()
    for account_rows in _data_values(components, "billNumberList"):
        if not isinstance(account_rows, list):
            continue
        for account in account_rows:
            if not isinstance(account, dict):
                continue
            for bill in account.get("billList") or []:
                if not isinstance(bill, dict):
                    continue
                month_item = (bill.get("monthList") or [{}])[0]
                month = _normalize_ym(bill.get("ym"))
                if not month or month in seen:
                    continue
                seen.add(month)
                bills.append({
                    "month": month,
                    "usage": _safe_float(month_item.get("pq")),
                    "charge": _safe_float(month_item.get("amt")),
                    "calc_id": month_item.get("calcId"),
                    "begin_date": month_item.get("begDate"),
                    "end_date": month_item.get("endDate"),
                    "issue_date": month_item.get("issuDate"),
                    "settle_type": month_item.get("billSettleType"),
                    "settle_name": month_item.get("billSettleName"),
                })
    bills.sort(key=lambda item: item["month"])
    return {
        "year": str(_first_data_value(components, "selectYear") or ""),
        "available_years": _first_data_value(components, "optionalYearArray") or [],
        "bills": bills,
    }


def normalize_payment_records(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从交费记录页面提取 payLists。"""
    rows = []
    for key in ("payLists", "payList", "paymentList"):
        for value in _data_values(components, key):
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
        if rows:
            break
    records = []
    seen = set()
    for row in rows:
        pay_date = (
            row.get("payDate")
            or row.get("payTime")
            or row.get("rcvDate")
            or row.get("date")
        )
        amount = _safe_float(row.get("rcvAmt", row.get("amount", row.get("payAmt"))))
        if not pay_date or amount is None:
            continue
        key = (str(pay_date), amount, str(row.get("payModeName") or ""))
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "pay_date": pay_date,
            "amount": amount,
            "pay_mode": row.get("payModeName") or row.get("payMode") or "",
            "total_rep_amt": _safe_float(row.get("totalRepAmt")),
            "cons_no": row.get("consNo") or row.get("consNoDst"),
            "raw": row,
        })
    records.sort(key=lambda item: str(item["pay_date"]))
    return records


def normalize_bill_detail_full(
    components: list[dict[str, Any]],
    target_month: str | None = None,
) -> dict[str, Any]:
    """从账单详情页提取完整账单，包含阶梯（oneLevelPq/twoLevelPq/levelFlag）。"""
    candidates = []
    for key in ("billList", "nowBillList", "BillList"):
        for value in _data_values(components, key):
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                candidates.append(value)

    # 有些页面把 basicInfo 直接挂在组件上
    for value in _data_values(components, "basicInfo"):
        if isinstance(value, dict):
            candidates.append({"basicInfo": value})

    target = _normalize_ym(target_month) if target_month else ""
    with_basic = [
        candidate for candidate in candidates
        if isinstance(candidate.get("basicInfo"), dict) and candidate.get("basicInfo")
    ]
    chosen = None
    if target:
        for candidate in with_basic:
            basic = candidate.get("basicInfo") or {}
            month = _normalize_ym(candidate.get("ym") or basic.get("ym") or basic.get("month"))
            if month == target:
                chosen = candidate
                break
        if chosen is None:
            for candidate in with_basic:
                basic = candidate.get("basicInfo") or {}
                month = _normalize_ym(basic.get("begDate") or basic.get("endDate"))
                if month == target:
                    chosen = candidate
                    break
    if chosen is None and with_basic:
        chosen = with_basic[0]
    if chosen is None and target:
        for candidate in candidates:
            if _normalize_ym(candidate.get("ym")) == target:
                chosen = candidate
                break
    if chosen is None and candidates:
        chosen = candidates[0]
    chosen = chosen or {}

    basic = chosen.get("basicInfo") or {}
    pv_qty = (chosen.get("pvQtyList") or [{}])[0] or {}
    month_item = (chosen.get("monthList") or [{}])[0] or {}
    month = _normalize_ym(chosen.get("ym") or basic.get("ym") or basic.get("month"))

    # 阶梯信息优先取当前账单自身的 basicInfo（历史账单的阶梯数据应归属该月）；
    # 账单里没有时才回退到汇总页的 setBilling 或阶梯页的 electricParticulars。
    if any(key in basic for key in ("oneLevelPq", "levelFlag", "totalYearPq")):
        tier_source = basic
    else:
        tier_source = (
            _first_data_value(components, "setBilling")
            or _first_data_value(components, "stepObj")
            or _first_data_value(components, "electricParticulars")
            or basic
        )
    if not isinstance(tier_source, dict):
        tier_source = {}

    return {
        "month": month,
        "user_id": basic.get("consNo"),
        "begin_date": basic.get("begDate") or basic.get("beginDate"),
        "end_date": basic.get("endDate"),
        "issue_date": basic.get("issuDate") or basic.get("issueDate"),
        "usage": _safe_float(basic.get("monthPq", basic.get("totalPq", month_item.get("pq")))),
        "charge": _safe_float(basic.get("monthAmt", basic.get("totalAmt", month_item.get("amt")))),
        "year_usage": _safe_float(basic.get("yearPq")),
        "year_charge": _safe_float(basic.get("yearAmt")),
        "valley_usage": _safe_float(pv_qty.get("valQty")),
        "flat_usage": _safe_float(pv_qty.get("flatQty")),
        "peak_usage": _safe_float(pv_qty.get("peakQty")),
        "tip_usage": _safe_float(pv_qty.get("sharpQty")),
        "one_level_pq": _safe_float(tier_source.get("oneLevelPq")),
        "two_level_pq": _safe_float(tier_source.get("twoLevelPq")),
        "total_year_pq": _safe_float(tier_source.get("totalYearPq")),
        "total_month_pq": _safe_float(tier_source.get("totalMonthPq")),
        "year_used_pq": _safe_float(tier_source.get("yearUsedPq")),
        "level_flag": _safe_float(tier_source.get("levelFlag"), default=None),
        "ladder": tier_source.get("ladder"),
        "ladder_num": tier_source.get("laddernum") or tier_source.get("ladderNum"),
        "bill_settle_type": chosen.get("billSettleType") or basic.get("billSettleType"),
        "charge_segments": _bill_charge_segments(chosen.get("prcGroupList") or []),
        "raw": chosen,
    }


def normalize_tier_snapshot(components: list[dict[str, Any]]) -> dict[str, Any]:
    """从阶梯用电页面提取官方阶梯状态。"""
    step = (
        _first_data_value(components, "stepObj")
        or _first_data_value(components, "electricParticulars")
        or _first_data_value(components, "setBilling")
        or {}
    )
    if not isinstance(step, dict):
        step = {}
    other = _first_data_value(components, "other") or {}
    if not isinstance(other, dict):
        other = {}
    user_d = _first_data_value(components, "userD") or {}
    soc = _first_data_value(components, "soc") or {}
    ladder_num = other.get("laddernum") or other.get("ladderNum") or ""
    official_tier = {
        "一": 1,
        "二": 2,
        "三": 3,
        "1": 1,
        "2": 2,
        "3": 3,
    }.get(str(ladder_num).strip(), None)
    return {
        "level_flag": _safe_float(step.get("levelFlag"), default=None),
        "one_level_pq": _safe_float(step.get("oneLevelPq")),
        "two_level_pq": _safe_float(step.get("twoLevelPq")),
        "total_year_pq": _safe_float(step.get("totalYearPq")),
        "total_month_pq": _safe_float(step.get("totalMonthPq")),
        "year_used_pq": _safe_float(step.get("yearUsedPq")),
        "ladder": other.get("ladder"),
        "ladder_num": ladder_num,
        "official_tier": official_tier,
        "year_used_pq_ui": _safe_float(other.get("yearUsedPq")),
        "user_d": user_d,
        "soc": soc,
        "yearstep": _first_data_value(components, "yearstep"),
        "newyear": _first_data_value(components, "newyear"),
        "newmonthdate": _first_data_value(components, "newmonthdate"),
        "bill_settle_type": _first_data_value(components, "billSettleType"),
        "raw": {"step": step, "other": other, "userD": user_d, "soc": soc},
    }


def _bill_charge_segments(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for item in group.get("amtList") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("amtName") or "")
            segments.append({
                "name": name,
                "usage": _safe_float(item.get("pq")),
                "price": _safe_float(item.get("price")),
                "charge": _safe_float(item.get("amount")),
                "period": _period_from_name(name),
            })
    return segments


def _period_from_name(name: str) -> Optional[str]:
    if "尖" in name:
        return "tip"
    if "峰" in name:
        return "peak"
    if "平" in name:
        return "flat"
    if "谷" in name:
        return "valley"
    return None


def _first_data_value(components: list[dict[str, Any]], key: str) -> Any:
    for component in components:
        data = component.get("data") or {}
        if key in data:
            return data[key]
    return None


def _data_values(components: list[dict[str, Any]], key: str) -> list[Any]:
    return [(component.get("data") or {}).get(key) for component in components if key in (component.get("data") or {})]


def _normalize_usage_month(row: dict[str, Any]) -> dict[str, Any]:
    total = _safe_float(row.get("monthEleNum"))
    charge = _safe_float(row.get("monthEleCost"))
    return {
        "month": _normalize_ym(row.get("month")),
        "total_usage": total,
        "total_charge": charge,
        "begin_date": row.get("begDate"),
        "end_date": row.get("endDate"),
        "meter_read_time": row.get("mrDate"),
        "is_max": bool(row.get("max")),
    }


def _normalize_daily_row(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    date = str(row.get("day") or "").strip()
    if not date:
        return None
    return {
        "date": date,
        "total_usage": _safe_float(row.get("dayElePq"), default=None),
        "valley_usage": _safe_float(row.get("thisVPq"), default=None),
        "flat_usage": _safe_float(row.get("thisNPq"), default=None),
        "peak_usage": _safe_float(row.get("thisPPq"), default=None),
        "tip_usage": _safe_float(row.get("thisTPq"), default=None),
    }


def _normalize_ym(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if len(text) == 6 and text.isdigit():
        return f"{text[:4]}-{text[4:]}"
    if len(text) >= 7:
        return text[:7]
    return text


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        text = str(value).strip()
        if text in ("", "-", "—", "None"):
            return default
        return float(text)
    except (TypeError, ValueError):
        return default
