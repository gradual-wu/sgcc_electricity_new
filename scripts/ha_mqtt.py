"""Home Assistant MQTT Discovery 发布器。

参考 ha-95598 的做法：每个户号对应一个 HA 设备，设备下挂多个 sensor
实体；实体 ID 保持与旧版 REST API 创建的实体一致（例如
sensor.last_electricity_usage_0123），方便从旧接入方式无缝切换。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from mqtt_publisher import MqttPublisher


# state_class 与旧版 REST 实体保持一致，避免 HA 已有的长期统计因 state_class
# 变化而报错/中断：
# - 日度、月度、分时数据是“区间值”，使用 measurement
# - 年度数据是累计值，使用 total_increasing
# - 余额/预付费余额是累计值，使用 total，并通过 last_reset 标记周期起点
SENSOR_SPECS: Dict[str, Dict[str, Any]] = {
    # 余额 / 费用
    "electricity_charge_balance": {
        "label": "电费余额",
        "unit": "CNY",
        "device_class": "monetary",
        "state_class": "total",
        "icon": "mdi:cash",
        "precision": 2,
    },
    "prepay_balance": {
        "label": "预付费余额",
        "unit": "CNY",
        "device_class": "monetary",
        "state_class": "total",
        "icon": "mdi:cash-check",
        "precision": 2,
    },
    # 用电量 / 电费
    "last_electricity_usage": {
        "label": "最新日电量",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:lightning-bolt",
        "precision": 3,
    },
    "yearly_electricity_usage": {
        "label": "本年电量",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
        "icon": "mdi:lightning-bolt",
        "precision": 3,
    },
    "yearly_electricity_charge": {
        "label": "本年电费",
        "unit": "CNY",
        "device_class": "monetary",
        "state_class": "total_increasing",
        "icon": "mdi:cash",
        "precision": 2,
    },
    "month_electricity_usage": {
        "label": "本月电量",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:lightning-bolt",
        "precision": 3,
    },
    "month_electricity_charge": {
        "label": "本月电费",
        "unit": "CNY",
        "device_class": "monetary",
        "state_class": "measurement",
        "icon": "mdi:cash",
        "precision": 2,
    },
    # 分时电量（月度）
    "month_valley_usage": {
        "label": "本月电量-谷",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:weather-night",
        "precision": 3,
    },
    "month_flat_usage": {
        "label": "本月电量-平",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:weather-sunset-up",
        "precision": 3,
    },
    "month_peak_usage": {
        "label": "本月电量-峰",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:chart-bell-curve",
        "precision": 3,
    },
    "month_tip_usage": {
        "label": "本月电量-尖",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:chart-bell-curve-cumulative",
        "precision": 3,
    },
    # 分时电量（最近一天）
    "last_valley_electricity_usage": {
        "label": "最新日电量-谷",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:weather-night",
        "precision": 3,
    },
    "last_flat_electricity_usage": {
        "label": "最新日电量-平",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:weather-sunset-up",
        "precision": 3,
    },
    "last_peak_electricity_usage": {
        "label": "最新日电量-峰",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:chart-bell-curve",
        "precision": 3,
    },
    "last_tip_electricity_usage": {
        "label": "最新日电量-尖",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "measurement",
        "icon": "mdi:chart-bell-curve-cumulative",
        "precision": 3,
    },
}


class HaMqttPublisher:
    def __init__(
        self,
        mqtt_publisher: MqttPublisher,
        discovery_prefix: str = "homeassistant",
        state_prefix: str = "sgcc_electricity",
        availability_topic: str = "",
        device_manufacturer: str = "SGCC Electricity New",
        device_model: str = "sgcc_electricity_new",
    ) -> None:
        self.mqtt = mqtt_publisher
        self.discovery_prefix = (discovery_prefix or "homeassistant").strip("/")
        self.state_prefix = (state_prefix or "sgcc_electricity").strip("/")
        self.availability_topic = (availability_topic or "").strip()
        self.device_manufacturer = device_manufacturer
        self.device_model = device_model
        self._published_discovery: set[str] = set()

    # ------------------------------------------------------------------
    # topics
    # ------------------------------------------------------------------
    @staticmethod
    def sensor_object_id(sensor_key: str, user_id: str) -> str:
        return f"{sensor_key}_{str(user_id)[-4:]}"

    def state_topic(self, sensor_key: str, user_id: str) -> str:
        return f"{self.state_prefix}/{user_id}/{sensor_key}/state"

    def discovery_topic(self, sensor_key: str, user_id: str) -> str:
        object_id = self.sensor_object_id(sensor_key, user_id)
        return f"{self.discovery_prefix}/sensor/{object_id}/config"

    # ------------------------------------------------------------------
    # payloads
    # ------------------------------------------------------------------
    def _device_payload(self, user_id: str, user_name: str = "") -> Dict[str, Any]:
        display_name = user_name.strip() if user_name else ""
        if not display_name:
            display_name = f"户号 {user_id[-4:]}"
        return {
            "identifiers": [f"sgcc_electricity_{user_id}"],
            "name": f"国网电费 {display_name}",
            "manufacturer": self.device_manufacturer,
            "model": self.device_model,
            "configuration_url": "https://github.com/gradual-wu/sgcc_electricity_new",
        }

    def build_discovery_payload(
        self,
        sensor_key: str,
        user_id: str,
        user_name: str = "",
    ) -> Dict[str, Any]:
        spec = SENSOR_SPECS[sensor_key]
        object_id = self.sensor_object_id(sensor_key, user_id)
        state_topic = self.state_topic(sensor_key, user_id)
        payload: Dict[str, Any] = {
            "name": spec["label"],
            "unique_id": f"sensor.{object_id}",
            # HA 2026.x 使用 default_entity_id 指定实体 ID；object_id 兼容旧版本
            "default_entity_id": f"sensor.{object_id}",
            "object_id": object_id,
            "state_topic": state_topic,
            "value_template": "{{ value_json.state }}",
            "json_attributes_topic": state_topic,
            "icon": spec.get("icon", "mdi:flash"),
            "device": self._device_payload(user_id, user_name),
            "origin": {
                "name": "sgcc_electricity_new",
                "support_url": "https://github.com/gradual-wu/sgcc_electricity_new",
            },
        }
        if spec.get("unit"):
            payload["unit_of_measurement"] = spec["unit"]
        if spec.get("device_class"):
            payload["device_class"] = spec["device_class"]
        if spec.get("state_class"):
            payload["state_class"] = spec["state_class"]
            if spec["state_class"] == "total":
                # 让 HA 从状态 JSON 中读取周期起点，余额统计不会因重启而错乱
                payload["last_reset_value_template"] = "{{ value_json.last_reset }}"
        if spec.get("precision") is not None:
            payload["suggested_display_precision"] = spec["precision"]
        if self.availability_topic:
            payload["availability_topic"] = self.availability_topic
            payload["payload_available"] = "online"
            payload["payload_not_available"] = "offline"
        return payload

    # ------------------------------------------------------------------
    # publishing
    # ------------------------------------------------------------------
    def publish_sensor(
        self,
        sensor_key: str,
        user_id: str,
        state: Any,
        *,
        user_name: str = "",
        attributes: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if sensor_key not in SENSOR_SPECS:
            logging.warning("未知传感器类型 %s，跳过发布。", sensor_key)
            return False
        if state is None:
            return False

        discovery_topic = self.discovery_topic(sensor_key, user_id)
        # 配置本身是 retained 的，但每次运行都重发一次，保证 HA 侧元数据不过期。
        discovery_payload = self.build_discovery_payload(sensor_key, user_id, user_name)
        self.mqtt.publish(discovery_topic, discovery_payload, retain=True)
        self._published_discovery.add(discovery_topic)

        payload: Dict[str, Any] = {"state": state}
        if attributes:
            payload.update({k: v for k, v in attributes.items() if v is not None})
        return self.mqtt.publish(
            self.state_topic(sensor_key, user_id),
            payload,
            retain=True,
        )

    def remove_sensor(self, sensor_key: str, user_id: str) -> None:
        """发布空配置以删除 HA 中的 MQTT 实体。"""
        self.mqtt.publish(self.discovery_topic(sensor_key, user_id), "", retain=True)
        self.mqtt.publish(self.state_topic(sensor_key, user_id), "", retain=True)

    def remove_user_sensors(self, user_id: str) -> None:
        for sensor_key in SENSOR_SPECS:
            self.remove_sensor(sensor_key, user_id)
