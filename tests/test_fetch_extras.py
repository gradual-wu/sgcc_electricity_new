from datetime import date, timedelta

from data_fetcher import DataFetcher


def test_sync_extras_persists_payment_tier_and_reads_previous_bill(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_NAME", str(tmp_path / "extras.db"))
    monkeypatch.setenv("DATA_RETENTION_DAYS", "0")
    monkeypatch.setenv("YEAR_BACKFILL_ENABLED", "false")
    monkeypatch.setenv("TIER1_MAX", "2160")
    monkeypatch.setenv("TIER2_MAX", "4200")

    user_id = "1234567890123"
    today = date.today()
    fetcher = DataFetcher("user", "pass")

    # 预置历史月份和当月日数据，供阶梯计算使用
    assert fetcher.db.connect_user_db(user_id) is True
    for month in range(1, today.month):
        fetcher.db.insert_monthly_data({
            "month": f"{today.year}-{month:02d}",
            "total_usage": 250.0,
            "source": "official",
        })
    for day in range(1, min(today.day, 20) + 1):
        fetcher.db.insert_daily_data({
            "date": f"{today.year}-{today.month:02d}-{day:02d}",
            "total_usage": 15.0,
        })
    previous_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    fetcher.db.insert_monthly_bill({
        "month": previous_month,
        "total_usage": 131.0,
        "total_charge": 78.6,
        "source": "official",
    })
    fetcher.db.close_connect()

    fetcher._year_backfill_needed = lambda year: False
    fetcher._missing_bill_months = lambda year: []
    fetcher._get_payment_records = lambda driver, uid, index: [{
        "pay_date": f"{today.year}-10-01 08:00:00",
        "amount": 200.0,
        "pay_mode": "微信",
    }]
    fetcher._get_tier_snapshot = lambda driver, uid, index: {
        "snapshot_date": today.isoformat(),
        "level_flag": 2,
        "one_level_pq": 2160.0,
        "two_level_pq": 2040.0,
        "total_year_pq": 1000.0,
        "ladder": "年",
        "ladder_num": "一",
        "official_tier": 1,
        "source": "official",
    }

    fetcher._sync_extras(None, user_id, 0, user_name="家庭用电")

    # 抓取流程只写库；HA 发布会从数据库读取
    assert fetcher.db.connect_user_db(user_id) is True
    try:
        assert fetcher.db.get_latest_payment()["amount"] == 200.0
        assert fetcher.db.get_monthly_bill(previous_month)["total_usage"] == 131.0
        snapshot = fetcher.db.get_latest_tier_snapshot()
        assert snapshot["official_tier"] == 1
        assert snapshot["level_flag"] == 2
    finally:
        fetcher.db.close_connect()
