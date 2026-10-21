"""本地一次性抓取：登录后完整跑一遍数据抓取、全年回补、交费记录和账单同步。"""

import argparse
import logging
import os
import sys

from data_fetcher import DataFetcher
from error_watcher import ErrorWatcher
from main import logger_init
from sensor_updator import SensorUpdator

from const import get_data_dir


def parse_args():
    parser = argparse.ArgumentParser(description="Run one full SGCC fetch and exit.")
    parser.add_argument(
        "--force-backfill",
        action="store_true",
        help="强制重新回补当年日用电量（默认只在数据缺口较大时回补）",
    )
    parser.add_argument(
        "--no-bill-sync",
        action="store_true",
        help="跳过官方月度账单同步",
    )
    parser.add_argument(
        "--no-payment",
        action="store_true",
        help="跳过交费记录同步",
    )
    return parser.parse_args()


def main():
    import dotenv

    dotenv.load_dotenv(verbose=True)
    args = parse_args()
    if args.force_backfill:
        os.environ["FORCE_YEAR_BACKFILL"] = "true"
    if args.no_bill_sync:
        os.environ["BILL_SYNC_ENABLED"] = "false"
    if args.no_payment:
        os.environ["PAYMENT_FETCH_ENABLED"] = "false"

    logger_init(os.getenv("LOG_LEVEL", "INFO"))
    data_dir = get_data_dir()
    ErrorWatcher.init(
        root_dir=os.path.join(data_dir, "errors"),
        screenshot_dir=os.path.join(data_dir, "errors"),
    )

    fetcher = DataFetcher(os.getenv("PHONE_NUMBER"), os.getenv("PASSWORD"))
    fetcher.fetch()
    updator = SensorUpdator()
    try:
        updator.publish_all_from_db(fetcher.db)
    finally:
        updator.close(publish_offline=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("用户中断")
        sys.exit(130)
    except Exception as exc:
        logging.error("一次性抓取失败: %s", exc)
        raise
