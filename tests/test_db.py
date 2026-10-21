import os
import sqlite3

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
