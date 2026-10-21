"""不访问国家电网，直接推送一组模拟数据到 HA。

用途：验证 MQTT broker 认证、HA MQTT Discovery、实体 ID 和状态属性是否正常。
默认使用模拟户号 9999999999999，不会覆盖真实户号的实体。

用法：
    python3 scripts/mock_publish.py
    python3 scripts/mock_publish.py --cleanup
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta

if 'PYTHON_IN_DOCKER' not in os.environ:
    try:
        import dotenv
        dotenv.load_dotenv(verbose=True)
    except ImportError:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ha_mqtt import SENSOR_SPECS  # noqa: E402
from sensor_updator import SensorUpdator  # noqa: E402


DEFAULT_USER_ID = "9999999999999"


def build_mock_data():
    yesterday = date.today() - timedelta(days=1)
    month = datetime.now().strftime("%Y-%m")
    daily_usage = 16.06
    return {
        "balance": 729.68,
        "last_daily_date": yesterday.isoformat(),
        "last_daily_usage": daily_usage,
        "yearly_charge": 333.6,
        "yearly_usage": 556.0,
        "month_charge": 78.6,
        "month_usage": 131.0,
        "month_period": month,
        "tou_data": {
            "daily": [{
                "date": yesterday.isoformat(),
                "total_usage": daily_usage,
                "valley_usage": 5.1,
                "flat_usage": 4.2,
                "peak_usage": 5.7,
                "tip_usage": 1.06,
            }],
            "months": [{
                "month": month,
                "total_usage": 131.0,
                "total_charge": 78.6,
            }],
        },
        "bill_tou_data": {
            "month": month,
            "usage": 131.0,
            "charge": 78.6,
            "valley_usage": 50.0,
            "flat_usage": 40.0,
            "peak_usage": 35.0,
            "tip_usage": 6.0,
        },
        "enhanced_balance": {
            "amount_due": 0.0,
            "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "user_name": "模拟数据",
    }


def main():
    parser = argparse.ArgumentParser(description="推送模拟数据到 Home Assistant MQTT")
    parser.add_argument("--user-id", default=os.getenv("MOCK_USER_ID", DEFAULT_USER_ID),
                        help="模拟户号，默认 9999999999999")
    parser.add_argument("--cleanup", action="store_true",
                        help="发布空发现配置，删除模拟户号的 HA 实体")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s  [%(levelname)-8s] ---- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 避免和正在运行的真实实例使用同一个 client id / 在线状态 topic
    os.environ["MQTT_CLIENT_ID"] = os.getenv("MOCK_MQTT_CLIENT_ID", "sgcc_electricity_mock")
    os.environ["MQTT_AVAILABILITY_TOPIC"] = os.getenv(
        "MOCK_MQTT_AVAILABILITY_TOPIC", "sgcc_electricity/status_mock"
    )

    updator = SensorUpdator()
    try:
        if args.cleanup:
            logging.info("正在删除模拟户号 %s 的 MQTT 实体...", args.user_id)
            updator.ha.remove_user_sensors(args.user_id)
            logging.info("清理完成。")
            return

        if not updator.mqtt.enabled():
            logging.error("MQTT_HOST 未配置，无法发布。请先检查 .env 中的 MQTT 配置。")
            return 1

        data = build_mock_data()
        logging.info("推送模拟数据到 MQTT broker（不会访问国家电网）...")
        updator.update_one_userid(
            user_id=args.user_id,
            balance=data["balance"],
            last_daily_date=data["last_daily_date"],
            last_daily_usage=data["last_daily_usage"],
            yearly_charge=data["yearly_charge"],
            yearly_usage=data["yearly_usage"],
            month_charge=data["month_charge"],
            month_usage=data["month_usage"],
            tou_data=data["tou_data"],
            bill_tou_data=data["bill_tou_data"],
            enhanced_balance=data["enhanced_balance"],
            user_name=data["user_name"],
            month_period=data["month_period"],
        )

        postfix = args.user_id[-4:]
        logging.info("模拟数据发布完成。请在 HA 中检查以下实体：")
        for sensor_key in SENSOR_SPECS:
            logging.info("  sensor.%s_%s", sensor_key, postfix)
        logging.info("验证后可运行：python3 scripts/mock_publish.py --cleanup")
        return 0
    finally:
        # 模拟脚本退出后仍保持在线，方便在 HA 中查看实体；清理请用 --cleanup。
        updator.close(publish_offline=False)


if __name__ == "__main__":
    raise SystemExit(main())
