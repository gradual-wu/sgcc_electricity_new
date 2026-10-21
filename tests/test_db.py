import os
import sqlite3
from datetime import date

from db import SqliteDB


def _make_db(tmp_path, monkeypatch, user_id="1234567890123"):
    monkeypatch.setenv("DB_NAME", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATA_RETENTION_DAYS", "0")
    db = SqliteDB()
    assert db.connect_user_db(user_id) is True
    return db


def test_sqlite_crud_and_tou_merge(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        assert db.upsert_user("1234567890123", "13800000000", "家庭用电") is True

        # DOM 先写入总量，Vue 再补充分时；分时字段不能丢掉总量
        assert db.insert_daily_data({
            "date": "2026-10-19",
            "total_usage": 16.06,
        }) is True
        assert db.insert_daily_data({
            "date": "2026-10-19",
            "total_usage": 16.06,
            "valley_usage": 5.1,
            "flat_usage": 4.2,
            "peak_usage": 5.7,
            "tip_usage": 1.06,
        }) is True

        cursor = db.connect.cursor()
        cursor.execute(
            "SELECT total_usage, valley_usage, flat_usage, peak_usage, tip_usage "
            "FROM sg_daily_usage WHERE user_id=? AND stat_date=?",
            ("1234567890123", "2026-10-19"),
        )
        row = cursor.fetchone()
        assert row == (16.06, 5.1, 4.2, 5.7, 1.06)

        # 月度：官方账单优先，后续页面计算值不能覆盖
        assert db.insert_monthly_data({
            "month": "2026-09",
            "total_usage": 131,
            "total_charge": 78.6,
            "source": "calculated",
        }) is True
        assert db.insert_monthly_data({
            "month": "2026-09",
            "total_usage": 131,
            "total_charge": 78.6,
            "valley_usage": 50,
            "flat_usage": 40,
            "peak_usage": 35,
            "tip_usage": 6,
            "source": "official",
        }) is True
        assert db.insert_monthly_data({
            "month": "2026-09",
            "total_usage": 999,
            "source": "calculated",
        }) is True

        cursor.execute(
            "SELECT total_usage, valley_usage, source FROM sg_monthly_usage "
            "WHERE user_id=? AND stat_month=?",
            ("1234567890123", "2026-09"),
        )
        assert cursor.fetchone() == (131.0, 50.0, "official")

        assert db.insert_yearly_data({
            "year": "2026",
            "total_usage": 556,
            "total_charge": 333.6,
            "source": "calculated",
        }) is True
        assert db.insert_balance_log({
            "balance": 729.68,
            "amount_due": 0.0,
            "as_of": "2026-10-20 08:00:00",
        }) is True

        cursor.execute("SELECT balance, amount_due FROM sg_balance_log")
        assert cursor.fetchone() == (729.68, 0.0)
        cursor.close()
    finally:
        db.close_connect()


def test_sqlite_retention_zero_keeps_data(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        assert db.insert_daily_data({"date": "2020-01-01", "total_usage": 1.0}) is True
        db.cleanup_old_data()
        cursor = db.connect.cursor()
        cursor.execute("SELECT COUNT(*) FROM sg_daily_usage")
        assert cursor.fetchone()[0] == 1
        cursor.close()
    finally:
        db.close_connect()


def test_sqlite_retention_positive_cleans_old_daily(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        assert db.insert_daily_data({"date": "2020-01-01", "total_usage": 1.0}) is True
        monkeypatch.setenv("DATA_RETENTION_DAYS", "30")
        db.cleanup_old_data()
        cursor = db.connect.cursor()
        cursor.execute("SELECT COUNT(*) FROM sg_daily_usage")
        assert cursor.fetchone()[0] == 0
        cursor.close()
    finally:
        db.close_connect()


def test_sqlite_legacy_migration(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    user_id = "1234567890123"
    legacy = sqlite3.connect(db_path)
    legacy.execute(f"CREATE TABLE daily{user_id} (date DATE, usage REAL)")
    legacy.execute(f"INSERT INTO daily{user_id} VALUES ('2026-10-19', 16.06)")
    legacy.execute(f"CREATE TABLE data{user_id} (name TEXT, value TEXT)")
    legacy.executemany(
        f"INSERT INTO data{user_id} VALUES (?, ?)",
        [
            ("user_info", f"{user_id}|13800000000|家庭用电"),
            ("balance_latest", "729.68|家庭用电|2026-10-20 08:00:00|0.0"),
            ("month_2026-09", "131.0|78.6|50|40|35|6|家庭用电"),
            ("year_2026", "556.0|333.6|家庭用电"),
        ],
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("DB_NAME", str(db_path))
    monkeypatch.setenv("DATA_RETENTION_DAYS", "0")
    db = SqliteDB()
    assert db.connect_user_db(user_id) is True
    try:
        cursor = db.connect.cursor()
        cursor.execute("SELECT total_usage FROM sg_daily_usage")
        assert cursor.fetchone()[0] == 16.06
        cursor.execute("SELECT total_usage, valley_usage, source FROM sg_monthly_usage")
        assert cursor.fetchone() == (131.0, 50.0, "legacy")
        cursor.execute("SELECT total_usage, total_charge FROM sg_yearly_usage")
        assert cursor.fetchone() == (556.0, 333.6)
        cursor.execute("SELECT user_name FROM sg_users WHERE user_id=?", (user_id,))
        assert cursor.fetchone()[0] == "家庭用电"
        cursor.close()
    finally:
        db.close_connect()


def test_sqlite_payment_dedup_and_latest(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        assert db.insert_payment({
            "pay_date": "2026-09-05 10:30:00",
            "amount": 100.0,
            "pay_mode": "支付宝",
        }) is True
        assert db.insert_payment({
            "pay_date": "2026-10-01 08:00:00",
            "amount": 200.0,
            "pay_mode": "微信",
        }) is True
        # 同一天、同金额、同渠道重复写入不会产生第二条
        assert db.insert_payment({
            "pay_date": "2026-10-01 08:00:00",
            "amount": 200.0,
            "pay_mode": "微信",
        }) is True
        latest = db.get_latest_payment()
        assert latest["pay_date"] == "2026-10-01 08:00:00"
        assert latest["amount"] == 200.0
        assert latest["pay_mode"] == "微信"
        cursor = db.connect.cursor()
        cursor.execute("SELECT COUNT(*) FROM sg_payments")
        assert cursor.fetchone()[0] == 2
        cursor.close()
    finally:
        db.close_connect()


def test_sqlite_monthly_bill_and_official_months(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        assert db.insert_monthly_bill({
            "month": "2026-09",
            "total_usage": 131.0,
            "total_charge": 78.6,
            "valley_usage": 50,
            "flat_usage": 40,
            "peak_usage": 35,
            "tip_usage": 6,
            "year_usage": 556.0,
            "year_charge": 333.6,
            "one_level_pq": 2160,
            "two_level_pq": 2040,
            "level_flag": 2,
            "ladder_num": "二",
            "begin_date": "2026-09-01",
            "end_date": "2026-09-30",
            "issue_date": "2026-10-01",
        }) is True
        bill = db.get_monthly_bill("2026-09")
        assert bill["total_usage"] == 131.0
        assert bill["one_level_pq"] == 2160.0
        assert bill["level_flag"] == 2
        assert db.has_official_bill("2026-09") is True
        assert db.get_official_bill_months("2026") == {"2026-09"}
    finally:
        db.close_connect()


def _seed_monthly(db, year, months, usage_per_month, source="official"):
    for month in months:
        db.insert_monthly_data({
            "month": f"{year}-{month:02d}",
            "total_usage": usage_per_month,
            "total_charge": 0,
            "source": source,
        })


def test_sqlite_tier_state_uses_history_plus_current_daily(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        _seed_monthly(db, 2026, range(1, 10), 250.0)
        for day in range(1, 21):
            db.insert_daily_data({
                "date": f"2026-10-{day:02d}",
                "total_usage": 15.0,
            })
        state = db.get_tier_state(2160, 4200, today=date(2026, 10, 21))
        assert state["previous_months_usage"] == 2250.0
        assert state["current_month_usage"] == 300.0
        assert state["total_usage"] == 2550.0
        assert state["tier"] == 2
        assert state["tier_name"] == "第二阶梯"
        assert state["range_label"] == "2161-4200"
        assert state["remaining_usage"] == 1650.0
        assert state["current_month_source"] == "daily_sum"
    finally:
        db.close_connect()


def test_sqlite_tier_state_boundaries_and_third_tier(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        _seed_monthly(db, 2026, range(1, 10), 500.0)
        state = db.get_tier_state(2160, 4200, today=date(2026, 10, 21))
        assert state["total_usage"] == 4500.0
        assert state["tier"] == 3
        assert state["range_label"] == "4201以上"
        assert state["remaining_usage"] == 0.0
    finally:
        db.close_connect()


def test_sqlite_tier_state_boundary_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_NAME", str(tmp_path / "boundary.db"))
    db = _make_db(tmp_path, monkeypatch)
    try:
        db.insert_monthly_data({"month": "2026-01", "total_usage": 2160.0, "source": "official"})
        state = db.get_tier_state(2160, 4200, today=date(2026, 2, 1))
        assert state["tier"] == 1
        assert state["remaining_usage"] == 0.0
    finally:
        db.close_connect()


def test_sqlite_calculated_month_from_daily_and_yearly_refresh(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        db.insert_daily_data({"date": "2026-09-01", "total_usage": 10.0, "valley_usage": 4.0})
        db.insert_daily_data({"date": "2026-09-02", "total_usage": 20.0, "flat_usage": 8.0})
        assert db.sync_calculated_month_from_daily("2026-09") is True
        row = db.get_monthly_usage_rows("2026")[0]
        assert row["month"] == "2026-09"
        assert row["total_usage"] == 30.0
        assert row["valley_usage"] == 4.0
        assert row["source"] == "calculated"
        assert db.refresh_yearly_from_monthly("2026") is True
        cursor = db.connect.cursor()
        cursor.execute("SELECT total_usage FROM sg_yearly_usage WHERE stat_year='2026'")
        assert cursor.fetchone()[0] == 30.0
        cursor.close()
    finally:
        db.close_connect()


def test_sqlite_daily_dates_reports_present_dates(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        db.insert_daily_data({"date": "2026-09-01", "total_usage": 1.0})
        db.insert_daily_data({"date": "2026-09-03", "total_usage": 1.0})
        assert db.get_daily_dates("2026-09-01", "2026-09-03") == {
            "2026-09-01",
            "2026-09-03",
        }
    finally:
        db.close_connect()


def test_sqlite_daily_sync_preserves_page_month_totals(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    try:
        db.insert_monthly_data({
            "month": "2026-09",
            "total_usage": 131.0,
            "total_charge": 78.6,
            "source": "calculated",
        })
        db.insert_daily_data({"date": "2026-09-01", "total_usage": 10.0, "valley_usage": 4.0})
        db.insert_daily_data({"date": "2026-09-02", "total_usage": 20.0, "flat_usage": 8.0})
        assert db.sync_calculated_month_from_daily("2026-09") is True
        row = db.get_monthly_usage_row("2026-09")
        assert row["total_usage"] == 131.0
        assert row["total_charge"] == 78.6
        assert row["valley_usage"] == 4.0
        assert row["flat_usage"] == 8.0
        assert row["source"] == "calculated"
    finally:
        db.close_connect()
