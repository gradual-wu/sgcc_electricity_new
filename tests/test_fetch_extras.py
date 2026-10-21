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


def test_daily_gaps_and_closed_bill_months(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_NAME", str(tmp_path / "gaps.db"))
    monkeypatch.setenv("DATA_RETENTION_DAYS", "0")
    fetcher = DataFetcher("user", "pass")
    user_id = "1234567890123"
    assert fetcher.db.connect_user_db(user_id) is True
    try:
        fetcher.db.insert_daily_data({"date": "2026-09-01", "total_usage": 1.0})
        fetcher.db.insert_daily_data({"date": "2026-09-03", "total_usage": 1.0})
        gaps = fetcher._daily_gaps(date(2026, 9, 1), date(2026, 9, 3))
        assert gaps == [[date(2026, 9, 2), date(2026, 9, 2)]]
    finally:
        fetcher.db.close_connect()

    today = date.today()
    months = fetcher._closed_bill_months(today.year)
    if today.month > 1:
        assert f"{today.year}-01" in months


def test_fill_daily_gaps_only_fetches_missing_range(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_NAME", str(tmp_path / "fill.db"))
    monkeypatch.setenv("DATA_RETENTION_DAYS", "0")
    monkeypatch.setenv("YEAR_BACKFILL_START", "2026-09-01")
    fetcher = DataFetcher("user", "pass")
    user_id = "1234567890123"
    assert fetcher.db.connect_user_db(user_id) is True
    try:
        fetcher.db.insert_daily_data({"date": "2026-09-01", "total_usage": 1.0})
        fetcher.db.insert_daily_data({"date": "2026-09-03", "total_usage": 1.0})
    finally:
        fetcher.db.close_connect()

    calls = []

    def fake_fetch(driver, uid, index, start, end):
        calls.append((start, end))
        return [{"date": "2026-09-02", "total_usage": 5.0}]

    state = {"n": 0}

    def fake_gaps(start, end):
        state["n"] += 1
        if state["n"] == 1:
            return [[date(2026, 9, 2), date(2026, 9, 2)]]
        return []

    fetcher._fetch_daily_range = fake_fetch
    fetcher._daily_gaps = fake_gaps
    rows = fetcher._fill_daily_gaps(None, user_id, 0, 2026)
    assert rows == 1
    assert calls == [("2026-09-02", "2026-09-02")]

    assert fetcher.db.connect_user_db(user_id) is True
    try:
        assert fetcher.db.get_daily_dates("2026-09-01", "2026-09-03") == {
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
        }
        assert fetcher.db.get_monthly_usage_row("2026-09")["total_usage"] == 7.0
        assert fetcher.db.get_yearly_row("2026")["total_usage"] == 7.0
    finally:
        fetcher.db.close_connect()
