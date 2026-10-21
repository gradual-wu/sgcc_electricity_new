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
        object_id = f"{sensor_key}_{user_id[-4:]}"
        topic = f"homeassistant/sensor/{object_id}/config"
        for published_topic, payload, _ in reversed(self.published):
            if published_topic == topic:
                return payload
        return None


def _build_updator(tmp_path, monkeypatch):
    monkeypatch.setenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
    monkeypatch.setenv("MQTT_STATE_PREFIX", "sgcc_electricity")
    monkeypatch.setenv("CLEANUP_LEGACY_REST_STATES", "false")
    monkeypatch.setenv("PUSH_TYPE", "None")
    fake = FakeMqttPublisher()
    updator = SensorUpdator(mqtt_publisher=fake, cache_file=str(tmp_path / "cache.json"))
    return updator, fake


def test_ha_discovery_payload_uses_legacy_entity_ids():
    fake = FakeMqttPublisher()
    publisher = HaMqttPublisher(fake, availability_topic="sgcc_electricity/status")
    payload = publisher.build_discovery_payload(
        "last_electricity_usage", "1234567890123", user_name="家庭用电"
    )
    assert payload["unique_id"] == "sensor.last_electricity_usage_0123"
    assert payload["object_id"] == "last_electricity_usage_0123"
    assert payload["default_entity_id"] == "sensor.last_electricity_usage_0123"
    assert payload["state_topic"] == "sgcc_electricity/1234567890123/last_electricity_usage/state"
    assert payload["device"]["identifiers"] == ["sgcc_electricity_1234567890123"]
    assert payload["device"]["name"] == "国网电费 家庭用电"
    assert payload.get("state_class") == "measurement"
    assert "last_reset_value_template" not in payload

    month_payload = publisher.build_discovery_payload(
        "month_electricity_usage", "1234567890123"
    )
    assert month_payload["state_class"] == "measurement"
    assert "last_reset_value_template" not in month_payload


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


def test_all_sensor_specs_have_required_fields():
    for key, spec in SENSOR_SPECS.items():
        assert spec["label"]
        assert spec["unit"]
        assert spec["device_class"]
        assert "icon" in spec
