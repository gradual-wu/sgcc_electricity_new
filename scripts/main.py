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

def mask_phone(value):
    if not value:
        return "(未配置)"
    text = str(value)
    if len(text) >= 7:
        return f"{text[:3]}****{text[-4:]}"
    return "***"


def main():
    global RETRY_TIMES_LIMIT
    # 直接用 print 打一条 stdout 标记，即使 logging 配置异常也能在 Docker 日志里看到
    print("SGCC Electricity add-on starting (stdout marker)", flush=True)
    # 先建立 stdout 日志，确保 options/env 读取阶段的日志也能出现在 HA 加载项日志里
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)-8s] ---- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.info("SGCC Electricity 插件进程启动")
    if 'PYTHON_IN_DOCKER' not in os.environ:
        # 读取 .env 文件
        import dotenv
        dotenv.load_dotenv(verbose=True)
    if os.path.isfile('/data/options.json'):
        try:
            with open('/data/options.json') as f:
                options = json.load(f)
            for key, value in options.items():
                os.environ[key] = str(value)
            import const
            const.LLM_API_KEY = os.getenv('LLM_API_KEY', '').strip()
            const.LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
            const.LLM_MODEL = os.getenv('LLM_MODEL', 'doubao-seed-2-0-pro-260215')
            logging.info("当前以 Home Assistant Add-on 形式运行")
        except Exception as e:
            logging.exception("读取 options.json 失败，程序将退出: %s", e)
            sys.exit(1)

    try:
        LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
        logger_init(LOG_LEVEL)
        logging.info(
            "当前以 Docker 镜像方式运行。"
            if "PYTHON_IN_DOCKER" in os.environ
            else "当前以本地 Python 方式运行。"
        )
        PHONE_NUMBER = os.getenv("PHONE_NUMBER")
        logging.info(f"读取环境变量 PHONE_NUMBER: {mask_phone(PHONE_NUMBER)}")
        PASSWORD = os.getenv("PASSWORD")
        HASS_URL = os.getenv("HASS_URL")
        JOB_START_TIME = os.getenv("JOB_START_TIME", "07:00")
        VERSION = os.getenv("VERSION")
        RETRY_TIMES_LIMIT = int(os.getenv("RETRY_TIMES_LIMIT", 5))
    except Exception as e:
        logging.exception("读取配置失败，程序将退出: %s", e)
        sys.exit(1)

    logging.info(f"当前仓库版本为 {VERSION}，仓库地址为 https://github.com/gradual-wu/sgcc_electricity_new.git")
    current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"当前日期为 {current_datetime}。")
    logging.info(
        "运行配置: DB_TYPE=%s, MQTT=%s:%s, JOB_START_TIME=%s, "
        "DAILY_FETCH_DAYS=%s, DAILY_RECONCILE_DAYS=%s, ENTITY_ID_STYLE=%s, "
        "FORCE_FULL_REFRESH=%s, FILL_MISSING_DATA=%s",
        os.getenv("DB_TYPE", "NONE"),
        os.getenv("MQTT_HOST") or "(从 HASS_URL 推断)",
        os.getenv("MQTT_PORT", "1883"),
        JOB_START_TIME,
        os.getenv("DAILY_FETCH_DAYS", "7"),
        os.getenv("DAILY_RECONCILE_DAYS", "7"),
        os.getenv("ENTITY_ID_STYLE", "none"),
        os.getenv("FORCE_FULL_REFRESH", "false"),
        os.getenv("FILL_MISSING_DATA", "false"),
    )

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

    # 启动时先从数据库恢复；数据库没有数据、要求全量刷新或补缺时才访问国家电网
    force_full_refresh = os.getenv("FORCE_FULL_REFRESH", "false").lower() == "true"
    fill_missing_data = os.getenv("FILL_MISSING_DATA", "false").lower() == "true"
    if force_full_refresh:
        logging.info("FORCE_FULL_REFRESH=true，启动时立即执行全年数据全量刷新...")
        run_task(fetcher, updator)
    elif fill_missing_data:
        logging.info("FILL_MISSING_DATA=true，启动时立即扫描数据库并补齐缺失数据...")
        run_task(fetcher, updator)
    elif not updator.publish_all_from_db(fetcher.db):
        logging.info("数据库中没有可发布数据，正在从国家电网获取数据...")
        run_task(fetcher, updator)
    else:
        logging.info("已从数据库恢复数据，跳过启动时抓取以保护账号。")

    try:
        heartbeat_seconds = max(int(os.getenv("HEARTBEAT_LOG_MINUTES", "30")), 1) * 60
    except ValueError:
        heartbeat_seconds = 30 * 60
    last_heartbeat = time.monotonic()
    logging.info(
        f"启动完成，进入调度循环；每 {heartbeat_seconds // 60} 分钟输出一次心跳日志。"
    )

    while True:
        try:
            schedule.run_pending()
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_seconds:
                last_heartbeat = now
                logging.info(
                    "心跳: 插件运行中，当前时间 %s，下次抓取 %s / %s。",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    parsed_time.strftime("%H:%M"),
                    next_run_time.strftime("%H:%M"),
                )
            time.sleep(1)
        except KeyboardInterrupt:
            logging.info("收到退出信号，正在关闭 MQTT 连接...")
            updator.close()
            break
        except Exception as exc:
            logging.exception("调度循环异常，10 秒后继续: %s", exc)
            time.sleep(10)


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
    level = str(level or "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = "INFO"
    logger = logging.getLogger()
    logger.setLevel(level)
    logger.handlers.clear()
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    format = logging.Formatter("%(asctime)s  [%(levelname)-8s] ---- %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(format)
    logger.addHandler(sh)

    # 额外写一份文件日志，避免 Supervisor 日志页异常时完全看不到日志。
    default_log_file = (
        "/config/sgcc_electricity.log"
        if "PYTHON_IN_DOCKER" in os.environ
        else os.path.join(get_data_dir(), "sgcc_electricity.log")
    )
    log_file = os.getenv("LOG_FILE", "").strip() or default_log_file
    try:
        from logging.handlers import RotatingFileHandler

        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = RotatingFileHandler(
            log_file,
            maxBytes=max(int(os.getenv("LOG_FILE_MAX_BYTES", "1048576")), 10240),
            backupCount=max(int(os.getenv("LOG_FILE_BACKUP_COUNT", "3")), 1),
            encoding="utf-8",
        )
        fh.setFormatter(format)
        logger.addHandler(fh)
        logger.info("文件日志已启用: %s", log_file)
    except Exception as exc:
        logger.warning("无法启用文件日志 %s: %s", log_file, exc)
    logger.propagate = False


if __name__ == "__main__":
    main()
