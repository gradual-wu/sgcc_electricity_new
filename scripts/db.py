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

    # ------------------------------------------------------------------
    # interface
    # ------------------------------------------------------------------
    def connect_user_db(self, user_id: Any) -> bool:
        raise NotImplementedError

    def upsert_user(self, user_id: Any, account: str = "", user_name: str = "") -> bool:
        raise NotImplementedError

    def insert_daily_data(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def insert_monthly_data(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def insert_yearly_data(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def insert_balance_log(self, data: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def cleanup_old_data(self) -> None:
        raise NotImplementedError

    def close_connect(self) -> None:
        raise NotImplementedError


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
        )
        cursor = self._cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
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
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            f"CREATE INDEX IF NOT EXISTS idx_sg_daily_date ON {self.DAILY_TABLE}(stat_date)",
            f"CREATE INDEX IF NOT EXISTS idx_sg_monthly_month ON {self.MONTHLY_TABLE}(stat_month)",
            f"CREATE INDEX IF NOT EXISTS idx_sg_balance_user_time ON {self.BALANCE_TABLE}(user_id, recorded_at)",
        )
        cursor = self._cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
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
