"""查看/修复数据库并从数据库发布 HA，不访问国家电网。

用法：
    python scripts/db_status.py                 # 查看所有户号
    python scripts/db_status.py --user-id ...   # 只看一个户号
    python scripts/db_status.py --repair        # 用日数据刷新当月月度/年度汇总
    python scripts/db_status.py --publish       # 从数据库推送 HA
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

import dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import MysqlDB, SqliteDB  # noqa: E402
from sensor_updator import SensorUpdator  # noqa: E402


def build_db():
    db_type = os.getenv("DB_TYPE", "NONE").lower()
    if db_type == "mysql":
        return MysqlDB()
    if db_type == "sqlite":
        return SqliteDB()
    return None


def _jsonable(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def show_user(db, user_id, repair=False):
    if not db.connect_user_db(user_id):
        print(f"[{user_id}] 数据库连接失败")
        return
    try:
        user = db.get_user(user_id) or {}
        print(f"\n===== {user_id} {user.get('user_name') or ''} 数据更新时间={user.get('data_updated_at') or '-'} =====")

        latest_daily = db.get_latest_daily()
        daily_summary = None
        if latest_daily:
            year_start = f"{datetime.now().year}-01-01"
            daily_summary = db.get_daily_summary(year_start, latest_daily["date"])
        print("daily:", json.dumps(_jsonable({
            "latest": latest_daily,
            "year_summary": daily_summary,
        }), ensure_ascii=False))

        monthly = db.get_monthly_usage_rows(str(datetime.now().year))
        print("monthly:", json.dumps(_jsonable(monthly), ensure_ascii=False))

        bills = []
        for row in monthly:
            bill = db.get_monthly_bill(row.get("month"))
            if bill:
                bills.append({
                    "month": bill.get("stat_month"),
                    "usage": bill.get("total_usage"),
                    "charge": bill.get("total_charge"),
                    "level_flag": bill.get("level_flag"),
                    "one_level_pq": bill.get("one_level_pq"),
                    "two_level_pq": bill.get("two_level_pq"),
                    "ladder_num": bill.get("ladder_num"),
                })
        print("official_bills:", json.dumps(_jsonable(bills), ensure_ascii=False))

        print("payment:", json.dumps(_jsonable(db.get_latest_payment()), ensure_ascii=False))
        print("tier_snapshot:", json.dumps(_jsonable(db.get_latest_tier_snapshot()), ensure_ascii=False))
        print("yearly:", json.dumps(_jsonable(db.get_yearly_row(str(datetime.now().year))), ensure_ascii=False))
        print("balance:", json.dumps(_jsonable(db.get_latest_balance()), ensure_ascii=False))
        print("tier_state:", json.dumps(_jsonable(db.get_tier_state()), ensure_ascii=False))

        if repair:
            month = datetime.now().strftime("%Y-%m")
            year = str(datetime.now().year)
            if not db.has_official_bill(month):
                db.sync_calculated_month_from_daily(month)
            db.refresh_yearly_from_monthly(year)
            print("repaired_monthly:", json.dumps(_jsonable(db.get_monthly_usage_row(month)), ensure_ascii=False))
            print("repaired_yearly:", json.dumps(_jsonable(db.get_yearly_row(year)), ensure_ascii=False))
    finally:
        db.close_connect()


def _closed_months(year: int) -> list[str]:
    today = date.today()
    last_closed = today.replace(day=1) - timedelta(days=1)
    months = []
    cursor = date(year, 1, 1)
    while cursor <= last_closed:
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    if last_closed.year < year:
        months.insert(0, last_closed.strftime("%Y-%m"))
    return list(dict.fromkeys(months))


def _group_dates(dates: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    for text in dates:
        current = date.fromisoformat(text)
        if groups and current == date.fromisoformat(groups[-1][1]) + timedelta(days=1):
            groups[-1][1] = text
        else:
            groups.append([text, text])
    return groups


def show_missing(db, user_id: str) -> None:
    if not db.connect_user_db(user_id):
        print(f"[{user_id}] 数据库连接失败")
        return
    try:
        today = date.today()
        start = date(today.year, 1, 1)
        end = today - timedelta(days=1)
        present = db.get_daily_dates(start.isoformat(), end.isoformat())
        missing = []
        cursor = start
        while cursor <= end:
            if cursor.isoformat() not in present:
                missing.append(cursor.isoformat())
            cursor += timedelta(days=1)
        missing_bills = [
            month for month in _closed_months(today.year)
            if not db.has_official_bill(month)
        ]
        payment = db.get_latest_payment()
        tier = db.get_latest_tier_snapshot()
        print(f"\n===== {user_id} 缺失数据 =====")
        print(json.dumps(_jsonable({
            "daily_missing_ranges": _group_dates(missing),
            "daily_missing_count": len(missing),
            "missing_bills": missing_bills,
            "payment_missing": payment is None,
            "tier_snapshot_missing": (
                tier is None
                or str(tier.get("snapshot_date") or "")[:7] != today.strftime("%Y-%m")
            ),
        }), ensure_ascii=False))
    finally:
        db.close_connect()


def main():
    parser = argparse.ArgumentParser(description="查看/修复数据库并从数据库发布 HA")
    parser.add_argument("--user-id", help="只处理指定户号")
    parser.add_argument("--missing", action="store_true", help="只扫描缺失的日数据/账单/交费/阶梯")
    parser.add_argument("--repair", action="store_true", help="用日数据刷新当月月度与年度汇总")
    parser.add_argument("--publish", action="store_true", help="从数据库推送 HA")
    args = parser.parse_args()

    dotenv.load_dotenv(verbose=True)
    db = build_db()
    if db is None:
        print("DB_TYPE 不是 MYSQL/SQLITE，无法查看数据库")
        return 1

    if not args.user_id:
        if not db.connect_users_db():
            print("数据库连接失败")
            return 1
        try:
            users = db.get_users()
        finally:
            db.close_connect()
        user_ids = [user["user_id"] for user in users]
    else:
        user_ids = [args.user_id]

    for user_id in user_ids:
        if args.missing:
            show_missing(db, user_id)
        else:
            show_user(db, user_id, repair=args.repair)

    if args.publish:
        updator = SensorUpdator()
        try:
            print("publish_all_from_db:", updator.publish_all_from_db(db))
        finally:
            updator.close(publish_offline=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
