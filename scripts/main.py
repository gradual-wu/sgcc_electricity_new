import logging
import logging.config
import os
import sys
import time
import schedule
import json
import random
from error_watcher import ErrorWatcher
from sensor_updator import SensorUpdator
from datetime import datetime,timedelta
from const import *
from data_fetcher import DataFetcher

def main():
    global RETRY_TIMES_LIMIT
    if 'PYTHON_IN_DOCKER' not in os.environ:
        # 读取 .env 文件
        import dotenv
        dotenv.load_dotenv(verbose=True)
    if os.path.isfile('/data/options.json'):
        with open('/data/options.json') as f:
            options = json.load(f)
        try:
            for key, value in options.items():
                os.environ[key] = str(value)
            import const
            const.LLM_API_KEY = os.getenv('LLM_API_KEY', '').strip()
            const.LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
            const.LLM_MODEL = os.getenv('LLM_MODEL', 'doubao-seed-2-0-pro-260215')
            logging.info(f"当前以Homeassistant Add-on 形式运行.")
        except Exception as e:
            logging.error(f"读取 options.json 文件失败，程序将退出，错误信息: {e}。")
            sys.exit()

    try:
        PHONE_NUMBER = os.getenv("PHONE_NUMBER")
        logging.info(f"读取环境变量 PHONE_NUMBER : {PHONE_NUMBER}")
        PASSWORD = os.getenv("PASSWORD")
        HASS_URL = os.getenv("HASS_URL")
        JOB_START_TIME = os.getenv("JOB_START_TIME","07:00" )
        LOG_LEVEL = os.getenv("LOG_LEVEL","INFO")
        VERSION = os.getenv("VERSION")
        RETRY_TIMES_LIMIT = int(os.getenv("RETRY_TIMES_LIMIT", 5))

        logger_init(LOG_LEVEL)
        logging.info(f"当前以Docker镜像方式运行。")
    except Exception as e:
        logging.error(f"读取 .env 文件失败，程序将退出，错误信息: {e}。")
        sys.exit()

    logging.info(f"当前仓库版本为 {VERSION}，仓库地址为 https://github.com/gradual-wu/sgcc_electricity_new.git")
    current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"当前日期为 {current_datetime}。")

    logging.info(f"开始初始化 ErrorWatcher")
    ErrorWatcher.init(root_dir='/data/errors')
    logging.info(f'ErrorWatcher 初始化完成！')
    fetcher = DataFetcher(PHONE_NUMBER, PASSWORD)
    updator = SensorUpdator()

    # 生成随机延迟时间（-10分钟到+10分钟）
    random_delay_minutes = random.randint(-10, 10)
    parsed_time = datetime.strptime(JOB_START_TIME, "%H:%M") + timedelta(minutes=random_delay_minutes)
    logging.info(f"当前登录用户名为 {PHONE_NUMBER}，Home Assistant 地址为 {HASS_URL}，程序将每天在 {parsed_time.strftime('%H:%M')} 执行。")
    logging.info(
        f"MQTT 配置: {os.getenv('MQTT_HOST') or '(从 HASS_URL 推断)'}:"
        f"{os.getenv('MQTT_PORT', '1883')}，"
        f"状态前缀 {os.getenv('MQTT_STATE_PREFIX', 'sgcc_electricity')}。"
    )

    # 添加随机延迟
    next_run_time = parsed_time + timedelta(hours=12)

    logging.info(f'立即执行任务！下次运行时间为每天 {parsed_time.strftime("%H:%M")} 和 {next_run_time.strftime("%H:%M")}')
    schedule.every().day.at(parsed_time.strftime("%H:%M")).do(run_task, fetcher, updator)
    schedule.every().day.at(next_run_time.strftime("%H:%M")).do(run_task, fetcher, updator)

    # 每5分钟从数据库重新发布一次数据，防止 HA 重启后数据丢失
    schedule.every(5).minutes.do(republish_or_fetch, updator, fetcher)

    # 启动时先从数据库恢复；数据库没有数据时才访问国家电网
    if not updator.publish_all_from_db(fetcher.db):
        logging.info("数据库中没有可发布数据，正在从国家电网获取数据...")
        run_task(fetcher, updator)
    else:
        logging.info("已从数据库恢复数据，跳过启动时抓取以保护账号。")

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            logging.info("收到退出信号，正在关闭 MQTT 连接...")
            updator.close()
            break


def republish_or_fetch(updator: SensorUpdator, fetcher: DataFetcher):
    if not updator.publish_all_from_db(fetcher.db):
        logging.info("数据库中没有可发布数据，正在从国家电网获取数据...")
        run_task(fetcher, updator)


def run_task(data_fetcher: DataFetcher, updator: SensorUpdator):
    for retry_times in range(1, RETRY_TIMES_LIMIT + 1):
        try:
            data_fetcher.fetch()
            # 抓取只负责写库，HA 数据统一从数据库读取
            updator.publish_all_from_db(data_fetcher.db)
            return
        except Exception as e:
            logging.error(f"状态刷新任务失败，原因是 [{e}]，还剩 {RETRY_TIMES_LIMIT - retry_times} 次重试机会。")
            continue

def logger_init(level: str):
    logger = logging.getLogger()
    logger.setLevel(level)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    format = logging.Formatter("%(asctime)s  [%(levelname)-8s] ---- %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(format)
    logger.addHandler(sh)


if __name__ == "__main__":
    main()
