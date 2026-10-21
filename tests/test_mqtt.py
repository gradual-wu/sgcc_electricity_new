import json
from datetime import date, datetime, timedelta

from date_utils import normalize_date, normalize_month
from ha_mqtt import SENSOR_SPECS, HaMqttPublisher
from sensor_updator import SensorUpdator


class FakeMqttPublisher:
    def __init__(self):
        self.published = []
        self.closed = False

    def enabled(self):
        return True

    def publish(self, topic, payload, retain=None):
        self.published.append((topic, payload, retain))
        return True

    def close(self):
        self.closed = True

    def state_payload(self, user_id, sensor_key):
        topic = f"sgcc_electricity/{user_id}/{sensor_key}/state"
        for published_topic, payload, _ in reversed(self.published):
            if published_topic == topic:
                return payload
        return None

    def discovery_payload(self, user_id, sensor_key):
        state_topic = f"sgcc_electricity/{user_id}/{sensor_key}/state"
        for published_topic, payload, _ in reversed(self.published):
            if (
                published_topic.startswith("homeassistant/")
                and published_topic.endswith("/config")
                and isinstance(payload, dict)
                and payload.get("state_topic") == state_topic
            ):
                return payload
        return None


def _build_updator(tmp_path, monkeypatch):
    monkeypatch.setenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
    monkeypatch.setenv("MQTT_STATE_PREFIX", "sgcc_electricity")
    monkeypatch.setenv("CLEANUP_LEGACY_REST_STATES", "false")
    monkeypatch.setenv("CLEANUP_LEGACY_ENTITY_IDS", "false")
    monkeypatch.setenv("ENTITY_ID_STYLE", "none")
    monkeypatch.setenv("PUSH_TYPE", "None")
    fake = FakeMqttPublisher()
    updator = SensorUpdator(mqtt_publisher=fake, cache_file=str(tmp_path / "cache.json"))
    return updator, fake


def test_ha_discovery_payload_uses_legacy_entity_ids():
    fake = FakeMqttPublisher()
    publisher = HaMqttPublisher(
        fake, availability_topic="sgcc_electricity/status", entity_id_style="last4"
    )
    payload = publisher.build_discovery_payload(
        "last_electricity_usage", "1234567890123", user_name="家庭用电"
    )
    assert payload["unique_id"] == "sensor.last_electricity_usage_0123"
    assert payload["object_id"] == "last_electricity_usage_0123"
    assert payload["default_entity_id"] == "sensor.last_electricity_usage_0123"
    assert payload["state_topic"] == "sgcc_electricity/1234567890123/last_electricity_usage/state"
    assert payload["device"]["identifiers"] == ["sgcc_electricity_1234567890123"]
    assert payload["device"]["name"] == "国网电费 家庭用电"
    assert payload["device"]["serial_number"] == "1234567890123"
    assert payload.get("state_class") == "measurement"
    assert "last_reset_value_template" not in payload

    month_payload = publisher.build_discovery_payload(
        "month_electricity_usage", "1234567890123"
    )
    assert month_payload["state_class"] == "measurement"
    assert "last_reset_value_template" not in month_payload


def test_ha_discovery_payload_uses_full_user_id():
    fake = FakeMqttPublisher()
    publisher = HaMqttPublisher(fake, entity_id_style="full")
    payload = publisher.build_discovery_payload(
        "last_electricity_usage", "1234567890123", user_name="家庭用电"
    )
    assert payload["unique_id"] == "sensor.last_electricity_usage_1234567890123"
    assert payload["object_id"] == "last_electricity_usage_1234567890123"
    assert payload["default_entity_id"] == "sensor.last_electricity_usage_1234567890123"


def test_ha_discovery_payload_uses_no_suffix():
    fake = FakeMqttPublisher()
    publisher = HaMqttPublisher(fake, entity_id_style="none")
    payload = publisher.build_discovery_payload(
        "last_electricity_usage", "1234567890123", user_name="家庭用电"
    )
    assert payload["unique_id"] == "sensor.last_electricity_usage"
    assert payload["object_id"] == "last_electricity_usage"
    assert payload["default_entity_id"] == "sensor.last_electricity_usage"
    assert payload["device"]["identifiers"] == ["sgcc_electricity_1234567890123"]


def test_sensor_updator_publishes_balance_usage_and_tou(tmp_path, monkeypatch):
    updator, fake = _build_updator(tmp_path, monkeypatch)
    user_id = "1234567890123"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    current_month = datetime.now().strftime("%Y-%m")

    updator.update_one_userid(
        user_id=user_id,
        balance=729.68,
        last_daily_date=yesterday,
        last_daily_usage=16.06,
        yearly_charge=333.6,
        yearly_usage=556.0,
        month_charge=78.6,
        month_usage=131.0,
        tou_data={
            "daily": [{
                "date": yesterday,
                "total_usage": 16.06,
                "valley_usage": 5.1,
                "flat_usage": 4.2,
                "peak_usage": 5.7,
                "tip_usage": 1.06,
            }]
        },
        bill_tou_data={
            "month": current_month,
            "usage": 131.0,
            "charge": 78.6,
            "valley_usage": 50.0,
            "flat_usage": 40.0,
            "peak_usage": 35.0,
            "tip_usage": 6.0,
        },
        enhanced_balance={"amount_due": 0.0, "as_of": "2026-10-20 08:00:00"},
        user_name="家庭用电",
        month_period="2026-09",
    )

    assert fake.state_payload(user_id, "electricity_charge_balance")["state"] == 729.68
    assert fake.state_payload(user_id, "electricity_charge_balance")["user_id"] == user_id
    assert fake.state_payload(user_id, "electricity_charge_balance")["user_name"] == "家庭用电"
    daily = fake.state_payload(user_id, "last_electricity_usage")
    assert daily["state"] == 16.06
    assert daily["last_daily_date"] == yesterday
    assert fake.state_payload(user_id, "last_valley_electricity_usage")["state"] == 5.1
    assert fake.state_payload(user_id, "last_flat_electricity_usage")["state"] == 4.2
    assert fake.state_payload(user_id, "last_peak_electricity_usage")["state"] == 5.7
    assert fake.state_payload(user_id, "last_tip_electricity_usage")["state"] == 1.06

    monthly_peak = fake.state_payload(user_id, "month_peak_usage")
    assert monthly_peak["state"] == 35.0
    assert monthly_peak["period"] == current_month
    assert monthly_peak["source"] == "official"
    assert fake.state_payload(user_id, "month_electricity_usage")["period"] == "2026-09"

    assert fake.discovery_payload(user_id, "month_tip_usage")["state_class"] == "measurement"
    assert fake.discovery_payload(user_id, "prepay_balance")["device_class"] == "monetary"
    assert fake.state_payload(user_id, "prepay_balance")["state"] == 0.0


def test_monthly_tou_falls_back_to_daily_sum(tmp_path, monkeypatch):
    updator, fake = _build_updator(tmp_path, monkeypatch)
    user_id = "1234567890123"
    today = date.today()
    rows = [
        {
            "date": today.replace(day=1).isoformat(),
            "valley_usage": 1.0,
            "flat_usage": 2.0,
            "peak_usage": 3.0,
            "tip_usage": 4.0,
        }
    ]
    updator.update_one_userid(
        user_id=user_id,
        balance=100,
        last_daily_date=today.isoformat(),
        last_daily_usage=10,
        yearly_charge=None,
        yearly_usage=None,
        month_charge=None,
        month_usage=None,
        tou_data={"daily": rows},
        bill_tou_data={"month": "2020-01", "valley_usage": 99},
    )
    payload = fake.state_payload(user_id, "month_valley_usage")
    assert payload["state"] == 1.0
    assert payload["source"] == "daily_sum"


def test_republish_uses_cache(tmp_path, monkeypatch):
    updator, fake = _build_updator(tmp_path, monkeypatch)
    user_id = "1234567890123"
    updator.update_one_userid(
        user_id=user_id,
        balance=100,
        last_daily_date=date.today().isoformat(),
        last_daily_usage=10,
        yearly_charge=None,
        yearly_usage=None,
        month_charge=None,
        month_usage=None,
    )
    assert updator.republish() is True
    assert any(topic.startswith("homeassistant/sensor/") for topic, _, _ in fake.published)


def test_date_utils_handles_state_grid_formats():
    today = date.today()
    expected_year = today.year if date(today.year, 10, 19) <= today + timedelta(days=1) else today.year - 1
    assert normalize_date("2026-10-19") == "2026-10-19"
    assert normalize_date("2026/10/19") == "2026-10-19"
    assert normalize_date("10月19日") == f"{expected_year}-10-19"
    assert normalize_month("1月1日-1月31日") == f"{today.year}-01"
    assert normalize_month("2026-09") == "2026-09"
    assert normalize_month("202609") == "2026-09"


def test_sensor_updator_publishes_payment_and_tier(tmp_path, monkeypatch):
    updator, fake = _build_updator(tmp_path, monkeypatch)
    user_id = "1234567890123"
    tier_state = {
        "year": "2026",
        "month": "2026-10",
        "previous_months_usage": 2250.0,
        "current_month_usage": 300.0,
        "total_usage": 2550.0,
        "tier": 2,
        "tier_name": "第二阶梯",
        "range_start": 2160.0,
        "range_end": 4200.0,
        "range_label": "2161-4200",
        "remaining_usage": 1650.0,
        "used_in_tier": 390.0,
        "progress_percent": 19.12,
        "thresholds": {"tier1_max": 2160.0, "tier2_max": 4200.0},
        "current_month_source": "daily_sum",
    }
    tier_official = {
        "snapshot_date": "2026-10-21",
        "level_flag": 2,
        "one_level_pq": 2160.0,
        "two_level_pq": 2040.0,
        "total_year_pq": 2550.0,
        "ladder": "年",
        "ladder_num": "二",
        "official_tier": 2,
    }
    month_bill = {
        "stat_month": "2026-09",
        "total_usage": 131.0,
        "total_charge": 78.6,
        "valley_usage": 50.0,
        "flat_usage": 40.0,
        "peak_usage": 35.0,
        "tip_usage": 6.0,
        "begin_date": "2026-09-01",
        "end_date": "2026-09-30",
        "issue_date": "2026-10-01",
        "year_usage": 556.0,
        "year_charge": 333.6,
        "level_flag": 2,
        "one_level_pq": 2160.0,
        "two_level_pq": 2040.0,
        "ladder_num": "二",
    }
    updator.update_one_userid(
        user_id=user_id,
        balance=729.68,
        last_daily_date="2026-10-20",
        last_daily_usage=16.06,
        yearly_charge=333.6,
        yearly_usage=556.0,
        month_charge=78.6,
        month_usage=131.0,
        month_period="2026-10",
        payment_record={
            "pay_date": "2026-10-01 08:00:00",
            "amount": 200.0,
            "pay_mode": "微信",
            "total_rep_amt": 0.0,
        },
        month_bill=month_bill,
        tier_state=tier_state,
        tier_official=tier_official,
        user_name="家庭用电",
    )

    payment = fake.state_payload(user_id, "last_payment_amount")
    assert payment["state"] == 200.0
    assert payment["pay_mode"] == "微信"
    assert payment["user_id"] == user_id
    assert payment["user_name"] == "家庭用电"
    payment_date = fake.state_payload(user_id, "last_payment_date")
    assert payment_date["state"] == "2026-10-01T08:00:00+08:00"
    assert fake.discovery_payload(user_id, "last_payment_date")["device_class"] == "timestamp"

    tier = fake.state_payload(user_id, "current_electricity_tier")
    assert tier["state"] == 2
    assert tier["user_id"] == user_id
    assert tier["user_name"] == "家庭用电"
    assert tier["tier_name"] == "第二阶梯"
    assert tier["range"] == "2161-4200"
    assert tier["official_tier"] == 2
    assert fake.state_payload(user_id, "current_tier_usage")["state"] == 2550.0
    assert fake.state_payload(user_id, "current_tier_remaining")["state"] == 1650.0

    month_usage = fake.state_payload(user_id, "month_electricity_usage")
    assert month_usage["state"] == 131.0
    assert month_usage["period"] == "2026-09"
    assert month_usage["source"] == "official"
    assert month_usage["begin_date"] == "2026-09-01"
    assert fake.state_payload(user_id, "month_peak_usage")["period"] == "2026-09"
    assert fake.state_payload(user_id, "month_peak_usage")["source"] == "official"


def test_sensor_updator_publishes_from_database(tmp_path, monkeypatch):
    from datetime import date, timedelta

    from db import SqliteDB

    monkeypatch.setenv("DB_NAME", str(tmp_path / "publish.db"))
    monkeypatch.setenv("DATA_RETENTION_DAYS", "0")
    user_id = "1234567890123"
    today = date.today()
    previous_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    yesterday = (today - timedelta(days=1)).isoformat()

    db = SqliteDB()
    assert db.connect_user_db(user_id) is True
    try:
        db.upsert_user(user_id, "13800000000", "家庭用电")
        db.insert_balance_log({
            "balance": 729.68,
            "amount_due": 0.0,
            "as_of": f"{today.isoformat()} 10:00:00",
        })
        db.insert_daily_data({
            "date": yesterday,
            "total_usage": 16.06,
            "valley_usage": 5.1,
            "flat_usage": 4.2,
            "peak_usage": 5.7,
            "tip_usage": 1.06,
        })
        db.insert_monthly_bill({
            "month": previous_month,
            "total_usage": 131.0,
            "total_charge": 78.6,
            "valley_usage": 50.0,
            "flat_usage": 40.0,
            "peak_usage": 35.0,
            "tip_usage": 6.0,
            "year_usage": 556.0,
            "year_charge": 333.6,
            "level_flag": 2,
            "one_level_pq": 2160.0,
            "two_level_pq": 2040.0,
            "ladder_num": "二",
        })
        db.insert_monthly_data({
            "month": previous_month,
            "total_usage": 131.0,
            "total_charge": 78.6,
            "valley_usage": 50.0,
            "flat_usage": 40.0,
            "peak_usage": 35.0,
            "tip_usage": 6.0,
            "source": "official",
        })
        db.insert_yearly_data({
            "year": str(today.year),
            "total_usage": 556.0,
            "total_charge": 333.6,
            "source": "official",
        })
        db.insert_payment({
            "pay_date": f"{today.isoformat()} 08:30:00",
            "amount": 200.0,
            "pay_mode": "微信",
        })
        db.insert_tier_snapshot({
            "snapshot_date": today.isoformat(),
            "level_flag": 2,
            "one_level_pq": 2160.0,
            "two_level_pq": 2040.0,
            "total_year_pq": 147.06,
            "ladder": "年",
            "ladder_num": "一",
            "official_tier": 1,
        })
        db.touch_user_data(user_id)
    finally:
        db.close_connect()

    updator, fake = _build_updator(tmp_path, monkeypatch)
    assert updator.publish_all_from_db(db, notify=False) is True

    assert fake.state_payload(user_id, "electricity_charge_balance")["state"] == 729.68
    daily = fake.state_payload(user_id, "last_electricity_usage")
    assert daily["state"] == 16.06
    assert daily["last_daily_date"] == yesterday
    assert fake.state_payload(user_id, "last_valley_electricity_usage")["state"] == 5.1
    assert fake.state_payload(user_id, "yearly_electricity_usage")["state"] == 556.0

    month_usage = fake.state_payload(user_id, "month_electricity_usage")
    assert month_usage["state"] == 131.0
    assert month_usage["period"] == previous_month
    assert month_usage["source"] == "official"
    assert fake.state_payload(user_id, "month_peak_usage")["state"] == 35.0

    assert fake.state_payload(user_id, "last_payment_amount")["state"] == 200.0
    assert fake.state_payload(user_id, "last_payment_amount")["user_id"] == user_id
    updated = fake.state_payload(user_id, "data_updated_at")
    assert updated["state"].endswith("+08:00")
    assert fake.discovery_payload(user_id, "data_updated_at")["device_class"] == "timestamp"
    tier = fake.state_payload(user_id, "current_electricity_tier")
    assert tier["state"] == 1
    assert tier["official_tier"] == 1
    expected_tier_usage = (131.0 if today.month > 1 else 0.0) + (16.06 if today.day > 1 else 0.0)
    assert fake.state_payload(user_id, "current_tier_usage")["state"] == expected_tier_usage


def test_all_sensor_specs_have_required_fields():
    unitless = {"last_payment_date", "data_updated_at", "current_electricity_tier"}
    for key, spec in SENSOR_SPECS.items():
        assert spec["label"]
        if key not in unitless:
            assert spec["unit"]
            assert spec["device_class"]
        assert "icon" in spec
