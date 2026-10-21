import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from date_utils import (
    day_start_iso,
    month_start_iso,
    normalize_date,
    normalize_month,
    now_iso,
    to_float,
)
from ha_mqtt import HaMqttPublisher
from mqtt_publisher import MqttPublisher


TOU_FIELDS = ("valley_usage", "flat_usage", "peak_usage", "tip_usage")

# 与 ha-95598 的实体命名保持一致
DAILY_TOU_SENSOR_KEYS = {
    "valley_usage": "last_valley_electricity_usage",
    "flat_usage": "last_flat_electricity_usage",
    "peak_usage": "last_peak_electricity_usage",
    "tip_usage": "last_tip_electricity_usage",
}
MONTHLY_TOU_SENSOR_KEYS = {
    "valley_usage": "month_valley_usage",
    "flat_usage": "month_flat_usage",
    "peak_usage": "month_peak_usage",
    "tip_usage": "month_tip_usage",
}

# 旧版通过 REST API 直接创建的实体 ID（没有实体注册表记录）。
# MQTT Discovery 想复用这些 ID，必须先删除旧状态，否则 HA 会给新实体加 _2 后缀。
LEGACY_REST_SENSOR_KEYS = (
    "electricity_charge_balance",
    "last_electricity_usage",
    "yearly_electricity_usage",
    "yearly_electricity_charge",
    "month_electricity_usage",
    "month_electricity_charge",
    "month_valley_usage",
    "month_flat_usage",
    "month_peak_usage",
    "month_tip_usage",
    "prepay_balance",
)


class SensorUpdator:

    def __init__(self, mqtt_publisher: Optional[MqttPublisher] = None, cache_file: Optional[str] = None):
        self._cache_file = cache_file
        self.discovery_prefix = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant").strip("/") or "homeassistant"
        self.state_prefix = os.getenv("MQTT_STATE_PREFIX", "sgcc_electricity").strip("/") or "sgcc_electricity"
        self.availability_topic = os.getenv(
            "MQTT_AVAILABILITY_TOPIC", f"{self.state_prefix}/status"
        ).strip()
        self.mqtt = mqtt_publisher if mqtt_publisher is not None else self._build_mqtt_publisher()
        self.ha = HaMqttPublisher(
            self.mqtt,
            discovery_prefix=self.discovery_prefix,
            state_prefix=self.state_prefix,
            availability_topic=self.availability_topic,
        )
        self._cleaned_legacy_users = set()
        self._init_balance_notify()

    # ------------------------------------------------------------------
    # MQTT / notifications
    # ------------------------------------------------------------------
    def _build_mqtt_publisher(self) -> MqttPublisher:
        host = os.getenv("MQTT_HOST", "").strip()
        hass_url = os.getenv("HASS_URL", "").strip()
        if not host and hass_url:
            # 未显式配置 MQTT_HOST 时，默认使用 HA 所在主机和标准端口。
            host = urlparse(hass_url).hostname or ""
        publisher = MqttPublisher(
            host=host,
            port=int(os.getenv("MQTT_PORT", "1883")),
            username=os.getenv("MQTT_USERNAME", ""),
            password=os.getenv("MQTT_PASSWORD", ""),
            client_id=os.getenv("MQTT_CLIENT_ID", "sgcc_electricity_new"),
            qos=int(os.getenv("MQTT_QOS", "1")),
            retain=os.getenv("MQTT_RETAIN", "true").lower() == "true",
            publish_timeout_seconds=float(os.getenv("MQTT_PUBLISH_TIMEOUT_SECONDS", "10")),
            availability_topic=self.availability_topic,
            tls=os.getenv("MQTT_TLS", "false").lower() == "true",
        )
        if publisher.enabled():
            logging.info(
                "MQTT 配置: %s:%s，discovery=%s，state=%s，认证=%s",
                host,
                os.getenv("MQTT_PORT", "1883"),
                self.discovery_prefix,
                self.state_prefix,
                "是" if os.getenv("MQTT_USERNAME") else "否",
            )
        else:
            logging.warning(
                "未配置 MQTT_HOST，且无法从 HASS_URL 推断出主机，HA 传感器将不会更新。"
            )
        return publisher

    def _init_balance_notify(self):
        push_type = os.getenv("PUSH_TYPE", "None").lower()
        if push_type == "pushplus":
            from notify import PushplusNotify
            self.balance_notify = PushplusNotify()
        elif push_type == "urlpush":
            from notify import UrlPushNotify
            self.balance_notify = UrlPushNotify()
        else:
            self.balance_notify = None

    def close(self, publish_offline: bool = True):
        self.mqtt.close(publish_offline=publish_offline)

    # ------------------------------------------------------------------
    # public entry
    # ------------------------------------------------------------------
    def update_one_userid(
        self,
        user_id: str,
        balance: float,
        last_daily_date: str,
        last_daily_usage: float,
        yearly_charge: float,
        yearly_usage: float,
        month_charge: float,
        month_usage: float,
        tou_data: dict = None,
        enhanced_balance: dict = None,
        bill_tou_data: dict = None,
        user_name: str = "",
        month_period: str = "",
        notify: bool = True,
    ):
        logging.info(f"[{user_id}] 开始更新 Home Assistant MQTT 传感器数据...")
        # 国网页面 DOM 取到的数字是字符串，统一转成 float，避免 MQTT 实体状态是字符串
        balance = to_float(balance)
        last_daily_usage = to_float(last_daily_usage)
        yearly_charge = to_float(yearly_charge)
        yearly_usage = to_float(yearly_usage)
        month_charge = to_float(month_charge)
        month_usage = to_float(month_usage)
        amount_due = to_float((enhanced_balance or {}).get("amount_due"))

        self._save_to_cache(
            user_id,
            balance,
            last_daily_date,
            last_daily_usage,
            yearly_charge,
            yearly_usage,
            month_charge,
            month_usage,
            tou_data,
            enhanced_balance,
            bill_tou_data,
            user_name,
            month_period,
        )

        if balance is not None:
            if notify and self.balance_notify is not None:
                self.balance_notify(user_id, balance)
            attributes = {"last_reset": now_iso()}
            if amount_due is not None:
                attributes["amount_due"] = amount_due
            self._publish_sensor(
                user_id,
                "electricity_charge_balance",
                balance,
                user_name,
                attributes,
            )

        if last_daily_usage is not None:
            daily_attributes = {
                "last_reset": day_start_iso(last_daily_date),
                "last_daily_date": last_daily_date,
            }
            self._publish_sensor(
                user_id,
                "last_electricity_usage",
                last_daily_usage,
                user_name,
                daily_attributes,
            )
            daily_tou = self._extract_daily_tou(tou_data, last_daily_date)
            for key, value in daily_tou.items():
                self._publish_sensor(
                    user_id,
                    DAILY_TOU_SENSOR_KEYS[key],
                    value,
                    user_name,
                    daily_attributes,
                )

        if yearly_usage is not None:
            self._publish_sensor(
                user_id,
                "yearly_electricity_usage",
                yearly_usage,
                user_name,
                self._yearly_attributes(),
            )
        if yearly_charge is not None:
            self._publish_sensor(
                user_id,
                "yearly_electricity_charge",
                yearly_charge,
                user_name,
                self._yearly_attributes(),
            )

        current_month = datetime.now().strftime("%Y-%m")
        display_month = normalize_month(month_period) or current_month
        if month_usage is not None:
            self._publish_sensor(
                user_id,
                "month_electricity_usage",
                month_usage,
                user_name,
                {"last_reset": month_start_iso(display_month), "period": display_month},
            )
        if month_charge is not None:
            self._publish_sensor(
                user_id,
                "month_electricity_charge",
                month_charge,
                user_name,
                {"last_reset": month_start_iso(display_month), "period": display_month},
            )

        monthly_tou, period, source = self._resolve_month_tou(tou_data, bill_tou_data)
        for key, value in monthly_tou.items():
            self._publish_sensor(
                user_id,
                MONTHLY_TOU_SENSOR_KEYS[key],
                value,
                user_name,
                {
                    "last_reset": month_start_iso(period),
                    "period": period,
                    "source": source,
                },
            )

        if enhanced_balance and amount_due is not None:
            self._publish_sensor(
                user_id,
                "prepay_balance",
                amount_due,
                user_name,
                {
                    "last_reset": now_iso(),
                    "as_of": enhanced_balance.get("as_of"),
                },
            )

        logging.info(f"[{user_id}] Home Assistant MQTT 传感器数据更新完成!")

    @staticmethod
    def _yearly_attributes() -> Dict[str, Any]:
        # 1 月页面默认展示上一年数据，国网页面逻辑与原版保持一致
        report_year = datetime.now().year - 1 if datetime.now().month == 1 else datetime.now().year
        return {
            "last_reset": f"{report_year}-01-01T00:00:00+08:00",
            "period": str(report_year),
        }

    # ------------------------------------------------------------------
    # cache
    # ------------------------------------------------------------------
    def _get_cache_file(self):
        if self._cache_file:
            return self._cache_file
        from const import get_data_dir
        return os.path.join(get_data_dir(), 'sgcc_cache.json')

    def _save_to_cache(
        self,
        user_id,
        balance,
        last_daily_date,
        last_daily_usage,
        yearly_charge,
        yearly_usage,
        month_charge,
        month_usage,
        tou_data=None,
        enhanced_balance=None,
        bill_tou_data=None,
        user_name="",
        month_period="",
    ):
        cache_file = self._get_cache_file()
        abs_cache_file = os.path.abspath(cache_file)
        data = {}
        try:
            if os.path.exists(cache_file):
                with open(cache_file, 'r') as f:
                    data = json.load(f)
        except Exception as e:
            logging.warning(f"加载缓存文件失败: {e}")

        cache_entry = {
            "balance": balance,
            "last_daily_date": last_daily_date,
            "last_daily_usage": last_daily_usage,
            "yearly_charge": yearly_charge,
            "yearly_usage": yearly_usage,
            "month_charge": month_charge,
            "month_usage": month_usage,
            "timestamp": datetime.now().isoformat()
        }

        if tou_data:
            cache_entry["tou_data"] = self._strip_raw(tou_data)
        if bill_tou_data:
            cache_entry["bill_tou_data"] = self._strip_raw(bill_tou_data)
        if enhanced_balance:
            cache_entry["enhanced_balance"] = enhanced_balance
        if user_name:
            cache_entry["user_name"] = user_name
        if month_period:
            cache_entry["month_period"] = month_period

        data[user_id] = cache_entry

        try:
            os.makedirs(os.path.dirname(abs_cache_file), exist_ok=True)
            with open(cache_file, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logging.debug(f"已保存数据到缓存文件: {abs_cache_file}")
        except Exception as e:
            logging.error(f"保存缓存文件失败 {abs_cache_file}: {e}")

    @staticmethod
    def _strip_raw(data: dict) -> dict:
        """缓存里不需要页面原始 Vue state，只保留结构化的日/月数据。"""
        if not isinstance(data, dict):
            return data
        return {key: value for key, value in data.items() if key != "raw"}

    def republish(self):
        cache_file = self._get_cache_file()
        abs_cache_file = os.path.abspath(cache_file)
        if not os.path.exists(cache_file):
            logging.info(f"未找到缓存文件 {abs_cache_file}，跳过重新推送。")
            return False

        data = {}
        try:
            with open(cache_file, 'r') as f:
                data = json.load(f)
        except Exception as e:
            logging.error(f"加载缓存文件失败 {abs_cache_file}: {e}")
            return False

        # 检查缓存数据的日期是否与当前日期一致
        today_str = datetime.now().strftime("%Y-%m-%d")
        for user_id, values in data.items():
            cache_timestamp = values.get("timestamp", "")
            cache_date = cache_timestamp[:10] if cache_timestamp else ""
            if cache_date != today_str:
                logging.info(f"缓存数据日期({cache_date})与当前日期({today_str})不一致，需要从国家电网重新获取数据。")
                return False

        try:
            for user_id, values in data.items():
                logging.info(f"正在从缓存重新推送用户 {user_id} 的数据。")
                clean_values = {k: v for k, v in values.items() if k != 'timestamp'}
                self.update_one_userid(user_id, **clean_values, notify=False)
            return True
        except Exception as e:
            logging.error(f"重新推送数据失败: {e}")
            return False

    # ------------------------------------------------------------------
    # legacy REST cleanup / publishing helpers
    # ------------------------------------------------------------------
    def _cleanup_legacy_rest_states(self, user_id: str):
        """删除旧版 REST API 直接创建的同名实体状态。

        旧版通过 POST /api/states/<entity_id> 创建状态，没有实体注册表记录。
        如果不清除，MQTT Discovery 创建同名实体时会被 HA 改名为 *_2。
        """
        if user_id in self._cleaned_legacy_users:
            return
        self._cleaned_legacy_users.add(user_id)

        if os.getenv("CLEANUP_LEGACY_REST_STATES", "true").lower() != "true":
            return

        hass_url = os.getenv("HASS_URL", "").rstrip("/")
        hass_token = os.getenv("HASS_TOKEN", "")
        if not hass_url or not hass_token:
            return

        marker = os.path.join(
            os.path.dirname(os.path.abspath(self._get_cache_file())),
            f".mqtt_legacy_cleaned_{user_id}",
        )
        if os.path.exists(marker):
            return

        headers = {"Authorization": f"Bearer {hass_token}"}
        postfix = f"_{user_id[-4:]}"
        had_error = False
        removed = 0
        for sensor_key in LEGACY_REST_SENSOR_KEYS:
            entity_id = f"sensor.{sensor_key}{postfix}"
            try:
                response = requests.delete(
                    f"{hass_url}/api/states/{entity_id}",
                    headers=headers,
                    timeout=5,
                )
                if response.status_code == 200:
                    removed += 1
                    logging.info(f"已删除旧版 REST 实体状态 {entity_id}")
                elif response.status_code != 404:
                    had_error = True
                    logging.warning(
                        "删除旧实体 %s 返回状态码 %s，下次启动会重试。",
                        entity_id,
                        response.status_code,
                    )
            except Exception as e:
                had_error = True
                logging.warning(f"删除旧实体 {entity_id} 失败，下次启动会重试: {e}")

        if removed:
            logging.info(f"旧版 REST 实体清理完成，共删除 {removed} 个状态。")
        if had_error:
            return

        try:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            with open(marker, "w") as f:
                f.write(datetime.now().isoformat())
        except Exception as e:
            logging.debug(f"写入旧实体清理标记失败: {e}")

    def _publish_sensor(
        self,
        user_id: str,
        sensor_key: str,
        value,
        user_name: str = "",
        attributes: Optional[Dict[str, Any]] = None,
    ):
        if value is None:
            return
        self._cleanup_legacy_rest_states(user_id)
        if self.ha.publish_sensor(
            sensor_key,
            user_id,
            value,
            user_name=user_name,
            attributes=attributes,
        ):
            logging.info(f"[{user_id}] MQTT 传感器 {sensor_key} 已更新: {value}")

    # ------------------------------------------------------------------
    # data extraction
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_daily_tou(tou_data: Optional[dict], last_daily_date: str) -> Dict[str, float]:
        if not tou_data:
            return {}
        rows = [row for row in (tou_data.get("daily") or []) if isinstance(row, dict)]
        if not rows:
            return {}

        target_date = normalize_date(last_daily_date)
        matched = None
        if target_date:
            for row in rows:
                if normalize_date(row.get("date")) == target_date:
                    matched = row
                    break
            if matched is None:
                # 页面日期格式偶尔不同，退回到不晚于目标日期的最近一条
                candidates = [
                    row for row in rows
                    if normalize_date(row.get("date")) and normalize_date(row.get("date")) <= target_date
                ]
                if candidates:
                    matched = max(candidates, key=lambda row: normalize_date(row.get("date")))
        if matched is None:
            dated_rows = [row for row in rows if normalize_date(row.get("date"))]
            matched = max(dated_rows, key=lambda row: normalize_date(row.get("date"))) if dated_rows else rows[0]

        result = {}
        for key in TOU_FIELDS:
            value = to_float(matched.get(key))
            if value is not None:
                result[key] = value
        return result

    @staticmethod
    def _resolve_month_tou(
        tou_data: Optional[dict],
        bill_tou_data: Optional[dict],
    ):
        """返回 (月度分时数据, 所属月份, 数据来源)。"""
        current_month = datetime.now().strftime("%Y-%m")
        bill_month = normalize_month((bill_tou_data or {}).get("month"))
        bill_values = {}
        if bill_tou_data:
            for key in TOU_FIELDS:
                value = to_float(bill_tou_data.get(key))
                if value is not None:
                    bill_values[key] = value

        if bill_month == current_month and bill_values:
            return bill_values, current_month, "official"

        # 当月账单通常还没出，用当月已有的日数据累加
        month_values = {key: 0.0 for key in TOU_FIELDS}
        row_count = 0
        if tou_data:
            for row in (tou_data.get("daily") or []):
                if not isinstance(row, dict):
                    continue
                if normalize_month(row.get("date")) != current_month:
                    continue
                row_count += 1
                for key in TOU_FIELDS:
                    month_values[key] += to_float(row.get(key), default=0.0) or 0.0

        if row_count:
            return month_values, current_month, "daily_sum"

        if bill_values and bill_month:
            return bill_values, bill_month, "official"

        return {}, None, None
