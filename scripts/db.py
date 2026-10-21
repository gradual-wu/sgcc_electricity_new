"""国网电费数据持久化层。

数据模型按“业务对象”拆分，而不是每个户号建一套表：

- sg_users          户号基本信息
- sg_daily_usage    日用电量（含谷/平/峰/尖分时）
- sg_monthly_usage  月度用电量/电费（区分官方账单与页面计算值）
- sg_yearly_usage   年度用电量/电费
- sg_balance_log    余额变更历史

MySQL 与 SQLite 共用同一套表结构和写入语义。默认 DATA_RETENTION_DAYS=0，
表示永久保留；只有显式设置大于 0 时才清理日数据和余额历史。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from date_utils import (
    normalize_date,
    normalize_datetime,
    normalize_month,
    normalize_year,
    to_float,
)


class DB:
    USERS_TABLE = "sg_users"
    DAILY_TABLE = "sg_daily_usage"
    MONTHLY_TABLE = "sg_monthly_usage"
    YEARLY_TABLE = "sg_yearly_usage"
    BALANCE_TABLE = "sg_balance_log"
    PAYMENT_TABLE = "sg_payments"
    BILL_TABLE = "sg_monthly_bill"
    TIER_TABLE = "sg_tier_snapshot"

    MONTHLY_SOURCE_OFFICIAL = "official"
    MONTHLY_SOURCE_CALCULATED = "calculated"
    MONTHLY_SOURCE_LEGACY = "legacy"

    def __init__(self) -> None:
        self.connect = None
        self.user_id: Optional[str] = None
        self._retention_logged = False

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_user_id(user_id: Any) -> str:
        value = str(user_id or "").strip()
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", value):
            raise ValueError(f"非法户号: {user_id!r}")
        return value

    @staticmethod
    def _normalize_source(value: Any, default: str = "calculated") -> str:
        source = str(value or default).strip().lower()
        if source not in ("official", "calculated", "daily_sum", "legacy"):
            return default
        if source == "daily_sum":
            return "calculated"
        return source

    @staticmethod
    def _normalize_text(value: Any, max_length: int = 128) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text in ("None", "null"):
            return None
        return text[:max_length]

    @staticmethod
    def _retention_days() -> int:
        try:
            return max(int(os.getenv("DATA_RETENTION_DAYS", "0") or 0), 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _daily_values(cls, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        stat_date = normalize_date(data.get("date") or data.get("stat_date"))
        if not stat_date:
            return None
        return {
            "stat_date": stat_date,
            "total_usage": to_float(data.get("total_usage", data.get("usage"))),
            "total_charge": to_float(data.get("total_charge")),
            "valley_usage": to_float(data.get("valley_usage")),
            "flat_usage": to_float(data.get("flat_usage")),
            "peak_usage": to_float(data.get("peak_usage")),
            "tip_usage": to_float(data.get("tip_usage")),
            "source": cls._normalize_source(data.get("source")),
        }

    @classmethod
    def _monthly_values(cls, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        stat_month = normalize_month(data.get("month") or data.get("stat_month"))
        if not stat_month:
            return None
        return {
            "stat_month": stat_month,
            "total_usage": to_float(data.get("total_usage", data.get("usage"))),
            "total_charge": to_float(data.get("total_charge", data.get("charge"))),
            "valley_usage": to_float(data.get("valley_usage")),
            "flat_usage": to_float(data.get("flat_usage")),
            "peak_usage": to_float(data.get("peak_usage")),
            "tip_usage": to_float(data.get("tip_usage")),
            "source": cls._normalize_source(data.get("source")),
        }

    @classmethod
    def _yearly_values(cls, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        stat_year = normalize_year(data.get("year") or data.get("stat_year"))
        if not stat_year:
            return None
        return {
            "stat_year": stat_year,
            "total_usage": to_float(data.get("total_usage", data.get("usage"))),
            "total_charge": to_float(data.get("total_charge", data.get("charge"))),
            "valley_usage": to_float(data.get("valley_usage")),
            "flat_usage": to_float(data.get("flat_usage")),
            "peak_usage": to_float(data.get("peak_usage")),
            "tip_usage": to_float(data.get("tip_usage")),
            "source": cls._normalize_source(data.get("source")),
        }

    @staticmethod
    def _balance_values(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        balance = to_float(data.get("balance"))
        if balance is None:
            return None
        return {
            "balance": balance,
            "amount_due": to_float(data.get("amount_due")),
            "as_of": normalize_datetime(data.get("as_of")),
        }

    @classmethod
    def _payment_values(cls, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raw_date = data.get("pay_date") or data.get("payDate") or data.get("date")
        pay_date = normalize_datetime(raw_date)
        if pay_date is None:
            date_only = normalize_date(raw_date)
            pay_date = f"{date_only} 00:00:00" if date_only else None
        amount = to_float(data.get("amount", data.get("rcvAmt")))
        if not pay_date or amount is None:
            return None
        raw_json = data.get("raw_json")
        if raw_json is None and data.get("raw") is not None:
            try:
                import json
                raw_json = json.dumps(data.get("raw"), ensure_ascii=False)
            except Exception:
                raw_json = None
        return {
            "pay_date": pay_date,
            "amount": amount,
            "pay_mode": cls._normalize_text(data.get("pay_mode") or data.get("payModeName"), 64) or "",
            "total_rep_amt": to_float(data.get("total_rep_amt", data.get("totalRepAmt"))),
            "cons_no": cls._normalize_text(data.get("cons_no") or data.get("consNo"), 64),
            "raw_json": raw_json,
        }

    @classmethod
    def _bill_values(cls, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        stat_month = normalize_month(data.get("month") or data.get("stat_month"))
        if not stat_month:
            return None
        raw_json = data.get("raw_json")
        if raw_json is None and data.get("raw") is not None:
            try:
                import json
                raw_json = json.dumps(data.get("raw"), ensure_ascii=False)
            except Exception:
                raw_json = None
        return {
            "stat_month": stat_month,
            "total_usage": to_float(data.get("total_usage", data.get("usage"))),
            "total_charge": to_float(data.get("total_charge", data.get("charge"))),
            "valley_usage": to_float(data.get("valley_usage")),
            "flat_usage": to_float(data.get("flat_usage")),
            "peak_usage": to_float(data.get("peak_usage")),
            "tip_usage": to_float(data.get("tip_usage")),
            "year_usage": to_float(data.get("year_usage")),
            "year_charge": to_float(data.get("year_charge")),
            "one_level_pq": to_float(data.get("one_level_pq")),
            "two_level_pq": to_float(data.get("two_level_pq")),
            "total_year_pq": to_float(data.get("total_year_pq")),
            "total_month_pq": to_float(data.get("total_month_pq")),
            "year_used_pq": to_float(data.get("year_used_pq")),
            "level_flag": int(to_float(data.get("level_flag"), default=0) or 0),
            "ladder": cls._normalize_text(data.get("ladder"), 8),
            "ladder_num": cls._normalize_text(data.get("ladder_num"), 8),
            "bill_settle_type": cls._normalize_text(data.get("bill_settle_type"), 8),
            "begin_date": normalize_date(data.get("begin_date")),
            "end_date": normalize_date(data.get("end_date")),
            "issue_date": normalize_date(data.get("issue_date")),
            "source": cls._normalize_source(data.get("source"), "official"),
            "raw_json": raw_json,
        }

    @classmethod
    def _tier_values(cls, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        snapshot_date = normalize_date(data.get("date") or data.get("snapshot_date"))
        if not snapshot_date:
            return None
        raw_json = data.get("raw_json")
        if raw_json is None and data.get("raw") is not None:
            try:
                import json
                raw_json = json.dumps(data.get("raw"), ensure_ascii=False)
            except Exception:
                raw_json = None
        return {
            "snapshot_date": snapshot_date,
            "level_flag": int(to_float(data.get("level_flag"), default=0) or 0),
            "one_level_pq": to_float(data.get("one_level_pq")),
            "two_level_pq": to_float(data.get("two_level_pq")),
            "total_year_pq": to_float(data.get("total_year_pq")),
            "total_month_pq": to_float(data.get("total_month_pq")),
            "year_used_pq": to_float(data.get("year_used_pq")),
            "ladder": cls._normalize_text(data.get("ladder"), 8),
            "ladder_num": cls._normalize_text(data.get("ladder_num"), 8),
            "official_tier": int(to_float(data.get("official_tier"), default=0) or 0),
            "source": cls._normalize_text(data.get("source"), 32) or "official",
            "raw_json": raw_json,
        }

    # ------------------------------------------------------------------
    # interface
    # ------------------------------------------------------------------
    def connect_user_db(self, user_id: Any) -> bool:
        raise NotImplementedError

    def connect_users_db(self) -> bool:
        """只建立连接并确保表结构存在，不绑定/创建具体户号。"""
        raise NotImplementedError

    def upsert_user(self, user_id: Any, account: str = "", user_name: str = "") -> bool:
        raise NotImplementedError

    def touch_user_data(self, user_id: Any) -> bool:
        raise NotImplementedError

    def insert_daily_data(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def insert_monthly_data(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def insert_yearly_data(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def insert_balance_log(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def insert_payment(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def get_latest_payment(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def insert_monthly_bill(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def get_monthly_bill(self, month: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_official_bill_months(self, year: str) -> set[str]:
        raise NotImplementedError

    def insert_tier_snapshot(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def get_latest_tier_snapshot(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_monthly_usage_rows(self, year: str) -> list[Dict[str, Any]]:
        raise NotImplementedError

    def get_monthly_usage_row(self, month: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_yearly_row(self, year: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_latest_balance(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_latest_daily(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_user(self, user_id: Any) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_users(self) -> list[Dict[str, Any]]:
        raise NotImplementedError

    def get_daily_summary(self, start_date: str, end_date: str) -> Dict[str, Any]:
        raise NotImplementedError

    def get_daily_dates(self, start_date: str, end_date: str) -> set[str]:
        raise NotImplementedError

    def cleanup_old_data(self) -> None:
        raise NotImplementedError

    def close_connect(self) -> None:
        raise NotImplementedError

    def ping(self) -> None:
        raise NotImplementedError

    def ensure_connected(self, user_id: Any) -> bool:
        """检查连接是否可用；MySQL 长时间空闲后可能被服务端断开，必要时重连。"""
        try:
            if self.connect is not None:
                self.ping()
                return True
        except Exception:
            pass
        return self.connect_user_db(user_id)

    # ------------------------------------------------------------------
    # derived helpers (shared by MySQL and SQLite)
    # ------------------------------------------------------------------
    def sync_calculated_month_from_daily(self, month: str) -> bool:
        """用日数据汇总一个月的电量/电费；官方账单已存在时不覆盖。

        如果该月已有页面月度值（calculated/legacy），保留页面电量和电费，
        只用日数据补充分时字段。否则用日数据创建一条 calculated 月度行。
        """
        month_key = normalize_month(month)
        if not month_key:
            return False
        if self.get_monthly_bill(month_key) is not None:
            return True
        year, mon = month_key.split("-")
        start = f"{month_key}-01"
        if int(mon) == 12:
            end = f"{year}-12-31"
        else:
            end = f"{year}-{int(mon) + 1:02d}-01"
            end = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
        summary = self.get_daily_summary(start, end)
        if not summary or not summary.get("count"):
            return False

        existing = self.get_monthly_usage_row(month_key)
        payload = {
            "month": month_key,
            "valley_usage": summary.get("valley_usage"),
            "flat_usage": summary.get("flat_usage"),
            "peak_usage": summary.get("peak_usage"),
            "tip_usage": summary.get("tip_usage"),
            "source": self.MONTHLY_SOURCE_CALCULATED,
        }
        if existing and existing.get("total_usage") is not None:
            # 页面月度电量/电费更接近官方口径，保留；只补分时。
            payload["total_usage"] = None
            payload["total_charge"] = None
        else:
            payload["total_usage"] = summary.get("total_usage")
            payload["total_charge"] = summary.get("total_charge")
        return self.insert_monthly_data(payload)

    def refresh_yearly_from_monthly(self, year: str) -> bool:
        year_key = normalize_year(year)
        if not year_key:
            return False
        rows = self.get_monthly_usage_rows(year_key)
        if not rows:
            return False
        totals = {
            "total_usage": 0.0,
            "total_charge": 0.0,
            "valley_usage": 0.0,
            "flat_usage": 0.0,
            "peak_usage": 0.0,
            "tip_usage": 0.0,
        }
        for row in rows:
            for key in totals:
                value = to_float(row.get(key))
                if value is not None:
                    totals[key] += value
        source = (
            self.MONTHLY_SOURCE_OFFICIAL
            if any(row.get("source") == self.MONTHLY_SOURCE_OFFICIAL for row in rows)
            else self.MONTHLY_SOURCE_CALCULATED
        )
        return self.insert_yearly_data(
            {
                "year": year_key,
                **totals,
                "source": source,
                # 年度表是月度表的派生值，允许覆盖旧的官方/计算值
                "force": True,
            }
        )

    def has_official_bill(self, month: str) -> bool:
        month_key = normalize_month(month)
        if not month_key:
            return False
        if self.get_monthly_bill(month_key) is not None:
            return True
        for row in self.get_monthly_usage_rows(month_key[:4]):
            if (
                row.get("month") == month_key
                and row.get("source") == self.MONTHLY_SOURCE_OFFICIAL
            ):
                return True
        return False

    def get_tier_state(
        self,
        tier1_max: float = 2160.0,
        tier2_max: float = 4200.0,
        today: Optional[date] = None,
    ) -> Dict[str, Any]:
        """按“历史月份账单 + 当月实时日数据”计算年度累计阶梯。

        当年账单当月不会出，因此当月使用日用电量实时累加；历史月份优先使用
        月度表（官方账单会覆盖计算值），缺失时回退到日数据汇总。
        """
        today = today or date.today()
        year = today.year
        current_month = today.month
        year_key = f"{year:04d}"
        current_month_key = f"{year_key}-{current_month:02d}"

        monthly_rows = {
            str(row.get("month")): row
            for row in self.get_monthly_usage_rows(year_key)
            if row.get("month")
        }

        previous_months_usage = 0.0
        previous_month_sources: Dict[str, str] = {}
        for month_no in range(1, current_month):
            month_key = f"{year_key}-{month_no:02d}"
            row = monthly_rows.get(month_key)
            usage = to_float((row or {}).get("total_usage"))
            if usage is not None:
                previous_months_usage += usage
                previous_month_sources[month_key] = str(
                    (row or {}).get("source") or self.MONTHLY_SOURCE_CALCULATED
                )
                continue
            month_start = f"{month_key}-01"
            if month_no == 12:
                month_end = f"{year_key}-12-31"
            else:
                month_end = (date(year, month_no + 1, 1) - timedelta(days=1)).isoformat()
            summary = self.get_daily_summary(month_start, month_end)
            previous_months_usage += to_float(summary.get("total_usage"), default=0.0) or 0.0
            previous_month_sources[month_key] = "daily_sum"

        current_month_usage = 0.0
        current_source = "daily_sum"
        current_row = monthly_rows.get(current_month_key)
        # 当月账单通常还没出，实时阶梯始终以当月日数据为准；只有当日数据
        # 完全缺失时才回退到月度表里已有的当月汇总，避免把当月账单重复累加。
        summary = self.get_daily_summary(f"{current_month_key}-01", today.isoformat())
        if summary.get("count"):
            current_month_usage = to_float(summary.get("total_usage"), default=0.0) or 0.0
            current_source = "daily_sum"
        elif current_row:
            current_month_usage = to_float(current_row.get("total_usage"), default=0.0) or 0.0
            current_source = str(current_row.get("source") or "calculated")

        total_usage = round(previous_months_usage + current_month_usage, 3)
        tier1_max = float(tier1_max)
        tier2_max = float(max(tier2_max, tier1_max))

        if total_usage <= tier1_max:
            tier = 1
            tier_name = "第一阶梯"
            range_start = 0.0
            range_end = tier1_max
            range_label = f"0-{int(tier1_max)}"
            remaining = max(tier1_max - total_usage, 0.0)
            tier_width = tier1_max
        elif total_usage <= tier2_max:
            tier = 2
            tier_name = "第二阶梯"
            range_start = tier1_max
            range_end = tier2_max
            range_label = f"{int(tier1_max) + 1}-{int(tier2_max)}"
            remaining = max(tier2_max - total_usage, 0.0)
            tier_width = tier2_max - tier1_max
        else:
            tier = 3
            tier_name = "第三阶梯"
            range_start = tier2_max
            range_end = None
            range_label = f"{int(tier2_max) + 1}以上"
            remaining = 0.0
            tier_width = None

        used_in_tier = max(total_usage - range_start, 0.0)
        if tier == 3:
            progress = 100.0
        else:
            progress = round(min(used_in_tier / tier_width * 100.0, 100.0), 2) if tier_width else 0.0

        return {
            "year": year_key,
            "month": current_month_key,
            "previous_months_usage": round(previous_months_usage, 3),
            "current_month_usage": round(current_month_usage, 3),
            "total_usage": total_usage,
            "tier": tier,
            "tier_name": tier_name,
            "range_start": range_start,
            "range_end": range_end,
            "range_label": range_label,
            "remaining_usage": round(remaining, 3),
            "used_in_tier": round(used_in_tier, 3),
            "progress_percent": progress,
            "thresholds": {"tier1_max": tier1_max, "tier2_max": tier2_max},
            "current_month_source": current_source,
            "previous_month_sources": previous_month_sources,
        }


class MysqlDB(DB):
    def connect_user_db(self, user_id: Any) -> bool:
        self.close_connect()
        try:
            import mysql.connector

            self.user_id = self._normalize_user_id(user_id)
            self.connect = mysql.connector.connect(
                host=os.getenv("MYSQL_HOST"),
                user=os.getenv("MYSQL_USER"),
                password=os.getenv("MYSQL_PASSWORD"),
                database=os.getenv("MYSQL_DATABASE"),
                port=int(os.getenv("MYSQL_PORT", 3306)),
                charset="utf8mb4",
                use_unicode=True,
                connection_timeout=10,
            )
            if not self.connect.is_connected():
                logging.error("连接 MySQL 数据库失败。")
                self.connect = None
                return False
            self._create_schema()
            self._migrate_legacy_if_needed()
            logging.info("已连接 MySQL 数据库，当前户号 %s。", self.user_id)
            return True
        except Exception as exc:
            logging.error("连接 MySQL 数据库失败: %s", exc)
            self.connect = None
            return False

    def connect_users_db(self) -> bool:
        self.close_connect()
        try:
            import mysql.connector

            self.connect = mysql.connector.connect(
                host=os.getenv("MYSQL_HOST"),
                user=os.getenv("MYSQL_USER"),
                password=os.getenv("MYSQL_PASSWORD"),
                database=os.getenv("MYSQL_DATABASE"),
                port=int(os.getenv("MYSQL_PORT", 3306)),
                charset="utf8mb4",
                use_unicode=True,
                connection_timeout=10,
            )
            if not self.connect.is_connected():
                logging.error("连接 MySQL 数据库失败。")
                self.connect = None
                return False
            self._create_schema()
            return True
        except Exception as exc:
            logging.error("连接 MySQL 数据库失败: %s", exc)
            self.connect = None
            return False

    def _cursor(self):
        if self.connect is None:
            raise RuntimeError("数据库连接未建立")
        return self.connect.cursor()

    def _create_schema(self) -> None:
        statements = (
            f"""
            CREATE TABLE IF NOT EXISTS `{self.USERS_TABLE}` (
                `user_id` VARCHAR(64) NOT NULL,
                `account` VARCHAR(64) DEFAULT NULL,
                `user_name` VARCHAR(128) DEFAULT NULL,
                `first_seen_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `last_seen_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `data_updated_at` DATETIME DEFAULT NULL,
                PRIMARY KEY (`user_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self.DAILY_TABLE}` (
                `user_id` VARCHAR(64) NOT NULL,
                `stat_date` DATE NOT NULL,
                `total_usage` DECIMAL(12,3) DEFAULT NULL,
                `total_charge` DECIMAL(12,2) DEFAULT NULL,
                `valley_usage` DECIMAL(12,3) DEFAULT NULL,
                `flat_usage` DECIMAL(12,3) DEFAULT NULL,
                `peak_usage` DECIMAL(12,3) DEFAULT NULL,
                `tip_usage` DECIMAL(12,3) DEFAULT NULL,
                `source` VARCHAR(16) NOT NULL DEFAULT 'calculated',
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`user_id`, `stat_date`),
                KEY `idx_sg_daily_date` (`stat_date`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self.MONTHLY_TABLE}` (
                `user_id` VARCHAR(64) NOT NULL,
                `stat_month` CHAR(7) NOT NULL,
                `total_usage` DECIMAL(12,3) DEFAULT NULL,
                `total_charge` DECIMAL(12,2) DEFAULT NULL,
                `valley_usage` DECIMAL(12,3) DEFAULT NULL,
                `flat_usage` DECIMAL(12,3) DEFAULT NULL,
                `peak_usage` DECIMAL(12,3) DEFAULT NULL,
                `tip_usage` DECIMAL(12,3) DEFAULT NULL,
                `source` VARCHAR(16) NOT NULL DEFAULT 'calculated',
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`user_id`, `stat_month`),
                KEY `idx_sg_monthly_month` (`stat_month`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self.YEARLY_TABLE}` (
                `user_id` VARCHAR(64) NOT NULL,
                `stat_year` CHAR(4) NOT NULL,
                `total_usage` DECIMAL(12,3) DEFAULT NULL,
                `total_charge` DECIMAL(12,2) DEFAULT NULL,
                `valley_usage` DECIMAL(12,3) DEFAULT NULL,
                `flat_usage` DECIMAL(12,3) DEFAULT NULL,
                `peak_usage` DECIMAL(12,3) DEFAULT NULL,
                `tip_usage` DECIMAL(12,3) DEFAULT NULL,
                `source` VARCHAR(16) NOT NULL DEFAULT 'calculated',
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`user_id`, `stat_year`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self.BALANCE_TABLE}` (
                `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                `user_id` VARCHAR(64) NOT NULL,
                `balance` DECIMAL(12,2) DEFAULT NULL,
                `amount_due` DECIMAL(12,2) DEFAULT NULL,
                `as_of` DATETIME DEFAULT NULL,
                `recorded_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (`id`),
                KEY `idx_sg_balance_user_time` (`user_id`, `recorded_at`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self.PAYMENT_TABLE}` (
                `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                `user_id` VARCHAR(64) NOT NULL,
                `pay_date` DATETIME NOT NULL,
                `amount` DECIMAL(12,2) NOT NULL,
                `pay_mode` VARCHAR(64) DEFAULT NULL,
                `total_rep_amt` DECIMAL(12,2) DEFAULT NULL,
                `cons_no` VARCHAR(64) DEFAULT NULL,
                `raw_json` LONGTEXT DEFAULT NULL,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`id`),
                UNIQUE KEY `uk_sg_payment` (`user_id`, `pay_date`, `amount`, `pay_mode`),
                KEY `idx_sg_payment_user_date` (`user_id`, `pay_date`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self.BILL_TABLE}` (
                `user_id` VARCHAR(64) NOT NULL,
                `stat_month` CHAR(7) NOT NULL,
                `total_usage` DECIMAL(12,3) DEFAULT NULL,
                `total_charge` DECIMAL(12,2) DEFAULT NULL,
                `valley_usage` DECIMAL(12,3) DEFAULT NULL,
                `flat_usage` DECIMAL(12,3) DEFAULT NULL,
                `peak_usage` DECIMAL(12,3) DEFAULT NULL,
                `tip_usage` DECIMAL(12,3) DEFAULT NULL,
                `year_usage` DECIMAL(12,3) DEFAULT NULL,
                `year_charge` DECIMAL(12,2) DEFAULT NULL,
                `one_level_pq` DECIMAL(12,3) DEFAULT NULL,
                `two_level_pq` DECIMAL(12,3) DEFAULT NULL,
                `total_year_pq` DECIMAL(12,3) DEFAULT NULL,
                `total_month_pq` DECIMAL(12,3) DEFAULT NULL,
                `year_used_pq` DECIMAL(12,3) DEFAULT NULL,
                `level_flag` INT DEFAULT NULL,
                `ladder` VARCHAR(8) DEFAULT NULL,
                `ladder_num` VARCHAR(8) DEFAULT NULL,
                `bill_settle_type` VARCHAR(8) DEFAULT NULL,
                `begin_date` DATE DEFAULT NULL,
                `end_date` DATE DEFAULT NULL,
                `issue_date` DATE DEFAULT NULL,
                `source` VARCHAR(16) NOT NULL DEFAULT 'official',
                `raw_json` LONGTEXT DEFAULT NULL,
                `fetched_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (`user_id`, `stat_month`),
                KEY `idx_sg_bill_month` (`stat_month`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            f"""
            CREATE TABLE IF NOT EXISTS `{self.TIER_TABLE}` (
                `user_id` VARCHAR(64) NOT NULL,
                `snapshot_date` DATE NOT NULL,
                `level_flag` INT DEFAULT NULL,
                `one_level_pq` DECIMAL(12,3) DEFAULT NULL,
                `two_level_pq` DECIMAL(12,3) DEFAULT NULL,
                `total_year_pq` DECIMAL(12,3) DEFAULT NULL,
                `total_month_pq` DECIMAL(12,3) DEFAULT NULL,
                `year_used_pq` DECIMAL(12,3) DEFAULT NULL,
                `ladder` VARCHAR(8) DEFAULT NULL,
                `ladder_num` VARCHAR(8) DEFAULT NULL,
                `official_tier` INT DEFAULT NULL,
                `source` VARCHAR(32) NOT NULL DEFAULT 'official',
                `raw_json` LONGTEXT DEFAULT NULL,
                `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`user_id`, `snapshot_date`),
                KEY `idx_sg_tier_user_date` (`user_id`, `snapshot_date`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
        )
        cursor = self._cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
            self.connect.commit()
        finally:
            cursor.close()
        self._ensure_column(self.USERS_TABLE, "data_updated_at", "DATETIME DEFAULT NULL")

    def _ensure_column(self, table_name: str, column_name: str, column_definition: str) -> None:
        cursor = self._cursor()
        try:
            cursor.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s
                """,
                (table_name, column_name),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {column_definition}"
                )
                self.connect.commit()
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def upsert_user(self, user_id: Any, account: str = "", user_name: str = "") -> bool:
        try:
            uid = self._normalize_user_id(user_id)
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{self.USERS_TABLE}` (`user_id`, `account`, `user_name`)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `account` = COALESCE(VALUES(`account`), `account`),
                        `user_name` = COALESCE(VALUES(`user_name`), `user_name`),
                        `last_seen_at` = CURRENT_TIMESTAMP
                    """,
                    (uid, self._normalize_text(account, 64), self._normalize_text(user_name)),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入用户信息失败: %s", exc)
            return False

    def touch_user_data(self, user_id: Any) -> bool:
        try:
            uid = self._normalize_user_id(user_id)
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"UPDATE `{self.USERS_TABLE}` SET `data_updated_at` = CURRENT_TIMESTAMP WHERE `user_id` = %s",
                    (uid,),
                )
                self.connect.commit()
                return cursor.rowcount > 0
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("更新数据更新时间失败: %s", exc)
            return False
    def insert_daily_data(self, data: Dict[str, Any]) -> bool:
        values = self._daily_values(data)
        if values is None:
            logging.debug("跳过无效的日用电数据: %s", data)
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{self.DAILY_TABLE}` (
                        `user_id`, `stat_date`, `total_usage`, `total_charge`,
                        `valley_usage`, `flat_usage`, `peak_usage`, `tip_usage`, `source`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `total_usage` = COALESCE(VALUES(`total_usage`), `total_usage`),
                        `total_charge` = COALESCE(VALUES(`total_charge`), `total_charge`),
                        `valley_usage` = COALESCE(VALUES(`valley_usage`), `valley_usage`),
                        `flat_usage` = COALESCE(VALUES(`flat_usage`), `flat_usage`),
                        `peak_usage` = COALESCE(VALUES(`peak_usage`), `peak_usage`),
                        `tip_usage` = COALESCE(VALUES(`tip_usage`), `tip_usage`),
                        `source` = VALUES(`source`),
                        `updated_at` = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["stat_date"],
                        values["total_usage"],
                        values["total_charge"],
                        values["valley_usage"],
                        values["flat_usage"],
                        values["peak_usage"],
                        values["tip_usage"],
                        values["source"],
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入日用电数据失败: %s", exc)
            return False

    def _existing_source(self, table: str, period_column: str, period_value: str) -> Optional[str]:
        cursor = self._cursor()
        try:
            cursor.execute(
                f"SELECT `source` FROM `{table}` WHERE `user_id` = %s AND `{period_column}` = %s",
                (self.user_id, period_value),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()

    def insert_monthly_data(self, data: Dict[str, Any]) -> bool:
        values = self._monthly_values(data)
        if values is None:
            logging.debug("跳过无效的月度数据: %s", data)
            return False
        try:
            existing_source = self._existing_source(
                self.MONTHLY_TABLE, "stat_month", values["stat_month"]
            )
            source = values["source"]
            # 官方账单优先级最高；旧数据不覆盖新数据。
            if existing_source == self.MONTHLY_SOURCE_OFFICIAL and source != self.MONTHLY_SOURCE_OFFICIAL:
                logging.debug("已存在官方账单 %s，跳过 %s 数据。", values["stat_month"], source)
                return True
            if source == self.MONTHLY_SOURCE_LEGACY and existing_source is not None:
                return True

            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{self.MONTHLY_TABLE}` (
                        `user_id`, `stat_month`, `total_usage`, `total_charge`,
                        `valley_usage`, `flat_usage`, `peak_usage`, `tip_usage`, `source`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `total_usage` = COALESCE(VALUES(`total_usage`), `total_usage`),
                        `total_charge` = COALESCE(VALUES(`total_charge`), `total_charge`),
                        `valley_usage` = COALESCE(VALUES(`valley_usage`), `valley_usage`),
                        `flat_usage` = COALESCE(VALUES(`flat_usage`), `flat_usage`),
                        `peak_usage` = COALESCE(VALUES(`peak_usage`), `peak_usage`),
                        `tip_usage` = COALESCE(VALUES(`tip_usage`), `tip_usage`),
                        `source` = VALUES(`source`),
                        `updated_at` = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["stat_month"],
                        values["total_usage"],
                        values["total_charge"],
                        values["valley_usage"],
                        values["flat_usage"],
                        values["peak_usage"],
                        values["tip_usage"],
                        source,
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入月度数据失败: %s", exc)
            return False

    def insert_yearly_data(self, data: Dict[str, Any]) -> bool:
        values = self._yearly_values(data)
        if values is None:
            logging.debug("跳过无效的年度数据: %s", data)
            return False
        try:
            existing_source = self._existing_source(
                self.YEARLY_TABLE, "stat_year", values["stat_year"]
            )
            source = values["source"]
            if not data.get("force"):
                if existing_source == self.MONTHLY_SOURCE_OFFICIAL and source != self.MONTHLY_SOURCE_OFFICIAL:
                    return True
                if source == self.MONTHLY_SOURCE_LEGACY and existing_source is not None:
                    return True

            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{self.YEARLY_TABLE}` (
                        `user_id`, `stat_year`, `total_usage`, `total_charge`,
                        `valley_usage`, `flat_usage`, `peak_usage`, `tip_usage`, `source`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `total_usage` = COALESCE(VALUES(`total_usage`), `total_usage`),
                        `total_charge` = COALESCE(VALUES(`total_charge`), `total_charge`),
                        `valley_usage` = COALESCE(VALUES(`valley_usage`), `valley_usage`),
                        `flat_usage` = COALESCE(VALUES(`flat_usage`), `flat_usage`),
                        `peak_usage` = COALESCE(VALUES(`peak_usage`), `peak_usage`),
                        `tip_usage` = COALESCE(VALUES(`tip_usage`), `tip_usage`),
                        `source` = VALUES(`source`),
                        `updated_at` = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["stat_year"],
                        values["total_usage"],
                        values["total_charge"],
                        values["valley_usage"],
                        values["flat_usage"],
                        values["peak_usage"],
                        values["tip_usage"],
                        source,
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入年度数据失败: %s", exc)
            return False

    def insert_balance_log(self, data: Dict[str, Any]) -> bool:
        values = self._balance_values(data)
        if values is None:
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{self.BALANCE_TABLE}`
                        (`user_id`, `balance`, `amount_due`, `as_of`, `recorded_at`)
                    VALUES (%s, %s, %s, %s, NOW())
                    """,
                    (self.user_id, values["balance"], values["amount_due"], values["as_of"]),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入余额历史失败: %s", exc)
            return False

    def insert_payment(self, data: Dict[str, Any]) -> bool:
        values = self._payment_values(data)
        if values is None:
            logging.debug("跳过无效的交费记录: %s", data)
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{self.PAYMENT_TABLE}` (
                        `user_id`, `pay_date`, `amount`, `pay_mode`,
                        `total_rep_amt`, `cons_no`, `raw_json`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `total_rep_amt` = VALUES(`total_rep_amt`),
                        `cons_no` = VALUES(`cons_no`),
                        `raw_json` = VALUES(`raw_json`),
                        `updated_at` = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["pay_date"],
                        values["amount"],
                        values["pay_mode"],
                        values["total_rep_amt"],
                        values["cons_no"],
                        values["raw_json"],
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入交费记录失败: %s", exc)
            return False

    def get_latest_payment(self) -> Optional[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT `pay_date`, `amount`, `pay_mode`, `total_rep_amt`
                    FROM `{self.PAYMENT_TABLE}`
                    WHERE `user_id` = %s
                    ORDER BY `pay_date` DESC, `id` DESC
                    LIMIT 1
                    """,
                    (self.user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "pay_date": normalize_datetime(row[0]),
                    "amount": to_float(row[1]),
                    "pay_mode": row[2],
                    "total_rep_amt": to_float(row[3]),
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取最近交费记录失败: %s", exc)
            return None

    def insert_monthly_bill(self, data: Dict[str, Any]) -> bool:
        values = self._bill_values(data)
        if values is None:
            logging.debug("跳过无效的月度账单: %s", data)
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{self.BILL_TABLE}` (
                        `user_id`, `stat_month`, `total_usage`, `total_charge`,
                        `valley_usage`, `flat_usage`, `peak_usage`, `tip_usage`,
                        `year_usage`, `year_charge`, `one_level_pq`, `two_level_pq`,
                        `total_year_pq`, `total_month_pq`, `year_used_pq`,
                        `level_flag`, `ladder`, `ladder_num`, `bill_settle_type`,
                        `begin_date`, `end_date`, `issue_date`, `source`, `raw_json`
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                        `total_usage` = VALUES(`total_usage`),
                        `total_charge` = VALUES(`total_charge`),
                        `valley_usage` = VALUES(`valley_usage`),
                        `flat_usage` = VALUES(`flat_usage`),
                        `peak_usage` = VALUES(`peak_usage`),
                        `tip_usage` = VALUES(`tip_usage`),
                        `year_usage` = VALUES(`year_usage`),
                        `year_charge` = VALUES(`year_charge`),
                        `one_level_pq` = VALUES(`one_level_pq`),
                        `two_level_pq` = VALUES(`two_level_pq`),
                        `total_year_pq` = VALUES(`total_year_pq`),
                        `total_month_pq` = VALUES(`total_month_pq`),
                        `year_used_pq` = VALUES(`year_used_pq`),
                        `level_flag` = VALUES(`level_flag`),
                        `ladder` = VALUES(`ladder`),
                        `ladder_num` = VALUES(`ladder_num`),
                        `bill_settle_type` = VALUES(`bill_settle_type`),
                        `begin_date` = VALUES(`begin_date`),
                        `end_date` = VALUES(`end_date`),
                        `issue_date` = VALUES(`issue_date`),
                        `source` = VALUES(`source`),
                        `raw_json` = VALUES(`raw_json`),
                        `fetched_at` = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["stat_month"],
                        values["total_usage"],
                        values["total_charge"],
                        values["valley_usage"],
                        values["flat_usage"],
                        values["peak_usage"],
                        values["tip_usage"],
                        values["year_usage"],
                        values["year_charge"],
                        values["one_level_pq"],
                        values["two_level_pq"],
                        values["total_year_pq"],
                        values["total_month_pq"],
                        values["year_used_pq"],
                        values["level_flag"],
                        values["ladder"],
                        values["ladder_num"],
                        values["bill_settle_type"],
                        values["begin_date"],
                        values["end_date"],
                        values["issue_date"],
                        values["source"],
                        values["raw_json"],
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入月度账单失败: %s", exc)
            return False

    def get_monthly_bill(self, month: str) -> Optional[Dict[str, Any]]:
        month_key = normalize_month(month)
        if not month_key:
            return None
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"SELECT * FROM `{self.BILL_TABLE}` WHERE `user_id` = %s AND `stat_month` = %s",
                    (self.user_id, month_key),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                columns = [description[0] for description in cursor.description]
                result = dict(zip(columns, row))
                for key in (
                    "total_usage", "total_charge", "valley_usage", "flat_usage",
                    "peak_usage", "tip_usage", "year_usage", "year_charge",
                    "one_level_pq", "two_level_pq", "total_year_pq",
                    "total_month_pq", "year_used_pq",
                ):
                    if result.get(key) is not None:
                        result[key] = float(result[key])
                for key in ("begin_date", "end_date", "issue_date", "fetched_at"):
                    if result.get(key) is not None:
                        result[key] = str(result[key])
                return result
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取月度账单失败: %s", exc)
            return None

    def get_official_bill_months(self, year: str) -> set[str]:
        year_key = normalize_year(year)
        if not year_key:
            return set()
        months: set[str] = set()
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"SELECT `stat_month` FROM `{self.BILL_TABLE}` WHERE `user_id` = %s AND `stat_month` LIKE %s",
                    (self.user_id, f"{year_key}-%"),
                )
                months.update(row[0] for row in cursor.fetchall() if row[0])
                cursor.execute(
                    f"SELECT `stat_month` FROM `{self.MONTHLY_TABLE}` WHERE `user_id` = %s AND `source` = %s AND `stat_month` LIKE %s",
                    (self.user_id, self.MONTHLY_SOURCE_OFFICIAL, f"{year_key}-%"),
                )
                months.update(row[0] for row in cursor.fetchall() if row[0])
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取官方账单月份失败: %s", exc)
        return months

    def insert_tier_snapshot(self, data: Dict[str, Any]) -> bool:
        values = self._tier_values(data)
        if values is None:
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO `{self.TIER_TABLE}` (
                        `user_id`, `snapshot_date`, `level_flag`, `one_level_pq`,
                        `two_level_pq`, `total_year_pq`, `total_month_pq`,
                        `year_used_pq`, `ladder`, `ladder_num`, `official_tier`,
                        `source`, `raw_json`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `level_flag` = VALUES(`level_flag`),
                        `one_level_pq` = VALUES(`one_level_pq`),
                        `two_level_pq` = VALUES(`two_level_pq`),
                        `total_year_pq` = VALUES(`total_year_pq`),
                        `total_month_pq` = VALUES(`total_month_pq`),
                        `year_used_pq` = VALUES(`year_used_pq`),
                        `ladder` = VALUES(`ladder`),
                        `ladder_num` = VALUES(`ladder_num`),
                        `official_tier` = VALUES(`official_tier`),
                        `source` = VALUES(`source`),
                        `raw_json` = VALUES(`raw_json`),
                        `updated_at` = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["snapshot_date"],
                        values["level_flag"],
                        values["one_level_pq"],
                        values["two_level_pq"],
                        values["total_year_pq"],
                        values["total_month_pq"],
                        values["year_used_pq"],
                        values["ladder"],
                        values["ladder_num"],
                        values["official_tier"],
                        values["source"],
                        values["raw_json"],
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入阶梯快照失败: %s", exc)
            return False

    def get_latest_tier_snapshot(self) -> Optional[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT `snapshot_date`, `level_flag`, `one_level_pq`, `two_level_pq`,
                           `total_year_pq`, `total_month_pq`, `year_used_pq`,
                           `ladder`, `ladder_num`, `official_tier`, `source`
                    FROM `{self.TIER_TABLE}`
                    WHERE `user_id` = %s
                    ORDER BY `snapshot_date` DESC
                    LIMIT 1
                    """,
                    (self.user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "snapshot_date": str(row[0]),
                    "level_flag": int(to_float(row[1], default=0) or 0),
                    "one_level_pq": to_float(row[2]),
                    "two_level_pq": to_float(row[3]),
                    "total_year_pq": to_float(row[4]),
                    "total_month_pq": to_float(row[5]),
                    "year_used_pq": to_float(row[6]),
                    "ladder": row[7],
                    "ladder_num": row[8],
                    "official_tier": int(to_float(row[9], default=0) or 0),
                    "source": row[10],
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取阶梯快照失败: %s", exc)
            return None

    def get_monthly_usage_rows(self, year: str) -> list[Dict[str, Any]]:
        year_key = normalize_year(year)
        if not year_key:
            return []
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT `stat_month`, `total_usage`, `total_charge`,
                           `valley_usage`, `flat_usage`, `peak_usage`, `tip_usage`, `source`
                    FROM `{self.MONTHLY_TABLE}`
                    WHERE `user_id` = %s AND `stat_month` LIKE %s
                    ORDER BY `stat_month`
                    """,
                    (self.user_id, f"{year_key}-%"),
                )
                return [
                    {
                        "month": row[0],
                        "total_usage": to_float(row[1]),
                        "total_charge": to_float(row[2]),
                        "valley_usage": to_float(row[3]),
                        "flat_usage": to_float(row[4]),
                        "peak_usage": to_float(row[5]),
                        "tip_usage": to_float(row[6]),
                        "source": row[7],
                    }
                    for row in cursor.fetchall()
                ]
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取月度汇总失败: %s", exc)
            return []

    def get_daily_summary(self, start_date: str, end_date: str) -> Dict[str, Any]:
        start = normalize_date(start_date)
        end = normalize_date(end_date)
        if not start or not end:
            return {"count": 0}
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT
                        COALESCE(SUM(`total_usage`), 0),
                        COALESCE(SUM(`total_charge`), 0),
                        COALESCE(SUM(`valley_usage`), 0),
                        COALESCE(SUM(`flat_usage`), 0),
                        COALESCE(SUM(`peak_usage`), 0),
                        COALESCE(SUM(`tip_usage`), 0),
                        COUNT(*)
                    FROM `{self.DAILY_TABLE}`
                    WHERE `user_id` = %s AND `stat_date` BETWEEN %s AND %s
                    """,
                    (self.user_id, start, end),
                )
                row = cursor.fetchone()
                return {
                    "total_usage": to_float(row[0], default=0.0) or 0.0,
                    "total_charge": to_float(row[1], default=0.0) or 0.0,
                    "valley_usage": to_float(row[2], default=0.0) or 0.0,
                    "flat_usage": to_float(row[3], default=0.0) or 0.0,
                    "peak_usage": to_float(row[4], default=0.0) or 0.0,
                    "tip_usage": to_float(row[5], default=0.0) or 0.0,
                    "count": int(row[6] or 0),
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取日数据汇总失败: %s", exc)
            return {"count": 0}

    # ------------------------------------------------------------------
    # legacy migration
    # ------------------------------------------------------------------
    def _table_exists(self, table_name: str) -> bool:
        cursor = self._cursor()
        try:
            cursor.execute(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = DATABASE() AND table_name = %s
                """,
                (table_name,),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()

    def _user_exists(self, user_id: str) -> bool:
        cursor = self._cursor()
        try:
            cursor.execute(
                f"SELECT 1 FROM `{self.USERS_TABLE}` WHERE `user_id` = %s",
                (user_id,),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()

    def _migrate_legacy_if_needed(self) -> None:
        """把旧版 daily{user_id} / data{user_id} 表迁移到新结构。

        迁移只在新表里没有该户号时执行一次，使用 INSERT IGNORE，绝不覆盖
        新结构里已有的数据；旧表保持原样，便于回滚。
        """
        uid = self.user_id
        if self._user_exists(uid):
            return

        legacy_daily = f"daily{uid}"
        legacy_data = f"data{uid}"
        legacy_user_name = ""
        migrated = {"daily": 0, "monthly": 0, "yearly": 0, "balance": 0}

        cursor = self._cursor()
        try:
            if self._table_exists(legacy_daily):
                cursor.execute(
                    f"""
                    INSERT IGNORE INTO `{self.DAILY_TABLE}`
                        (`user_id`, `stat_date`, `total_usage`, `source`)
                    SELECT %s, `date`, `usage`, 'legacy'
                    FROM `{legacy_daily}`
                    WHERE `date` IS NOT NULL
                    """,
                    (uid,),
                )
                migrated["daily"] = cursor.rowcount

            if self._table_exists(legacy_data):
                cursor.execute(f"SELECT `name`, `value` FROM `{legacy_data}`")
                rows = cursor.fetchall()
                for name, value in rows:
                    name = str(name or "")
                    value = "" if value is None else str(value)
                    if name == "user_info":
                        parts = value.split("|")
                        if len(parts) >= 3:
                            legacy_user_name = parts[2].strip()
                    elif name.startswith("month_"):
                        parts = value.split("|")
                        parts += [""] * (7 - len(parts))
                        cursor.execute(
                            f"""
                            INSERT IGNORE INTO `{self.MONTHLY_TABLE}`
                                (`user_id`, `stat_month`, `total_usage`, `total_charge`,
                                 `valley_usage`, `flat_usage`, `peak_usage`, `tip_usage`, `source`)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'legacy')
                            """,
                            (
                                uid,
                                normalize_month(name[6:]),
                                to_float(parts[0]),
                                to_float(parts[1]),
                                to_float(parts[2]),
                                to_float(parts[3]),
                                to_float(parts[4]),
                                to_float(parts[5]),
                            ),
                        )
                        migrated["monthly"] += max(cursor.rowcount, 0)
                    elif name.startswith("year_"):
                        parts = value.split("|")
                        parts += [""] * (3 - len(parts))
                        cursor.execute(
                            f"""
                            INSERT IGNORE INTO `{self.YEARLY_TABLE}`
                                (`user_id`, `stat_year`, `total_usage`, `total_charge`, `source`)
                            VALUES (%s, %s, %s, %s, 'legacy')
                            """,
                            (
                                uid,
                                normalize_year(name[5:]),
                                to_float(parts[0]),
                                to_float(parts[1]),
                            ),
                        )
                        migrated["yearly"] += max(cursor.rowcount, 0)
                    elif name == "balance_latest":
                        cursor.execute(
                            f"SELECT 1 FROM `{self.BALANCE_TABLE}` WHERE `user_id` = %s LIMIT 1",
                            (uid,),
                        )
                        if cursor.fetchone() is None:
                            parts = value.split("|")
                            parts += [""] * (4 - len(parts))
                            cursor.execute(
                                f"""
                                INSERT INTO `{self.BALANCE_TABLE}`
                                    (`user_id`, `balance`, `amount_due`, `as_of`, `recorded_at`)
                                VALUES (%s, %s, %s, %s, NOW())
                                """,
                                (
                                    uid,
                                    to_float(parts[0]),
                                    to_float(parts[3]),
                                    normalize_datetime(parts[2]),
                                ),
                            )
                            migrated["balance"] += 1
        finally:
            cursor.close()

        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                INSERT INTO `{self.USERS_TABLE}` (`user_id`, `user_name`)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE
                    `user_name` = COALESCE(`user_name`, VALUES(`user_name`)),
                    `last_seen_at` = CURRENT_TIMESTAMP
                """,
                (uid, self._normalize_text(legacy_user_name)),
            )
            self.connect.commit()
        finally:
            cursor.close()

        if any(migrated.values()):
            logging.info(
                "旧表迁移完成: 日数据 %s 条，月数据 %s 条，年数据 %s 条，余额 %s 条。",
                migrated["daily"], migrated["monthly"], migrated["yearly"], migrated["balance"],
            )

    # ------------------------------------------------------------------
    # cleanup / close
    # ------------------------------------------------------------------
    def cleanup_old_data(self) -> None:
        retention_days = self._retention_days()
        if retention_days <= 0:
            if not self._retention_logged:
                logging.info("DATA_RETENTION_DAYS=%s，数据永久保留。", retention_days)
                self._retention_logged = True
            return

        cutoff_date = (date.today() - timedelta(days=retention_days)).isoformat()
        cursor = self._cursor()
        try:
            cursor.execute(
                f"DELETE FROM `{self.DAILY_TABLE}` WHERE `user_id` = %s AND `stat_date` < %s",
                (self.user_id, cutoff_date),
            )
            daily_deleted = cursor.rowcount
            cursor.execute(
                f"DELETE FROM `{self.BALANCE_TABLE}` WHERE `user_id` = %s AND DATE(`recorded_at`) < %s",
                (self.user_id, cutoff_date),
            )
            balance_deleted = cursor.rowcount
            self.connect.commit()
            logging.info(
                "数据清理完成：删除 %s 条日数据、%s 条余额历史（保留最近 %s 天）。",
                daily_deleted, balance_deleted, retention_days,
            )
        except Exception as exc:
            logging.error("清理旧数据失败: %s", exc)
        finally:
            cursor.close()

    def close_connect(self) -> None:
        if self.connect is not None:
            try:
                if getattr(self.connect, "is_connected", lambda: False)():
                    self.connect.close()
            except Exception:
                pass
            finally:
                self.connect = None
                self.user_id = None
                logging.info("MySQL 数据库连接已关闭。")

    def ping(self) -> None:
        if self.connect is None:
            raise RuntimeError("数据库连接未建立")
        self.connect.ping(reconnect=True, attempts=3, delay=1)

    def get_monthly_usage_row(self, month: str) -> Optional[Dict[str, Any]]:
        month_key = normalize_month(month)
        if not month_key:
            return None
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT `stat_month`, `total_usage`, `total_charge`,
                           `valley_usage`, `flat_usage`, `peak_usage`, `tip_usage`, `source`
                    FROM `{self.MONTHLY_TABLE}`
                    WHERE `user_id` = %s AND `stat_month` = %s
                    """,
                    (self.user_id, month_key),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "month": row[0],
                    "total_usage": to_float(row[1]),
                    "total_charge": to_float(row[2]),
                    "valley_usage": to_float(row[3]),
                    "flat_usage": to_float(row[4]),
                    "peak_usage": to_float(row[5]),
                    "tip_usage": to_float(row[6]),
                    "source": row[7],
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取月度汇总失败: %s", exc)
            return None

    def get_yearly_row(self, year: str) -> Optional[Dict[str, Any]]:
        year_key = normalize_year(year)
        if not year_key:
            return None
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT `stat_year`, `total_usage`, `total_charge`,
                           `valley_usage`, `flat_usage`, `peak_usage`, `tip_usage`, `source`
                    FROM `{self.YEARLY_TABLE}`
                    WHERE `user_id` = %s AND `stat_year` = %s
                    """,
                    (self.user_id, year_key),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "year": row[0],
                    "total_usage": to_float(row[1]),
                    "total_charge": to_float(row[2]),
                    "valley_usage": to_float(row[3]),
                    "flat_usage": to_float(row[4]),
                    "peak_usage": to_float(row[5]),
                    "tip_usage": to_float(row[6]),
                    "source": row[7],
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取年度汇总失败: %s", exc)
            return None

    def get_latest_balance(self) -> Optional[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT `balance`, `amount_due`, `as_of`, `recorded_at`
                    FROM `{self.BALANCE_TABLE}`
                    WHERE `user_id` = %s
                    ORDER BY `id` DESC
                    LIMIT 1
                    """,
                    (self.user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "balance": to_float(row[0]),
                    "amount_due": to_float(row[1]),
                    "as_of": normalize_datetime(row[2]),
                    "recorded_at": normalize_datetime(row[3]),
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取最近余额失败: %s", exc)
            return None

    def get_latest_daily(self) -> Optional[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT `stat_date`, `total_usage`, `total_charge`,
                           `valley_usage`, `flat_usage`, `peak_usage`, `tip_usage`, `source`
                    FROM `{self.DAILY_TABLE}`
                    WHERE `user_id` = %s
                    ORDER BY `stat_date` DESC
                    LIMIT 1
                    """,
                    (self.user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "date": normalize_date(row[0]),
                    "total_usage": to_float(row[1]),
                    "total_charge": to_float(row[2]),
                    "valley_usage": to_float(row[3]),
                    "flat_usage": to_float(row[4]),
                    "peak_usage": to_float(row[5]),
                    "tip_usage": to_float(row[6]),
                    "source": row[7],
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取最近日数据失败: %s", exc)
            return None

    def get_user(self, user_id: Any) -> Optional[Dict[str, Any]]:
        try:
            uid = self._normalize_user_id(user_id)
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"SELECT `user_id`, `account`, `user_name`, `data_updated_at` FROM `{self.USERS_TABLE}` WHERE `user_id` = %s",
                    (uid,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "user_id": row[0],
                    "account": row[1],
                    "user_name": row[2],
                    "data_updated_at": normalize_datetime(row[3]),
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取用户信息失败: %s", exc)
            return None

    def get_users(self) -> list[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"SELECT `user_id`, `account`, `user_name`, `data_updated_at` FROM `{self.USERS_TABLE}` ORDER BY `user_id`"
                )
                return [
                    {
                        "user_id": row[0],
                        "account": row[1],
                        "user_name": row[2],
                        "data_updated_at": normalize_datetime(row[3]),
                    }
                    for row in cursor.fetchall()
                ]
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取用户列表失败: %s", exc)
            return []

    def get_daily_dates(self, start_date: str, end_date: str) -> set[str]:
        start = normalize_date(start_date)
        end = normalize_date(end_date)
        if not start or not end:
            return set()
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT `stat_date` FROM `{self.DAILY_TABLE}`
                    WHERE `user_id` = %s AND `stat_date` BETWEEN %s AND %s
                    """,
                    (self.user_id, start, end),
                )
                return {
                    normalize_date(row[0])
                    for row in cursor.fetchall()
                    if row[0] is not None
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取日数据日期失败: %s", exc)
            return set()


class SqliteDB(DB):
    def _resolve_db_path(self) -> str:
        db_name = os.getenv("DB_NAME", "homeassistant.db")
        if os.path.isabs(db_name):
            return db_name
        from const import get_data_dir
        return os.path.join(get_data_dir(), os.path.basename(db_name))

    def connect_user_db(self, user_id: Any) -> bool:
        self.close_connect()
        try:
            self.user_id = self._normalize_user_id(user_id)
            db_path = self._resolve_db_path()
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            self.connect = sqlite3.connect(db_path, timeout=30)
            self.connect.execute("PRAGMA journal_mode=WAL")
            self.connect.execute("PRAGMA synchronous=NORMAL")
            self.connect.execute("PRAGMA busy_timeout=5000")
            self._create_schema()
            self._migrate_legacy_if_needed()
            logging.info("已连接 SQLite 数据库 %s，当前户号 %s。", db_path, self.user_id)
            return True
        except Exception as exc:
            logging.error("连接 SQLite 数据库失败: %s", exc)
            self.connect = None
            return False

    def connect_users_db(self) -> bool:
        self.close_connect()
        try:
            db_path = self._resolve_db_path()
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            self.connect = sqlite3.connect(db_path, timeout=30)
            self.connect.execute("PRAGMA journal_mode=WAL")
            self.connect.execute("PRAGMA synchronous=NORMAL")
            self.connect.execute("PRAGMA busy_timeout=5000")
            self._create_schema()
            return True
        except Exception as exc:
            logging.error("连接 SQLite 数据库失败: %s", exc)
            self.connect = None
            return False

    def _cursor(self):
        if self.connect is None:
            raise RuntimeError("数据库连接未建立")
        return self.connect.cursor()

    def _create_schema(self) -> None:
        statements = (
            f"""
            CREATE TABLE IF NOT EXISTS {self.USERS_TABLE} (
                user_id TEXT NOT NULL PRIMARY KEY,
                account TEXT,
                user_name TEXT,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                data_updated_at TEXT
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.DAILY_TABLE} (
                user_id TEXT NOT NULL,
                stat_date TEXT NOT NULL,
                total_usage REAL,
                total_charge REAL,
                valley_usage REAL,
                flat_usage REAL,
                peak_usage REAL,
                tip_usage REAL,
                source TEXT NOT NULL DEFAULT 'calculated',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, stat_date)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.MONTHLY_TABLE} (
                user_id TEXT NOT NULL,
                stat_month TEXT NOT NULL,
                total_usage REAL,
                total_charge REAL,
                valley_usage REAL,
                flat_usage REAL,
                peak_usage REAL,
                tip_usage REAL,
                source TEXT NOT NULL DEFAULT 'calculated',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, stat_month)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.YEARLY_TABLE} (
                user_id TEXT NOT NULL,
                stat_year TEXT NOT NULL,
                total_usage REAL,
                total_charge REAL,
                valley_usage REAL,
                flat_usage REAL,
                peak_usage REAL,
                tip_usage REAL,
                source TEXT NOT NULL DEFAULT 'calculated',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, stat_year)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.BALANCE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                balance REAL,
                amount_due REAL,
                as_of TEXT,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.PAYMENT_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                pay_date TEXT NOT NULL,
                amount REAL NOT NULL,
                pay_mode TEXT NOT NULL DEFAULT '',
                total_rep_amt REAL,
                cons_no TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, pay_date, amount, pay_mode)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.BILL_TABLE} (
                user_id TEXT NOT NULL,
                stat_month TEXT NOT NULL,
                total_usage REAL,
                total_charge REAL,
                valley_usage REAL,
                flat_usage REAL,
                peak_usage REAL,
                tip_usage REAL,
                year_usage REAL,
                year_charge REAL,
                one_level_pq REAL,
                two_level_pq REAL,
                total_year_pq REAL,
                total_month_pq REAL,
                year_used_pq REAL,
                level_flag INTEGER,
                ladder TEXT,
                ladder_num TEXT,
                bill_settle_type TEXT,
                begin_date TEXT,
                end_date TEXT,
                issue_date TEXT,
                source TEXT NOT NULL DEFAULT 'official',
                raw_json TEXT,
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, stat_month)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {self.TIER_TABLE} (
                user_id TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                level_flag INTEGER,
                one_level_pq REAL,
                two_level_pq REAL,
                total_year_pq REAL,
                total_month_pq REAL,
                year_used_pq REAL,
                ladder TEXT,
                ladder_num TEXT,
                official_tier INTEGER,
                source TEXT NOT NULL DEFAULT 'official',
                raw_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, snapshot_date)
            )
            """,
            f"CREATE INDEX IF NOT EXISTS idx_sg_daily_date ON {self.DAILY_TABLE}(stat_date)",
            f"CREATE INDEX IF NOT EXISTS idx_sg_monthly_month ON {self.MONTHLY_TABLE}(stat_month)",
            f"CREATE INDEX IF NOT EXISTS idx_sg_balance_user_time ON {self.BALANCE_TABLE}(user_id, recorded_at)",
            f"CREATE INDEX IF NOT EXISTS idx_sg_payment_user_date ON {self.PAYMENT_TABLE}(user_id, pay_date)",
            f"CREATE INDEX IF NOT EXISTS idx_sg_bill_month ON {self.BILL_TABLE}(stat_month)",
            f"CREATE INDEX IF NOT EXISTS idx_sg_tier_user_date ON {self.TIER_TABLE}(user_id, snapshot_date)",
        )
        cursor = self._cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
            self.connect.commit()
        finally:
            cursor.close()
        self._ensure_column(self.USERS_TABLE, "data_updated_at", "TEXT")

    def _ensure_column(self, table_name: str, column_name: str, column_definition: str) -> None:
        cursor = self._cursor()
        try:
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = {row[1] for row in cursor.fetchall()}
            if column_name not in columns:
                cursor.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
                )
                self.connect.commit()
        finally:
            cursor.close()

    def upsert_user(self, user_id: Any, account: str = "", user_name: str = "") -> bool:
        try:
            uid = self._normalize_user_id(user_id)
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO {self.USERS_TABLE} (user_id, account, user_name)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        account = COALESCE(excluded.account, account),
                        user_name = COALESCE(excluded.user_name, user_name),
                        last_seen_at = CURRENT_TIMESTAMP
                    """,
                    (uid, self._normalize_text(account, 64), self._normalize_text(user_name)),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入用户信息失败: %s", exc)
            return False

    def touch_user_data(self, user_id: Any) -> bool:
        try:
            uid = self._normalize_user_id(user_id)
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"UPDATE {self.USERS_TABLE} SET data_updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (uid,),
                )
                self.connect.commit()
                return cursor.rowcount > 0
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("更新数据更新时间失败: %s", exc)
            return False
    def insert_daily_data(self, data: Dict[str, Any]) -> bool:
        values = self._daily_values(data)
        if values is None:
            logging.debug("跳过无效的日用电数据: %s", data)
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO {self.DAILY_TABLE} (
                        user_id, stat_date, total_usage, total_charge,
                        valley_usage, flat_usage, peak_usage, tip_usage, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, stat_date) DO UPDATE SET
                        total_usage = COALESCE(excluded.total_usage, total_usage),
                        total_charge = COALESCE(excluded.total_charge, total_charge),
                        valley_usage = COALESCE(excluded.valley_usage, valley_usage),
                        flat_usage = COALESCE(excluded.flat_usage, flat_usage),
                        peak_usage = COALESCE(excluded.peak_usage, peak_usage),
                        tip_usage = COALESCE(excluded.tip_usage, tip_usage),
                        source = excluded.source,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["stat_date"],
                        values["total_usage"],
                        values["total_charge"],
                        values["valley_usage"],
                        values["flat_usage"],
                        values["peak_usage"],
                        values["tip_usage"],
                        values["source"],
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入日用电数据失败: %s", exc)
            return False

    def _existing_source(self, table: str, period_column: str, period_value: str) -> Optional[str]:
        cursor = self._cursor()
        try:
            cursor.execute(
                f"SELECT source FROM {table} WHERE user_id = ? AND {period_column} = ?",
                (self.user_id, period_value),
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()

    def insert_monthly_data(self, data: Dict[str, Any]) -> bool:
        values = self._monthly_values(data)
        if values is None:
            logging.debug("跳过无效的月度数据: %s", data)
            return False
        try:
            existing_source = self._existing_source(
                self.MONTHLY_TABLE, "stat_month", values["stat_month"]
            )
            source = values["source"]
            if existing_source == self.MONTHLY_SOURCE_OFFICIAL and source != self.MONTHLY_SOURCE_OFFICIAL:
                return True
            if source == self.MONTHLY_SOURCE_LEGACY and existing_source is not None:
                return True

            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO {self.MONTHLY_TABLE} (
                        user_id, stat_month, total_usage, total_charge,
                        valley_usage, flat_usage, peak_usage, tip_usage, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, stat_month) DO UPDATE SET
                        total_usage = COALESCE(excluded.total_usage, total_usage),
                        total_charge = COALESCE(excluded.total_charge, total_charge),
                        valley_usage = COALESCE(excluded.valley_usage, valley_usage),
                        flat_usage = COALESCE(excluded.flat_usage, flat_usage),
                        peak_usage = COALESCE(excluded.peak_usage, peak_usage),
                        tip_usage = COALESCE(excluded.tip_usage, tip_usage),
                        source = excluded.source,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["stat_month"],
                        values["total_usage"],
                        values["total_charge"],
                        values["valley_usage"],
                        values["flat_usage"],
                        values["peak_usage"],
                        values["tip_usage"],
                        source,
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入月度数据失败: %s", exc)
            return False

    def insert_yearly_data(self, data: Dict[str, Any]) -> bool:
        values = self._yearly_values(data)
        if values is None:
            logging.debug("跳过无效的年度数据: %s", data)
            return False
        try:
            existing_source = self._existing_source(
                self.YEARLY_TABLE, "stat_year", values["stat_year"]
            )
            source = values["source"]
            if not data.get("force"):
                if existing_source == self.MONTHLY_SOURCE_OFFICIAL and source != self.MONTHLY_SOURCE_OFFICIAL:
                    return True
                if source == self.MONTHLY_SOURCE_LEGACY and existing_source is not None:
                    return True

            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO {self.YEARLY_TABLE} (
                        user_id, stat_year, total_usage, total_charge,
                        valley_usage, flat_usage, peak_usage, tip_usage, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, stat_year) DO UPDATE SET
                        total_usage = COALESCE(excluded.total_usage, total_usage),
                        total_charge = COALESCE(excluded.total_charge, total_charge),
                        valley_usage = COALESCE(excluded.valley_usage, valley_usage),
                        flat_usage = COALESCE(excluded.flat_usage, flat_usage),
                        peak_usage = COALESCE(excluded.peak_usage, peak_usage),
                        tip_usage = COALESCE(excluded.tip_usage, tip_usage),
                        source = excluded.source,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["stat_year"],
                        values["total_usage"],
                        values["total_charge"],
                        values["valley_usage"],
                        values["flat_usage"],
                        values["peak_usage"],
                        values["tip_usage"],
                        source,
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入年度数据失败: %s", exc)
            return False

    def insert_balance_log(self, data: Dict[str, Any]) -> bool:
        values = self._balance_values(data)
        if values is None:
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO {self.BALANCE_TABLE}
                        (user_id, balance, amount_due, as_of, recorded_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (self.user_id, values["balance"], values["amount_due"], values["as_of"]),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入余额历史失败: %s", exc)
            return False

    def insert_payment(self, data: Dict[str, Any]) -> bool:
        values = self._payment_values(data)
        if values is None:
            logging.debug("跳过无效的交费记录: %s", data)
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO {self.PAYMENT_TABLE} (
                        user_id, pay_date, amount, pay_mode,
                        total_rep_amt, cons_no, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, pay_date, amount, pay_mode) DO UPDATE SET
                        total_rep_amt = excluded.total_rep_amt,
                        cons_no = excluded.cons_no,
                        raw_json = excluded.raw_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["pay_date"],
                        values["amount"],
                        values["pay_mode"],
                        values["total_rep_amt"],
                        values["cons_no"],
                        values["raw_json"],
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入交费记录失败: %s", exc)
            return False

    def get_latest_payment(self) -> Optional[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT pay_date, amount, pay_mode, total_rep_amt
                    FROM {self.PAYMENT_TABLE}
                    WHERE user_id = ?
                    ORDER BY pay_date DESC, id DESC
                    LIMIT 1
                    """,
                    (self.user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "pay_date": normalize_datetime(row[0]),
                    "amount": to_float(row[1]),
                    "pay_mode": row[2],
                    "total_rep_amt": to_float(row[3]),
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取最近交费记录失败: %s", exc)
            return None

    def insert_monthly_bill(self, data: Dict[str, Any]) -> bool:
        values = self._bill_values(data)
        if values is None:
            logging.debug("跳过无效的月度账单: %s", data)
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO {self.BILL_TABLE} (
                        user_id, stat_month, total_usage, total_charge,
                        valley_usage, flat_usage, peak_usage, tip_usage,
                        year_usage, year_charge, one_level_pq, two_level_pq,
                        total_year_pq, total_month_pq, year_used_pq,
                        level_flag, ladder, ladder_num, bill_settle_type,
                        begin_date, end_date, issue_date, source, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, stat_month) DO UPDATE SET
                        total_usage = excluded.total_usage,
                        total_charge = excluded.total_charge,
                        valley_usage = excluded.valley_usage,
                        flat_usage = excluded.flat_usage,
                        peak_usage = excluded.peak_usage,
                        tip_usage = excluded.tip_usage,
                        year_usage = excluded.year_usage,
                        year_charge = excluded.year_charge,
                        one_level_pq = excluded.one_level_pq,
                        two_level_pq = excluded.two_level_pq,
                        total_year_pq = excluded.total_year_pq,
                        total_month_pq = excluded.total_month_pq,
                        year_used_pq = excluded.year_used_pq,
                        level_flag = excluded.level_flag,
                        ladder = excluded.ladder,
                        ladder_num = excluded.ladder_num,
                        bill_settle_type = excluded.bill_settle_type,
                        begin_date = excluded.begin_date,
                        end_date = excluded.end_date,
                        issue_date = excluded.issue_date,
                        source = excluded.source,
                        raw_json = excluded.raw_json,
                        fetched_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["stat_month"],
                        values["total_usage"],
                        values["total_charge"],
                        values["valley_usage"],
                        values["flat_usage"],
                        values["peak_usage"],
                        values["tip_usage"],
                        values["year_usage"],
                        values["year_charge"],
                        values["one_level_pq"],
                        values["two_level_pq"],
                        values["total_year_pq"],
                        values["total_month_pq"],
                        values["year_used_pq"],
                        values["level_flag"],
                        values["ladder"],
                        values["ladder_num"],
                        values["bill_settle_type"],
                        values["begin_date"],
                        values["end_date"],
                        values["issue_date"],
                        values["source"],
                        values["raw_json"],
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入月度账单失败: %s", exc)
            return False

    def get_monthly_bill(self, month: str) -> Optional[Dict[str, Any]]:
        month_key = normalize_month(month)
        if not month_key:
            return None
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"SELECT * FROM {self.BILL_TABLE} WHERE user_id = ? AND stat_month = ?",
                    (self.user_id, month_key),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                columns = [description[0] for description in cursor.description]
                return dict(zip(columns, row))
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取月度账单失败: %s", exc)
            return None

    def get_official_bill_months(self, year: str) -> set[str]:
        year_key = normalize_year(year)
        if not year_key:
            return set()
        months: set[str] = set()
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"SELECT stat_month FROM {self.BILL_TABLE} WHERE user_id = ? AND stat_month LIKE ?",
                    (self.user_id, f"{year_key}-%"),
                )
                months.update(row[0] for row in cursor.fetchall() if row[0])
                cursor.execute(
                    f"SELECT stat_month FROM {self.MONTHLY_TABLE} WHERE user_id = ? AND source = ? AND stat_month LIKE ?",
                    (self.user_id, self.MONTHLY_SOURCE_OFFICIAL, f"{year_key}-%"),
                )
                months.update(row[0] for row in cursor.fetchall() if row[0])
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取官方账单月份失败: %s", exc)
        return months

    def insert_tier_snapshot(self, data: Dict[str, Any]) -> bool:
        values = self._tier_values(data)
        if values is None:
            return False
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    INSERT INTO {self.TIER_TABLE} (
                        user_id, snapshot_date, level_flag, one_level_pq,
                        two_level_pq, total_year_pq, total_month_pq,
                        year_used_pq, ladder, ladder_num, official_tier,
                        source, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, snapshot_date) DO UPDATE SET
                        level_flag = excluded.level_flag,
                        one_level_pq = excluded.one_level_pq,
                        two_level_pq = excluded.two_level_pq,
                        total_year_pq = excluded.total_year_pq,
                        total_month_pq = excluded.total_month_pq,
                        year_used_pq = excluded.year_used_pq,
                        ladder = excluded.ladder,
                        ladder_num = excluded.ladder_num,
                        official_tier = excluded.official_tier,
                        source = excluded.source,
                        raw_json = excluded.raw_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self.user_id,
                        values["snapshot_date"],
                        values["level_flag"],
                        values["one_level_pq"],
                        values["two_level_pq"],
                        values["total_year_pq"],
                        values["total_month_pq"],
                        values["year_used_pq"],
                        values["ladder"],
                        values["ladder_num"],
                        values["official_tier"],
                        values["source"],
                        values["raw_json"],
                    ),
                )
                self.connect.commit()
                return True
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("写入阶梯快照失败: %s", exc)
            return False

    def get_latest_tier_snapshot(self) -> Optional[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT snapshot_date, level_flag, one_level_pq, two_level_pq,
                           total_year_pq, total_month_pq, year_used_pq,
                           ladder, ladder_num, official_tier, source
                    FROM {self.TIER_TABLE}
                    WHERE user_id = ?
                    ORDER BY snapshot_date DESC
                    LIMIT 1
                    """,
                    (self.user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "snapshot_date": row[0],
                    "level_flag": int(to_float(row[1], default=0) or 0),
                    "one_level_pq": to_float(row[2]),
                    "two_level_pq": to_float(row[3]),
                    "total_year_pq": to_float(row[4]),
                    "total_month_pq": to_float(row[5]),
                    "year_used_pq": to_float(row[6]),
                    "ladder": row[7],
                    "ladder_num": row[8],
                    "official_tier": int(to_float(row[9], default=0) or 0),
                    "source": row[10],
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取阶梯快照失败: %s", exc)
            return None

    def get_monthly_usage_rows(self, year: str) -> list[Dict[str, Any]]:
        year_key = normalize_year(year)
        if not year_key:
            return []
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT stat_month, total_usage, total_charge,
                           valley_usage, flat_usage, peak_usage, tip_usage, source
                    FROM {self.MONTHLY_TABLE}
                    WHERE user_id = ? AND stat_month LIKE ?
                    ORDER BY stat_month
                    """,
                    (self.user_id, f"{year_key}-%"),
                )
                return [
                    {
                        "month": row[0],
                        "total_usage": to_float(row[1]),
                        "total_charge": to_float(row[2]),
                        "valley_usage": to_float(row[3]),
                        "flat_usage": to_float(row[4]),
                        "peak_usage": to_float(row[5]),
                        "tip_usage": to_float(row[6]),
                        "source": row[7],
                    }
                    for row in cursor.fetchall()
                ]
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取月度汇总失败: %s", exc)
            return []

    def get_daily_summary(self, start_date: str, end_date: str) -> Dict[str, Any]:
        start = normalize_date(start_date)
        end = normalize_date(end_date)
        if not start or not end:
            return {"count": 0}
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT
                        COALESCE(SUM(total_usage), 0),
                        COALESCE(SUM(total_charge), 0),
                        COALESCE(SUM(valley_usage), 0),
                        COALESCE(SUM(flat_usage), 0),
                        COALESCE(SUM(peak_usage), 0),
                        COALESCE(SUM(tip_usage), 0),
                        COUNT(*)
                    FROM {self.DAILY_TABLE}
                    WHERE user_id = ? AND stat_date BETWEEN ? AND ?
                    """,
                    (self.user_id, start, end),
                )
                row = cursor.fetchone()
                return {
                    "total_usage": to_float(row[0], default=0.0) or 0.0,
                    "total_charge": to_float(row[1], default=0.0) or 0.0,
                    "valley_usage": to_float(row[2], default=0.0) or 0.0,
                    "flat_usage": to_float(row[3], default=0.0) or 0.0,
                    "peak_usage": to_float(row[4], default=0.0) or 0.0,
                    "tip_usage": to_float(row[5], default=0.0) or 0.0,
                    "count": int(row[6] or 0),
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取日数据汇总失败: %s", exc)
            return {"count": 0}

    # ------------------------------------------------------------------
    # legacy migration
    # ------------------------------------------------------------------
    def _table_exists(self, table_name: str) -> bool:
        cursor = self._cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()

    def _user_exists(self, user_id: str) -> bool:
        cursor = self._cursor()
        try:
            cursor.execute(
                f"SELECT 1 FROM {self.USERS_TABLE} WHERE user_id = ?",
                (user_id,),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()

    def _migrate_legacy_if_needed(self) -> None:
        uid = self.user_id
        if self._user_exists(uid):
            return

        legacy_daily = f"daily{uid}"
        legacy_data = f"data{uid}"
        legacy_user_name = ""
        migrated = {"daily": 0, "monthly": 0, "yearly": 0, "balance": 0}

        cursor = self._cursor()
        try:
            if self._table_exists(legacy_daily):
                cursor.execute(
                    f"""
                    INSERT OR IGNORE INTO {self.DAILY_TABLE}
                        (user_id, stat_date, total_usage, source)
                    SELECT ?, date, usage, 'legacy'
                    FROM {legacy_daily}
                    WHERE date IS NOT NULL
                    """,
                    (uid,),
                )
                migrated["daily"] = cursor.rowcount

            if self._table_exists(legacy_data):
                cursor.execute(f"SELECT name, value FROM {legacy_data}")
                rows = cursor.fetchall()
                for name, value in rows:
                    name = str(name or "")
                    value = "" if value is None else str(value)
                    if name == "user_info":
                        parts = value.split("|")
                        if len(parts) >= 3:
                            legacy_user_name = parts[2].strip()
                    elif name.startswith("month_"):
                        parts = value.split("|")
                        parts += [""] * (7 - len(parts))
                        cursor.execute(
                            f"""
                            INSERT OR IGNORE INTO {self.MONTHLY_TABLE}
                                (user_id, stat_month, total_usage, total_charge,
                                 valley_usage, flat_usage, peak_usage, tip_usage, source)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'legacy')
                            """,
                            (
                                uid,
                                normalize_month(name[6:]),
                                to_float(parts[0]),
                                to_float(parts[1]),
                                to_float(parts[2]),
                                to_float(parts[3]),
                                to_float(parts[4]),
                                to_float(parts[5]),
                            ),
                        )
                        migrated["monthly"] += max(cursor.rowcount, 0)
                    elif name.startswith("year_"):
                        parts = value.split("|")
                        parts += [""] * (3 - len(parts))
                        cursor.execute(
                            f"""
                            INSERT OR IGNORE INTO {self.YEARLY_TABLE}
                                (user_id, stat_year, total_usage, total_charge, source)
                            VALUES (?, ?, ?, ?, 'legacy')
                            """,
                            (
                                uid,
                                normalize_year(name[5:]),
                                to_float(parts[0]),
                                to_float(parts[1]),
                            ),
                        )
                        migrated["yearly"] += max(cursor.rowcount, 0)
                    elif name == "balance_latest":
                        cursor.execute(
                            f"SELECT 1 FROM {self.BALANCE_TABLE} WHERE user_id = ? LIMIT 1",
                            (uid,),
                        )
                        if cursor.fetchone() is None:
                            parts = value.split("|")
                            parts += [""] * (4 - len(parts))
                            cursor.execute(
                                f"""
                                INSERT INTO {self.BALANCE_TABLE}
                                    (user_id, balance, amount_due, as_of, recorded_at)
                                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                                """,
                                (
                                    uid,
                                    to_float(parts[0]),
                                    to_float(parts[3]),
                                    normalize_datetime(parts[2]),
                                ),
                            )
                            migrated["balance"] += 1
        finally:
            cursor.close()

        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                INSERT INTO {self.USERS_TABLE} (user_id, user_name)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    user_name = COALESCE(user_name, excluded.user_name),
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (uid, self._normalize_text(legacy_user_name)),
            )
            self.connect.commit()
        finally:
            cursor.close()

        if any(migrated.values()):
            logging.info(
                "旧表迁移完成: 日数据 %s 条，月数据 %s 条，年数据 %s 条，余额 %s 条。",
                migrated["daily"], migrated["monthly"], migrated["yearly"], migrated["balance"],
            )

    # ------------------------------------------------------------------
    # cleanup / close
    # ------------------------------------------------------------------
    def cleanup_old_data(self) -> None:
        retention_days = self._retention_days()
        if retention_days <= 0:
            if not self._retention_logged:
                logging.info("DATA_RETENTION_DAYS=%s，数据永久保留。", retention_days)
                self._retention_logged = True
            return

        cutoff_date = (date.today() - timedelta(days=retention_days)).isoformat()
        cursor = self._cursor()
        try:
            cursor.execute(
                f"DELETE FROM {self.DAILY_TABLE} WHERE user_id = ? AND stat_date < ?",
                (self.user_id, cutoff_date),
            )
            daily_deleted = cursor.rowcount
            cursor.execute(
                f"DELETE FROM {self.BALANCE_TABLE} WHERE user_id = ? AND DATE(recorded_at) < ?",
                (self.user_id, cutoff_date),
            )
            balance_deleted = cursor.rowcount
            self.connect.commit()
            logging.info(
                "数据清理完成：删除 %s 条日数据、%s 条余额历史（保留最近 %s 天）。",
                daily_deleted, balance_deleted, retention_days,
            )
        except Exception as exc:
            logging.error("清理旧数据失败: %s", exc)
        finally:
            cursor.close()

    def close_connect(self) -> None:
        if self.connect is not None:
            try:
                self.connect.close()
            except Exception:
                pass
            finally:
                self.connect = None
                self.user_id = None
                logging.info("SQLite 数据库连接已关闭。")

    def ping(self) -> None:
        if self.connect is None:
            raise RuntimeError("数据库连接未建立")
        self.connect.execute("SELECT 1")

    def get_monthly_usage_row(self, month: str) -> Optional[Dict[str, Any]]:
        month_key = normalize_month(month)
        if not month_key:
            return None
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT stat_month, total_usage, total_charge,
                           valley_usage, flat_usage, peak_usage, tip_usage, source
                    FROM {self.MONTHLY_TABLE}
                    WHERE user_id = ? AND stat_month = ?
                    """,
                    (self.user_id, month_key),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "month": row[0],
                    "total_usage": to_float(row[1]),
                    "total_charge": to_float(row[2]),
                    "valley_usage": to_float(row[3]),
                    "flat_usage": to_float(row[4]),
                    "peak_usage": to_float(row[5]),
                    "tip_usage": to_float(row[6]),
                    "source": row[7],
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取月度汇总失败: %s", exc)
            return None

    def get_yearly_row(self, year: str) -> Optional[Dict[str, Any]]:
        year_key = normalize_year(year)
        if not year_key:
            return None
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT stat_year, total_usage, total_charge,
                           valley_usage, flat_usage, peak_usage, tip_usage, source
                    FROM {self.YEARLY_TABLE}
                    WHERE user_id = ? AND stat_year = ?
                    """,
                    (self.user_id, year_key),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "year": row[0],
                    "total_usage": to_float(row[1]),
                    "total_charge": to_float(row[2]),
                    "valley_usage": to_float(row[3]),
                    "flat_usage": to_float(row[4]),
                    "peak_usage": to_float(row[5]),
                    "tip_usage": to_float(row[6]),
                    "source": row[7],
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取年度汇总失败: %s", exc)
            return None

    def get_latest_balance(self) -> Optional[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT balance, amount_due, as_of, recorded_at
                    FROM {self.BALANCE_TABLE}
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (self.user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "balance": to_float(row[0]),
                    "amount_due": to_float(row[1]),
                    "as_of": normalize_datetime(row[2]),
                    "recorded_at": normalize_datetime(row[3]),
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取最近余额失败: %s", exc)
            return None

    def get_latest_daily(self) -> Optional[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT stat_date, total_usage, total_charge,
                           valley_usage, flat_usage, peak_usage, tip_usage, source
                    FROM {self.DAILY_TABLE}
                    WHERE user_id = ?
                    ORDER BY stat_date DESC
                    LIMIT 1
                    """,
                    (self.user_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "date": normalize_date(row[0]),
                    "total_usage": to_float(row[1]),
                    "total_charge": to_float(row[2]),
                    "valley_usage": to_float(row[3]),
                    "flat_usage": to_float(row[4]),
                    "peak_usage": to_float(row[5]),
                    "tip_usage": to_float(row[6]),
                    "source": row[7],
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取最近日数据失败: %s", exc)
            return None

    def get_user(self, user_id: Any) -> Optional[Dict[str, Any]]:
        try:
            uid = self._normalize_user_id(user_id)
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"SELECT user_id, account, user_name, data_updated_at FROM {self.USERS_TABLE} WHERE user_id = ?",
                    (uid,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return {
                    "user_id": row[0],
                    "account": row[1],
                    "user_name": row[2],
                    "data_updated_at": normalize_datetime(row[3]),
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取用户信息失败: %s", exc)
            return None

    def get_users(self) -> list[Dict[str, Any]]:
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"SELECT user_id, account, user_name, data_updated_at FROM {self.USERS_TABLE} ORDER BY user_id"
                )
                return [
                    {
                        "user_id": row[0],
                        "account": row[1],
                        "user_name": row[2],
                        "data_updated_at": normalize_datetime(row[3]),
                    }
                    for row in cursor.fetchall()
                ]
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取用户列表失败: %s", exc)
            return []

    def get_daily_dates(self, start_date: str, end_date: str) -> set[str]:
        start = normalize_date(start_date)
        end = normalize_date(end_date)
        if not start or not end:
            return set()
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    f"""
                    SELECT stat_date FROM {self.DAILY_TABLE}
                    WHERE user_id = ? AND stat_date BETWEEN ? AND ?
                    """,
                    (self.user_id, start, end),
                )
                return {
                    normalize_date(row[0])
                    for row in cursor.fetchall()
                    if row[0] is not None
                }
            finally:
                cursor.close()
        except Exception as exc:
            logging.error("读取日数据日期失败: %s", exc)
            return set()
