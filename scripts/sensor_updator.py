import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from date_utils import (
    day_start_iso,
    month_start_iso,
    normalize_date,
    normalize_datetime,
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
        payment_record: dict = None,
        month_bill: dict = None,
        tier_state: dict = None,
        tier_official: dict = None,
        data_updated_at: str = None,
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
            payment_record,
            month_bill,
            tier_state,
            tier_official,
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
        bill_month = normalize_month((month_bill or {}).get("stat_month")) if month_bill else None
        if bill_month:
            bill_usage = to_float(month_bill.get("total_usage"))
            bill_charge = to_float(month_bill.get("total_charge"))
            if bill_usage is not None:
                month_usage = bill_usage
            if bill_charge is not None:
                month_charge = bill_charge
            display_month = bill_month

            monthly_tou, period, source = self._resolve_month_tou(
                tou_data, bill_tou_data, preferred_month=display_month
            )
            bill_tou = {
                key: to_float(month_bill.get(key))
                for key in TOU_FIELDS
                if to_float(month_bill.get(key)) is not None
            }
            if bill_tou:
                monthly_tou, period, source = bill_tou, bill_month, "official"
            else:
                # 官方账单没有分时电量时，不要把当月页面的分时数据挂到账单月份上
                monthly_tou, period, source = {}, bill_month, "official"
        else:
            display_month = normalize_month(month_period) or current_month
            monthly_tou, period, source = self._resolve_month_tou(
                tou_data, bill_tou_data, preferred_month=display_month
            )

        month_attributes = {
            "last_reset": month_start_iso(display_month),
            "period": display_month,
            "source": "official" if bill_month else (source or "page"),
        }
        if bill_month:
            month_attributes.update({
                "begin_date": month_bill.get("begin_date"),
                "end_date": month_bill.get("end_date"),
                "issue_date": month_bill.get("issue_date"),
                "year_usage": to_float(month_bill.get("year_usage")),
                "year_charge": to_float(month_bill.get("year_charge")),
                "level_flag": month_bill.get("level_flag"),
                "one_level_pq": to_float(month_bill.get("one_level_pq")),
                "two_level_pq": to_float(month_bill.get("two_level_pq")),
                "ladder_num": month_bill.get("ladder_num"),
            })

        if month_usage is not None:
            self._publish_sensor(
                user_id,
                "month_electricity_usage",
                month_usage,
                user_name,
                month_attributes,
            )
        if month_charge is not None:
            self._publish_sensor(
                user_id,
                "month_electricity_charge",
                month_charge,
                user_name,
                month_attributes,
            )

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

        self._publish_payment_sensors(user_id, payment_record, user_name)
        self._publish_tier_sensors(user_id, tier_state, tier_official, user_name)
        if data_updated_at:
            updated = normalize_datetime(data_updated_at)
            if updated:
                self._publish_sensor(
                    user_id,
                    "data_updated_at",
                    updated.replace(" ", "T") + "+08:00",
                    user_name,
                    {"updated_at": updated},
                )

        logging.info(f"[{user_id}] Home Assistant MQTT 传感器数据更新完成!")

    def _publish_payment_sensors(self, user_id: str, payment_record: Optional[dict], user_name: str):
        if not payment_record:
            return
        payment_date = normalize_datetime(payment_record.get("pay_date"))
        amount = to_float(payment_record.get("amount"))
        attributes = {
            "pay_date": payment_date,
            "pay_mode": payment_record.get("pay_mode"),
            "total_rep_amt": to_float(payment_record.get("total_rep_amt")),
        }
        if amount is not None:
            self._publish_sensor(
                user_id, "last_payment_amount", amount, user_name, attributes
            )
        if payment_date:
            timestamp = payment_date.replace(" ", "T") + "+08:00"
            self._publish_sensor(
                user_id, "last_payment_date", timestamp, user_name, attributes
            )

    def _publish_tier_sensors(
        self,
        user_id: str,
        tier_state: Optional[dict],
        tier_official: Optional[dict],
        user_name: str,
    ):
        if not tier_state:
            return
        official = tier_official or {}
        attributes = {
            "tier_name": tier_state.get("tier_name"),
            "range": tier_state.get("range_label"),
            "range_start": tier_state.get("range_start"),
            "range_end": tier_state.get("range_end"),
            "total_usage": tier_state.get("total_usage"),
            "previous_months_usage": tier_state.get("previous_months_usage"),
            "current_month_usage": tier_state.get("current_month_usage"),
            "remaining_usage": tier_state.get("remaining_usage"),
            "used_in_tier": tier_state.get("used_in_tier"),
            "progress_percent": tier_state.get("progress_percent"),
            "year": tier_state.get("year"),
            "month": tier_state.get("month"),
            "last_reset": f"{tier_state.get('year')}-01-01T00:00:00+08:00",
            "thresholds": tier_state.get("thresholds"),
            "current_month_source": tier_state.get("current_month_source"),
            "previous_month_sources": tier_state.get("previous_month_sources"),
            "official_tier": official.get("official_tier"),
            "official_level_flag": official.get("level_flag"),
            "official_one_level_pq": official.get("one_level_pq"),
            "official_two_level_pq": official.get("two_level_pq"),
            "official_total_year_pq": official.get("total_year_pq"),
            "official_total_month_pq": official.get("total_month_pq"),
            "official_ladder": official.get("ladder"),
            "official_ladder_num": official.get("ladder_num"),
            "official_snapshot_date": official.get("snapshot_date"),
        }
        self._publish_sensor(
            user_id, "current_electricity_tier", tier_state.get("tier"), user_name, attributes
        )
        self._publish_sensor(
            user_id, "current_tier_usage", tier_state.get("total_usage"), user_name, attributes
        )
        self._publish_sensor(
            user_id,
            "current_tier_remaining",
            tier_state.get("remaining_usage"),
            user_name,
            attributes,
        )

    # ------------------------------------------------------------------
    # publish from database
    # ------------------------------------------------------------------
    def publish_from_db(self, user_id: str, db, notify: bool = True) -> bool:
        """从数据库读取一个户号的全部数据并推送 HA，不访问国网页面。"""
        if db is None:
            return False
        if not db.connect_user_db(user_id):
            return False
        try:
            user = db.get_user(user_id) or {}
            user_name = user.get("user_name") or ""

            balance_row = db.get_latest_balance() or {}
            latest_daily = db.get_latest_daily() or {}
            today = datetime.now()
            current_month = today.strftime("%Y-%m")
            previous_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

            month_bill = db.get_monthly_bill(previous_month)
            month_row = None
            if month_bill:
                month_row = {
                    "month": month_bill.get("stat_month"),
                    "total_usage": to_float(month_bill.get("total_usage")),
                    "total_charge": to_float(month_bill.get("total_charge")),
                    "valley_usage": to_float(month_bill.get("valley_usage")),
                    "flat_usage": to_float(month_bill.get("flat_usage")),
                    "peak_usage": to_float(month_bill.get("peak_usage")),
                    "tip_usage": to_float(month_bill.get("tip_usage")),
                    "source": "official",
                }
            if month_row is None:
                month_row = (
                    db.get_monthly_usage_row(previous_month)
                    or db.get_monthly_usage_row(current_month)
                    or {}
                )

            year = str(today.year)
            year_row = db.get_yearly_row(year)
            if not year_row:
                monthly_rows = db.get_monthly_usage_rows(year)
                if monthly_rows:
                    year_row = {
                        "total_usage": sum(
                            to_float(row.get("total_usage"), default=0.0) or 0.0
                            for row in monthly_rows
                        ),
                        "total_charge": sum(
                            to_float(row.get("total_charge"), default=0.0) or 0.0
                            for row in monthly_rows
                        ),
                    }

            tou_data = {}
            if latest_daily:
                tou_data["daily"] = [latest_daily]
            if month_row:
                tou_data["months"] = [month_row]

            enhanced_balance = None
            if balance_row.get("amount_due") is not None or balance_row.get("as_of"):
                enhanced_balance = {
                    "amount_due": balance_row.get("amount_due"),
                    "as_of": balance_row.get("as_of"),
                }

            bill_tou_data = None
            if month_bill:
                bill_tou_data = {
                    "month": month_bill.get("stat_month"),
                    "usage": month_bill.get("total_usage"),
                    "charge": month_bill.get("total_charge"),
                    "valley_usage": month_bill.get("valley_usage"),
                    "flat_usage": month_bill.get("flat_usage"),
                    "peak_usage": month_bill.get("peak_usage"),
                    "tip_usage": month_bill.get("tip_usage"),
                    "source": "official",
                }
            elif month_row:
                bill_tou_data = {
                    "month": month_row.get("month"),
                    "usage": month_row.get("total_usage"),
                    "charge": month_row.get("total_charge"),
                    "valley_usage": month_row.get("valley_usage"),
                    "flat_usage": month_row.get("flat_usage"),
                    "peak_usage": month_row.get("peak_usage"),
                    "tip_usage": month_row.get("tip_usage"),
                    "source": month_row.get("source") or "calculated",
                }

            tier1 = float(os.getenv("TIER1_MAX", "2160"))
            tier2 = float(os.getenv("TIER2_MAX", "4200"))
            tier_state = db.get_tier_state(tier1, tier2)
            tier_official = db.get_latest_tier_snapshot()

            self.update_one_userid(
                user_id=user_id,
                balance=balance_row.get("balance"),
                last_daily_date=latest_daily.get("date"),
                last_daily_usage=latest_daily.get("total_usage"),
                yearly_charge=(year_row or {}).get("total_charge"),
                yearly_usage=(year_row or {}).get("total_usage"),
                month_charge=month_row.get("total_charge"),
                month_usage=month_row.get("total_usage"),
                tou_data=tou_data,
                enhanced_balance=enhanced_balance,
                bill_tou_data=bill_tou_data,
                user_name=user_name,
                month_period=month_row.get("month") or previous_month,
                payment_record=db.get_latest_payment(),
                month_bill=month_bill,
                tier_state=tier_state,
                tier_official=tier_official,
                data_updated_at=user.get("data_updated_at"),
                notify=notify,
            )
            return bool(
                balance_row or latest_daily or month_row or year_row or month_bill
            )
        finally:
            db.close_connect()

    def publish_all_from_db(self, db, notify: bool = True) -> bool:
        """从数据库读取所有户号并推送 HA。"""
        if db is None:
            return False
        if not db.connect_users_db():
            logging.warning("无法连接数据库，跳过 HA 发布。")
            return False
        try:
            users = db.get_users()
        finally:
            db.close_connect()
        if not users:
            logging.info("数据库中没有户号，跳过 HA 发布。")
            return False
        published = False
        for user in users:
            try:
                if self.publish_from_db(user["user_id"], db, notify=notify):
                    published = True
            except Exception as exc:
                logging.warning(f"[{user.get('user_id')}] 从数据库推送 HA 失败: {exc}")
        return published

    @staticmethod
    def _yearly_attributes() -> Dict[str, Any]:
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
        payment_record=None,
        month_bill=None,
        tier_state=None,
        tier_official=None,
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
        if payment_record:
            cache_entry["payment_record"] = payment_record
        if month_bill:
            cache_entry["month_bill"] = self._strip_raw(month_bill)
        if tier_state:
            cache_entry["tier_state"] = tier_state
        if tier_official:
            cache_entry["tier_official"] = self._strip_raw(tier_official)

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
        """缓存里不需要页面原始 Vue state / 原始 JSON，只保留结构化数据。"""
        if not isinstance(data, dict):
            return data
        return {key: value for key, value in data.items() if key not in ("raw", "raw_json")}

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
        preferred_month: Optional[str] = None,
    ):
        """返回 (月度分时数据, 所属月份, 数据来源)。"""
        current_month = datetime.now().strftime("%Y-%m")
        bill_month = normalize_month((bill_tou_data or {}).get("month"))
        bill_source = (bill_tou_data or {}).get("source") or "official"
        bill_values = {}
        if bill_tou_data:
            for key in TOU_FIELDS:
                value = to_float(bill_tou_data.get(key))
                if value is not None:
                    bill_values[key] = value

        preferred = normalize_month(preferred_month) if preferred_month else current_month

        # 优先使用与展示月份一致的账单/月度汇总分时数据
        if bill_month and bill_month == preferred and bill_values:
            return bill_values, bill_month, bill_source

        if bill_month == current_month and bill_values:
            return bill_values, current_month, bill_source

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

        if row_count and preferred == current_month:
            return month_values, current_month, "daily_sum"

        if bill_values and bill_month:
            return bill_values, bill_month, bill_source

        return {}, None, None
