"""日期、月份与数值解析工具。

国家电网页面在不同版本/不同入口下会返回多种日期格式，数据库层和
传感器层都需要统一成 ISO 风格的字符串后再使用。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Optional


_DATE_PATTERNS = (
    re.compile(r"^(?P<y>\d{4})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})日?$"),
    re.compile(r"^(?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})日?$"),
    re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})$"),
)

_MONTH_PATTERNS = (
    re.compile(r"^(?P<y>\d{4})[-/.年](?P<m>\d{1,2})月?$"),
    re.compile(r"^(?P<y>\d{4})(?P<m>\d{2})$"),
    re.compile(r"^(?P<m>\d{1,2})月$"),
    # 国网月度表格里常见 "1月1日-1月31日" 这种区间，取起始月份
    re.compile(r"^(?P<m>\d{1,2})月\d{1,2}日\s*[-~至]\s*\d{1,2}月\d{1,2}日$"),
)


def to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """把页面上的数字/带单位文本转成 float。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "—", "None", "null", "null", "暂无"):
        return default
    text = re.sub(r"[^\d.\-+]", "", text)
    if text in ("", "-", "+", "."):
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def normalize_date(value: Any, today: Optional[date] = None) -> Optional[str]:
    """统一日期为 YYYY-MM-DD。

    只有月日时按以下规则推断年份：如果推断出的日期比今天还晚，
    认为它是去年的数据（主要处理跨年时国网仍显示 12-31 的情况）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    today = today or date.today()
    for pattern in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        groups = match.groupdict()
        year = int(groups.get("y") or today.year)
        month = int(groups["m"])
        day = int(groups["d"])
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if not groups.get("y") and candidate > today + timedelta(days=1):
            try:
                candidate = date(year - 1, month, day)
            except ValueError:
                return None
        return candidate.isoformat()
    return None


def normalize_month(value: Any, today: Optional[date] = None) -> Optional[str]:
    """统一月份为 YYYY-MM。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    today = today or date.today()
    # 先处理完整日期（"2026-10-19"、"10月19日" 等），取前 7 位作为月份
    date_text = normalize_date(text, today)
    if date_text:
        return date_text[:7]

    for pattern in _MONTH_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        groups = match.groupdict()
        year = int(groups.get("y") or today.year)
        month = int(groups["m"])
        if not groups.get("y") and month > today.month:
            # 没有年份且月份大于当前月，认为是上一年（常见于 1 月查询上年数据）
            year -= 1
        if not 1 <= month <= 12:
            return None
        return f"{year:04d}-{month:02d}"
    return None


def normalize_year(value: Any, today: Optional[date] = None) -> Optional[str]:
    """统一年份为 YYYY。"""
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{4})", text)
    if match:
        return match.group(1)
    return None


def normalize_datetime(value: Any) -> Optional[str]:
    """把常见时间格式统一为 YYYY-MM-DD HH:MM:SS。"""
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "-", "—", "None", "null"):
        return None
    text = text.replace("T", " ")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text[:19], fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def day_start_iso(value: Any) -> Optional[str]:
    """返回某天的 ISO 时间戳，供 HA 的 last_reset 使用。"""
    normalized = normalize_date(value)
    if normalized is None:
        return None
    return f"{normalized}T00:00:00+08:00"


def month_start_iso(value: Any) -> Optional[str]:
    """返回某月第一天的 ISO 时间戳。"""
    normalized = normalize_month(value)
    if normalized is None:
        return None
    return f"{normalized}-01T00:00:00+08:00"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
