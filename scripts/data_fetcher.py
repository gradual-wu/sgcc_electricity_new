import logging
import os
import re
import time
import json

import random
import base64
from datetime import datetime, date, timedelta
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from error_watcher import ErrorWatcher
from typing import Optional

from const import *

from date_utils import normalize_date, normalize_month, normalize_year, to_float

import numpy as np
from captcha_selenium import solve_captcha_in_browser
import vue_state


# 电费账单页选择器（与 ha-95598 保持一致）
BILL_CARD = "//div[contains(@class,'billContent_bill')]"
BILL_YEAR_OPTIONS = "//div[contains(@class,'billList_timeSelection')]//div[contains(@class,'content_year')]/span"
BILL_ACTIVE_YEAR = "//div[contains(@class,'billList_timeSelection')]//div[contains(@class,'content_sleectYear')]/span"
BILL_EXPAND_BUTTON = "//div[contains(@class,'content_button')]//*[contains(text(),'查看更多')]"
BILL_MONTH_LABEL = "//div[contains(@class,'bill_time')]/span[1]"
BILL_LIST_ROW = "//div[contains(@class,'billList_content')]"
BILL_ROW_DETAIL_ARROW = ".//img[contains(@class,'back_right')]"
BILL_DETAIL_CYCLE = "//div[contains(@class,'billInfo_cycle')]"


def bill_year_option(target_year: int) -> str:
    return (
        "//div[contains(@class,'billList_timeSelection')]//div[contains(@class,'content_year')]/"
        f"span[normalize-space()='{int(target_year)}年']"
    )

class DataFetcher:

    def __init__(self, username: str, password: str):
        if 'PYTHON_IN_DOCKER' not in os.environ:
            import dotenv
            dotenv.load_dotenv(verbose=True)
        self._username = username
        self._password = password

        self.DRIVER_IMPLICITY_WAIT_TIME = int(os.getenv("DRIVER_IMPLICITY_WAIT_TIME", 60))
        self.RETRY_TIMES_LIMIT = int(os.getenv("RETRY_TIMES_LIMIT", 5))
        self.LOGIN_EXPECTED_TIME = int(os.getenv("LOGIN_EXPECTED_TIME", 10))
        self.RETRY_WAIT_TIME_OFFSET_UNIT = int(os.getenv("RETRY_WAIT_TIME_OFFSET_UNIT", 10))
        # 数据页操作等待；页面加载通常几秒内完成，不需要像登录那样长时间等待
        self.PAGE_WAIT_TIME = int(os.getenv("PAGE_WAIT_TIME", "10"))
        self.IGNORE_USER_ID = os.getenv("IGNORE_USER_ID", "xxxxx,xxxxx").split(",")
        self.QR_CODE_LOGIN_WAIT_COUNT = int(os.getenv("QR_CODE_LOGIN_WAIT_COUNT", 18))
        self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT = int(os.getenv("QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT", 10))
        self._user_name_map = {}
        raw_names = os.getenv("USER_NAMES", "")
        if raw_names:
            for pair in raw_names.split(","):
                if ":" in pair:
                    uid, name = pair.split(":", 1)
                    self._user_name_map[uid.strip()] = name.strip()
        self._backfilled_years = set()
        self._init_db()

    def _init_db(self):
        self.db_type = os.getenv("DB_TYPE", "None").lower()
        if self.db_type == 'mysql':
            from db import MysqlDB
            self.db = MysqlDB()
            logging.info("使用 MySQL 数据库存储数据。")
        elif self.db_type == 'sqlite':
            from db import SqliteDB
            self.db = SqliteDB()
            logging.info("使用 SQLite 数据库存储数据。")
        else:
            self.db = None
            logging.info("不使用数据库存储数据。")

    # @staticmethod
    def _click_button(self, driver, button_search_type, button_search_key):
        '''封装点击函数，仅在元素可点击时点击'''
        click_element = driver.find_element(button_search_type, button_search_key)
        WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(EC.element_to_be_clickable(click_element))
        driver.execute_script("arguments[0].click();", click_element)
        # 点击后添加微小随机暂停，模拟人工操作
        time.sleep(random.uniform(0.1, 0.5))


    def _get_webdriver(self):
        chrome_options = webdriver.ChromeOptions()

        # 基础参数
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-gpu-sandbox")   # 新版Chromium(120+)需要此参数
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--start-maximized")

        # 反检测核心参数（参考 ha-95598）
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)

        # 可选：环境变量自定义反检测参数
        browser_lang = os.getenv("BROWSER_LANGUAGE", "zh-HK,zh,en-US,en")
        browser_ua = os.getenv("BROWSER_USER_AGENT", "")
        device_scale = os.getenv("BROWSER_DEVICE_SCALE_FACTOR", "2")
        window_size = os.getenv("BROWSER_WINDOW_SIZE", "1158,848")

        chrome_options.add_argument(f"--lang={browser_lang}")
        chrome_options.add_argument(f"--window-size={window_size}")
        chrome_options.add_argument(f"--force-device-scale-factor={device_scale}")
        chrome_options.add_argument("--high-dpi-support=1")
        if browser_ua:
            chrome_options.add_argument(f"user-agent={browser_ua}")

        chrome_options.add_experimental_option("prefs", {
            "intl.accept_languages": browser_lang,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        })

        # 无头模式（Docker 环境）
        if 'PYTHON_IN_DOCKER' in os.environ:
            chrome_options.add_argument("--headless=new")
            chrome_options.binary_location = "/usr/bin/chromium"
            service = ChromeService(executable_path="/usr/bin/chromedriver")
            # 诊断：打印 Chromium 版本，帮助排查崩溃原因
            try:
                import subprocess
                ver = subprocess.run(["/usr/bin/chromium", "--version"],
                                     capture_output=True, text=True, timeout=5)
                logging.info(f"Chromium 版本: {ver.stdout.strip()}")
                drv = subprocess.run(["/usr/bin/chromedriver", "--version"],
                                     capture_output=True, text=True, timeout=5)
                logging.info(f"ChromeDriver 版本: {drv.stdout.strip()}")
            except Exception as e:
                logging.warning(f"版本检测失败: {e}")
            def _setting_driver(driver):
                # 显式设置窗口大小（解决无头模式下 --window-size 不生效的问题）
                width, height = map(int, window_size.split(','))
                driver.set_window_size(width, height)
                try:
                    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                        "width": width,
                        "height": height,
                        "deviceScaleFactor": int(device_scale),
                        "mobile": False,
                        "dontSetVisibleSize": False
                    })
                except Exception as e:
                    logging.warning(f"CDP 设置 viewport 失败: {e}")
                
        else:
            service = self._find_chromedriver()
            def _setting_driver(driver):
                driver.maximize_window()

        driver = webdriver.Chrome(options=chrome_options, service=service)
        driver.implicitly_wait(self.DRIVER_IMPLICITY_WAIT_TIME)

        _setting_driver(driver)

        # ── 反指纹检测：伪装为真实 Windows Chrome，绕过腾讯风控 ──
        # 修复 Issue #267：容器环境被识别为高风险导致 RK001
        real_ua = (browser_ua or
                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/136.0.0.0 Safari/537.36")
        try:
            driver.execute_cdp_cmd("Network.setUserAgentOverride", {
                "userAgent": real_ua,
                "acceptLanguage": browser_lang,
                "platform": "Win32",
            })
        except Exception as e:
            logging.warning(f"CDP 设置 UA 失败: {e}")

        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    // 隐藏 webdriver 标识
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    // 伪装为 Windows 平台
                    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                    // 伪装语言
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['zh-CN', 'zh', 'en-US', 'en']
                    });
                    // 模拟正常浏览器插件数量
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                    // 补充 chrome 对象（无头模式下可能缺失）
                    window.chrome = window.chrome || {};
                    window.chrome.runtime = window.chrome.runtime || {};
                    // 伪装 userAgentData 为 Windows Chrome（腾讯风控检测此字段）
                    try {
                        Object.defineProperty(navigator, 'userAgentData', {
                            get: () => ({
                                brands: [
                                    {brand: 'Chromium', version: '136'},
                                    {brand: 'Google Chrome', version: '136'},
                                    {brand: 'Not-A.Brand', version: '99'}
                                ],
                                mobile: false,
                                platform: 'Windows',
                                getHighEntropyValues: (hints) => Promise.resolve({
                                    platform: 'Windows',
                                    platformVersion: '10.0.0',
                                    architecture: 'x86',
                                    bitness: '64',
                                    model: '',
                                    uaFullVersion: '136.0.7103.116',
                                    fullVersionList: [
                                        {brand: 'Chromium', version: '136.0.7103.116'},
                                        {brand: 'Google Chrome', version: '136.0.7103.116'},
                                        {brand: 'Not-A.Brand', version: '99.0.0.0'}
                                    ]
                                })
                            })
                        });
                    } catch(e) {}
                    // 隐藏无头模式特征
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications'
                            ? Promise.resolve({state: Notification.permission})
                            : originalQuery(parameters)
                    );
                """
            })
        except Exception as e:
            logging.warning(f"CDP 注入反检测脚本失败: {e}")

        return driver

    @staticmethod
    def _find_chromedriver() -> ChromeService:
        """在非 Docker 环境中查找可用的 ChromeDriver。"""
        import shutil

        # 1) 尝试系统 PATH
        path = shutil.which("chromedriver") or shutil.which("chromedriver.exe")
        if path:
            return ChromeService(executable_path=path)

        # 2) 尝试 CloakBrowser 缓存的 chromedriver（如果有）
        for base in [
            os.path.expanduser("~/.cloakbrowser"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), ".cloakbrowser"),
        ]:
            try:
                for root, dirs, files in os.walk(base):
                    if "chromedriver.exe" in files or "chromedriver" in files:
                        fname = "chromedriver.exe" if "chromedriver.exe" in files else "chromedriver"
                        path = os.path.join(root, fname)
                        if os.path.isfile(path):
                            return ChromeService(executable_path=path)
                    # 最多扫描两级目录
                    if len(root) - len(base) > 200:
                        dirs.clear()
            except Exception:
                pass

        # 3) 尝试 Selenium Manager 自动下载
        try:
            return ChromeService()
        except Exception:
            pass

        raise RuntimeError(
            "ChromeDriver 未找到。请安装 ChromeDriver 或运行: pip install chromedriver-binary-auto"
        )

    @ErrorWatcher.watch
    def _login(self, driver, phone_code = False):
        try:
            driver.get(LOGIN_URL)
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME * 3).until(EC.visibility_of_element_located((By.CLASS_NAME, "user")))
        except Exception:
            logging.error(f"登录页面加载失败: {LOGIN_URL}")
            return False
        logging.info(f"打开登录页面: {LOGIN_URL}。\r")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT*2)
        # swtich to username-password login page
        # 临时关闭隐式等待，避免与 WebDriverWait 叠加导致超时
        driver.implicitly_wait(0)
        try:
            WebDriverWait(driver, 10).until(
                EC.invisibility_of_element_located((By.CLASS_NAME, 'el-loading-mask')))
        finally:
            driver.implicitly_wait(self.DRIVER_IMPLICITY_WAIT_TIME)  # 恢复隐式等待

        element = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'user')))
        driver.execute_script("arguments[0].click();", element)
        logging.info("已找到 'user' 元素。\r")
        self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[1]/div[1]/div[2]/span')
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # 点击同意按钮
        self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[1]/form/div[1]/div[3]/div/span[2]')
        logging.info("已点击同意选项。\r")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        if phone_code:
            self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[1]/div[1]/div[3]/span')
            input_elements = driver.find_elements(By.CLASS_NAME, "el-input__inner")
            input_elements[2].send_keys(self._username)
            logging.info(f"已输入用户名: {self._username}\r")
            self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[2]/form/div[1]/div[2]/div[2]/div/a')
            code = input("请输入手机验证码: ")
            input_elements[3].send_keys(code)
            logging.info(f"已输入验证码: {code}。\r")
            # 点击登录按钮
            self._click_button(driver, By.XPATH, '//*[@id="login_box"]/div[2]/div[2]/form/div[2]/div/button/span')
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT*2)
            logging.info("已点击登录按钮。\r")

            return True
        # 增加判空校验便于测试备用方案
        elif self._password is not None and len(self._password) > 0:
            # 输入用户名和密码
            input_elements = driver.find_elements(By.CLASS_NAME, "el-input__inner")
            input_elements[0].send_keys(self._username)
            logging.info(f"已输入用户名: {self._username}\r")
            input_elements[1].send_keys(self._password)
            logging.info("已输入密码。\r")

            # 点击登录按钮
            self._click_button(driver, By.CLASS_NAME, "el-button.el-button--primary")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
            logging.info("已点击登录按钮。\r")

            # 快速检查：如果已经跳转离开登录页，说明无需验证码，直接成功
            if driver.current_url != LOGIN_URL:
                logging.info("无需验证码登录成功 (已被重定向)。\r")
                return True

            # 会出现点击登录直接失败（账号被限制登录）
            error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
            if error is None:
                # 处理腾讯点击验证码
                captcha_passed = solve_captcha_in_browser(driver, max_retries=self.RETRY_TIMES_LIMIT)
                if captcha_passed:
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    if driver.current_url != LOGIN_URL:
                        logging.info("通过点击验证码登录成功。\r")
                        return True
                    else:
                        error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
                        if error:
                            logging.info(f"验证码通过但登录失败: [{error}]\r")
                        else:
                            logging.error("验证码已通过但仍停留在登录页面。")
                else:
                    error = self._get_error_message(driver, "//div[@class='errmsg-tip']//span")
                    logging.error("点击验证码识别在所有重试后均失败。")
            else:
                logging.error(f"登录失败: [{error}]\r")
                # RK001 = 腾讯风控拦截，继续用密码重试无意义，直接走二维码
                if error and ("RK001" in error or "网络连接超时" in error):
                    logging.warning("检测到风控拦截（RK001），跳过密码重试，直接切换二维码登录...")
                    return self._qr_login(driver, error)
        return self._fallback_login(driver, error)

    def _get_error_message(self, driver, path) -> Optional[str]:
        """获取错误信息，如果不存在则返回 None"""
        # 关闭隐式等待
        driver.implicitly_wait(0)
        try:
            element = driver.find_element(By.XPATH, path)
            return element.text
        except Exception:
            return None
        finally:
            driver.implicitly_wait(self.DRIVER_IMPLICITY_WAIT_TIME)  # 恢复隐式等待

    def _fallback_login(self, driver, reason: str) -> bool:
        """使用备用方案登录"""
        fallback = os.getenv("LOGIN_FALLBACK")
        if fallback == 'qrcode':
            return self._qr_login(driver, reason)
        return False

    def _qr_login(self, driver, reason: str) -> bool:
        logging.info("二维码登录开始")
        # 切换验证码
        element = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.presence_of_element_located((By.CLASS_NAME, 'qr_code')))
        driver.execute_script("arguments[0].click();", element)
        logging.info("已切换到二维码模式")

        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        # 获取登录二维码
        qrElement = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.visibility_of_element_located((By.XPATH, "//div[@class='sweepCodePic']//img")))
        logging.info("已找到二维码图片元素")

        img_src = qrElement.get_attribute('src')

        if img_src.startswith('data:image'):
            base64_data = img_src.split(',')[1]
            img_screenshot = base64.b64decode(base64_data)
        else:
          logging.info('二维码图片源不是 base64 格式')
          img_screenshot = qrElement.screenshot_as_png

        with open("/data/login_qr_code.png", "wb") as f:
            f.write(img_screenshot)
            logging.info("已将二维码保存到 /data/login_qr_code.png")

        from notify import UrlLoginQrCodeNotify
        notifyFunc = UrlLoginQrCodeNotify()
        notifyFunc(img_screenshot, reason)
        for i in range(1, self.QR_CODE_LOGIN_WAIT_COUNT + 1):
            logging.info(f'二维码登录等待检查[{self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT}] 次数[{i}]')
            time.sleep(self.QR_CODE_LOGIN_WAIT_TIME_INTERVAL_UNIT)
            if (driver.current_url != LOGIN_URL):
                logging.info("二维码登录成功")
                return True
            else:
                error = self._get_error_message(driver, "//div[@class='sweepCodePic']//div[@class='erwBg']//p")
                if error is not None:
                    logging.error(f'二维码登录错误[{error}]')
                    return False

        logging.warning("二维码登录超时")

        return False

    def _random_delay(self, min_seconds=0.5, max_seconds=3.0):
        """添加随机延迟，使自动化操作更难被检测。"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)


    def fetch(self):

        """主逻辑"""

        driver = self._get_webdriver()
        ErrorWatcher.instance().set_driver(driver)

        self._random_delay(1, 3)
        logging.info("浏览器驱动已初始化。")

        try:
            if os.getenv("DEBUG_MODE", "false").lower() == "true":
                if self._login(driver,phone_code=True):
                    logging.info("登录成功!")
                else:
                    logging.info("登录失败!")
                    raise Exception("login unsuccessed")
            else:
                if self._login(driver):
                    logging.info("登录成功!")
                else:
                    logging.info("登录失败!")
                    raise Exception("login unsuccessed")
        except Exception as e:
            logging.error(
                f"浏览器驱动异常退出，原因: {e}。还剩 {self.RETRY_TIMES_LIMIT} 次重试机会。")
            driver.quit()
            return

        try:
            return self.fetch_with_driver(driver)
        finally:
            driver.quit()

    def fetch_with_driver(self, driver):
        """在已登录的浏览器里执行抓取并写库，不负责创建/退出浏览器，也不直接推送 HA。"""
        processed_user_ids = []
        logging.info(f"在 {LOGIN_URL} 登录成功")
        self._random_delay(1, 3)
        # self._random_mouse_move(driver)
        logging.info(f"尝试获取用户 ID 列表")
        user_id_list = self._get_user_ids(driver)
        logging.info(f"共找到 {len(user_id_list)} 个用户 ID，其中 {user_id_list} 将被忽略: {self.IGNORE_USER_ID}")
        self._random_delay(0.5, 2)


        for userid_index, user_id in enumerate(user_id_list):
            if user_id in self.IGNORE_USER_ID:
                logging.info(f"用户 ID {user_id} 将被忽略")
                continue
            try:
                self._random_delay(1, 3)
                # 切换到电费余额页面
                driver.get(BALANCE_URL)
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                self._choose_current_userid(driver, userid_index, target_user_id=user_id)
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                current_userid = self._get_page_user_id(driver)
                if current_userid and current_userid != user_id:
                    logging.warning(
                        f"户号不匹配：期望 {user_id}，页面当前为 {current_userid}，重试切换"
                    )
                    self._choose_current_userid(driver, userid_index, target_user_id=user_id)
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    current_userid = self._get_page_user_id(driver)
                if current_userid and current_userid != user_id:
                    logging.error(
                        f"户号匹配失败：期望 {user_id}，页面当前为 {current_userid}，跳过该户号"
                    )
                    continue
                if current_userid in self.IGNORE_USER_ID:
                    logging.info(f"用户 ID {current_userid} 将被忽略")
                    continue
                data_user_id = current_userid or user_id
                ### 获取数据
                (balance, last_daily_date, last_daily_usage, yearly_charge, yearly_usage,
                 month_charge, month_usage, tou_data, enhanced_balance, bill_tou_data,
                 user_name, month_period) = self._get_all_data(driver, data_user_id, userid_index)
                logging.info(f"用户 [{data_user_id}] 数据获取完成: 余额={balance}元, 最近日用电={last_daily_usage}度({last_daily_date}), "
                             f"年度用电={yearly_usage}度, 年度电费={yearly_charge}元, 月用电={month_usage}度, 月电费={month_charge}元")
                try:
                    self._sync_extras(
                        driver, data_user_id, userid_index, user_name=user_name
                    )
                except Exception as exc:
                    logging.warning(f"[{data_user_id}] 附加数据同步失败（不影响主流程）: {exc}")
                processed_user_ids.append(data_user_id)

                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            except Exception as e:
                if (userid_index != len(user_id_list)):
                    logging.info(f"当前用户 {user_id} 数据抓取失败: {e}，将继续抓取下一个用户数据。")
                else:
                    logging.info(f"用户 {user_id} 数据抓取失败: {e}")
                    logging.info("数据抓取完成后浏览器驱动退出。")
                continue

        return processed_user_ids


    def _get_current_userid(self, driver) -> str:
        """读取当前页面的用户户号（兼容多种页面布局）"""
        # 方式一：从"用电户号"标签中读取
        try:
            label = driver.find_element(By.XPATH, "//*[contains(normalize-space(.), '用电户号')]").text or ""
            matches = re.findall(r"\b\d{13}\b", label)
            if matches:
                return matches[-1]
        except Exception:
            pass
        # 方式二：从页面源码中正则匹配
        try:
            page_source = driver.page_source or ""
            match = re.search(r"用电户号[:：\s]*([0-9]{13})", page_source)
            if match:
                return match.group(1)
        except Exception:
            pass
        # 方式三：从下拉框中读取当前选中项
        try:
            dropdown = driver.find_element(By.CLASS_NAME, "el-dropdown")
            text = dropdown.text or ""
            matches = re.findall(r"\b\d{13}\b", text)
            if matches:
                return matches[-1]
        except Exception:
            pass
        logging.warning("无法读取当前户号")
        return ""

    def _open_user_selector(self, driver) -> bool:
        """打开户号选择器；优先使用用电量/账单页的 .houseNum 下拉。"""
        inputs = driver.find_elements(By.CSS_SELECTOR, ".houseNum .el-select .el-input__inner")
        if inputs:
            try:
                driver.execute_script("arguments[0].click();", inputs[0])
                return True
            except Exception:
                pass

        # 顶部导航的通用账户下拉（电费账单等页面使用）
        try:
            dropdown = driver.find_element(By.CLASS_NAME, "el-dropdown")
            trigger = dropdown.find_element(By.TAG_NAME, "span")
            driver.execute_script("arguments[0].click();", trigger)
            return True
        except Exception:
            pass

        for xpath in (
            "//span[contains(normalize-space(.), '切换用户')]",
            "//div[contains(@class,'houseNum')]//div[contains(@class,'el-select')]//span[contains(@class,'el-input__suffix')]",
            "//div[contains(@class,'houseNum')]//span[contains(normalize-space(.), '切换用户')]",
        ):
            try:
                trigger = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                driver.execute_script("arguments[0].click();", trigger)
                return True
            except Exception:
                continue

        try:
            self._click_button(driver, By.CLASS_NAME, "el-input__suffix")
            return True
        except Exception:
            return False

    def _choose_current_userid(self, driver, userid_index, target_user_id: str = ""):
        """切换到指定户号；优先按户号文本匹配，匹配不到再按索引。"""
        # 关闭确认弹窗（如果有）
        elements = driver.find_elements(By.CLASS_NAME, "button_confirm")
        if elements:
            try:
                self._click_button(driver, By.XPATH, "//*[@id='app']/div/div[2]/div/div/div/div[2]/div[2]/div/button")
            except Exception:
                pass
        time.sleep(self.PAGE_WAIT_TIME)

        # 下拉选项轮询不能叠加 60 秒隐式等待，否则找不到选项时会卡很久
        driver.implicitly_wait(0)
        try:
            # 打开用户选择器并等待选项出现
            options = []
            for attempt in range(3):
                self._open_user_selector(driver)
                deadline = time.time() + 6
                while time.time() < deadline:
                    options = self._get_visible_user_options(driver)
                    if options:
                        break
                    time.sleep(1)
                if options:
                    break
                time.sleep(2)
            if not options:
                logging.error("户号下拉选项为空，无法切换用户")
                return False

            # 获取下拉选项并点击目标
            target = None
            if target_user_id:
                for option in options:
                    try:
                        text = option.text or ""
                    except Exception:
                        continue
                    if target_user_id in text:
                        target = option
                        break
                if target is None:
                    # 某些页面只显示户号后 4 位或友好名称，无法用完整户号匹配；
                    # 回退按索引点击，随后由 _get_page_user_id 校验是否切换正确。
                    logging.warning(
                        f"下拉选项中未匹配到完整户号 {target_user_id}，回退按索引 {userid_index} 切换"
                    )
            if target is None:
                if userid_index >= len(options):
                    logging.error(f"用户索引 {userid_index} 超出范围, 共 {len(options)} 个选项")
                    return False
                target = options[userid_index]
            driver.execute_script("arguments[0].click();", target)
            logging.info(f"已切换到用户索引 {userid_index}（目标户号 {target_user_id or '按索引'}）")
            return True
        finally:
            driver.implicitly_wait(self.DRIVER_IMPLICITY_WAIT_TIME)

    def _get_visible_user_options(self, driver):
        """获取可见的用户下拉选项（兼容 el-dropdown 和 el-select）"""
        return [
            option
            for option in driver.find_elements(
                By.XPATH,
                "//ul[contains(@class,'el-dropdown-menu')]//li"
                " | //div[contains(@class,'el-select-dropdown')]//li",
            )
            if option.is_displayed()
            and "is-disabled" not in (option.get_attribute("class") or "")
            and "disabled" not in (option.get_attribute("class") or "")
        ]


    def _get_all_data(self, driver, user_id, userid_index):
        logging.info(f"[{user_id}] 正在获取电费余额...")
        balance = self._get_electric_balance(driver)
        if balance is None:
            logging.error(f"[{user_id}] 获取电费余额失败")
        else:
            logging.info(f"[{user_id}] 电费余额: {balance} 元")

        # 尝试通过 Vue state 获取增强余额（与数据库是否启用无关，MQTT 也需要）
        enhanced_balance = None
        user_name = self._user_name_map.get(user_id, "")
        if user_name:
            logging.info(f"[{user_id}] 用户名: {user_name}")
        try:
            components = vue_state.selected_vue_data(driver)
            enhanced_balance = vue_state.normalize_balance(components)
        except Exception as e:
            logging.warning(f"[{user_id}] 增强余额获取失败: {e}")

        logging.info(f"[{user_id}] 正在切换到用电量页面...")
        driver.get(ELECTRIC_USAGE_URL)
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        try:
            self._choose_current_userid(driver, userid_index, target_user_id=user_id)
        except Exception as e:
            logging.warning(f"[{user_id}] 用电量页面用户切换失败 (非致命): {e}")
        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
        page_user_id = self._get_page_user_id(driver)
        if page_user_id and page_user_id != user_id:
            raise RuntimeError(
                f"用电量页面户号 {page_user_id} 与目标 {user_id} 不一致"
            )

        logging.info(f"[{user_id}] 正在获取年度用电数据...")
        yearly_usage, yearly_charge = self._get_yearly_data(driver)
        if yearly_usage is None:
            logging.error(f"[{user_id}] 获取年度用电量失败")
        else:
            logging.info(f"[{user_id}] 年度用电量: {yearly_usage} 度")
        if yearly_charge is None:
            logging.error(f"[{user_id}] 获取年度电费失败")
        else:
            logging.info(f"[{user_id}] 年度电费: {yearly_charge} 元")

        logging.info(f"[{user_id}] 正在获取月度用电数据...")
        month, month_usage, month_charge = self._get_month_usage(driver)
        if month is None:
            logging.error(f"[{user_id}] 获取月度用电数据失败")
        else:
            for m in range(len(month)):
                logging.info(f"[{user_id}] {month[m]}: 用电 {month_usage[m]} 度, 电费 {month_charge[m]} 元")

        logging.info(f"[{user_id}] 正在获取每日用电量...")
        last_daily_date, last_daily_usage = self._get_yesterday_usage(driver)
        if last_daily_usage is None:
            logging.error(f"[{user_id}] 获取每日用电量失败")
        else:
            logging.info(f"[{user_id}] 最近用电: {last_daily_date} 用电 {last_daily_usage} 度")

        # 先获取电费账单明细（月度分时）；保持在日用电量页面当前状态，
        # 避免后续切换 30 天视图影响 Vue 里的 billList。
        bill_tou_data = None
        try:
            bill_tou_data = self._get_bill_detail(driver, user_id)
        except Exception as e:
            logging.warning(f"[{user_id}] 电费账单分时数据获取失败: {e}")

        # 先切换到 7/30 天视图并读取 DOM，再读取 Vue state，
        # 这样 tou_data 里的日分时数据也能覆盖整个 30 天窗口。
        date_list, usage_list = self._get_daily_usage_data(driver)

        # 尝试通过 Vue state 获取分时电量（与数据库是否启用无关，MQTT 也需要）
        tou_data = None
        try:
            components = vue_state.selected_vue_data(driver)
            usage_info = vue_state.normalize_usage(components)
            tou_data = usage_info
            logging.info(f"[{user_id}] Vue state 分时数据: 年度={usage_info.get('year')}, "
                         f"月数据={len(usage_info.get('months', []))}条, "
                         f"日数据={len(usage_info.get('daily', []))}条")
            # 打印 Vue state 日数据详情
            if usage_info.get("daily"):
                for d in usage_info["daily"][:7]:
                    logging.info(f"  [日数据] {d.get('date')}: "
                                 f"总={d.get('total_usage')}度, "
                                 f"谷={d.get('valley_usage')}, 平={d.get('flat_usage')}, "
                                 f"峰={d.get('peak_usage')}, 尖={d.get('tip_usage')}")
                if len(usage_info["daily"]) > 7:
                    logging.info(f"  ... 还有 {len(usage_info['daily']) - 7} 条日数据")
        except Exception as e:
            logging.warning(f"[{user_id}] Vue state 分时数据获取失败: {e}")

        # 数据库存储
        if self.db is not None:
            logging.info(f"[{user_id}] 数据库类型: {self.db_type}, 开始保存数据到数据库")
            self._save_user_data(
                user_id, balance, enhanced_balance,
                last_daily_date, last_daily_usage,
                date_list, usage_list,
                month, month_usage, month_charge,
                yearly_charge, yearly_usage,
                tou_data, bill_tou_data, user_name,
            )
        else:
            logging.info(f"[{user_id}] 未配置数据库, 跳过数据存储")

        if month_charge:
            month_charge = month_charge[-1]
        else:
            month_charge = None
        if month_usage:
            month_usage = month_usage[-1]
        else:
            month_usage = None

        # 月度传感器对应的是 DOM 表格最后一行，不一定是当前自然月
        page_year = normalize_year((tou_data or {}).get("year")) or str(
            datetime.now().year - 1 if datetime.now().month == 1 else datetime.now().year
        )
        month_period = self._format_month(month[-1], page_year) if month else None

        return (balance, last_daily_date, last_daily_usage, yearly_charge, yearly_usage,
                month_charge, month_usage, tou_data, enhanced_balance, bill_tou_data,
                user_name, month_period)

    def _get_user_ids(self, driver):
        """获取用户 ID 列表。优先从 el-dropdown 获取（余额页面），
        失败则从 el-select 获取（用电量页面），最后从页面源码正则匹配。"""
        try:
            # 方式一：经典方式 - 从 el-dropdown 下拉框获取
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            dropdowns = driver.find_elements(By.CLASS_NAME, 'el-dropdown')
            if dropdowns:
                self._click_button(driver, By.XPATH, "//div[@class='el-dropdown']/span")
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                try:
                    target = driver.find_element(By.CLASS_NAME, "el-dropdown-menu.el-popper").find_element(By.TAG_NAME, "li")
                    WebDriverWait(driver, 10).until(EC.visibility_of(target))
                    WebDriverWait(driver, 10).until(
                        EC.text_to_be_present_in_element((By.XPATH, "//ul[@class='el-dropdown-menu el-popper']/li"), ":"))
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                    userid_elements = driver.find_element(By.CLASS_NAME, "el-dropdown-menu.el-popper").find_elements(By.TAG_NAME, "li")
                    userid_list = []
                    for element in userid_elements:
                        matches = re.findall(r"\b\d{13}\b", element.text or "")
                        if matches:
                            uid = matches[-1]
                            if uid not in userid_list:
                                userid_list.append(uid)
                    if userid_list:
                        logging.info(f"从 el-dropdown 获取到 {len(userid_list)} 个用户: {userid_list}")
                        return userid_list
                except Exception as e:
                    logging.debug(f"el-dropdown 获取失败, 尝试其他方式: {e}")

            # 方式二：从 el-select 下拉框获取（用电量页面）
            try:
                select_inputs = driver.find_elements(By.CSS_SELECTOR, ".houseNum .el-select .el-input__inner")
                if not select_inputs:
                    driver.get(ELECTRIC_USAGE_URL)
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
                    select_inputs = driver.find_elements(By.CSS_SELECTOR, ".houseNum .el-select .el-input__inner")

                if select_inputs:
                    driver.execute_script("arguments[0].click();", select_inputs[0])
                    time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

                    options = driver.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item")
                    userid_list = []
                    for opt in options:
                        text = opt.text.strip()
                        if re.match(r'^\d{4}$', text):
                            continue
                        driver.execute_script("arguments[0].click();", opt)
                        time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                        try:
                            current_id = self._get_current_userid(driver)
                            if current_id and current_id not in userid_list:
                                userid_list.append(current_id)
                                logging.info(f"从 el-select 获取到用户: {current_id} ({text})")
                        except Exception:
                            pass
                        select_inputs = driver.find_elements(By.CSS_SELECTOR, ".houseNum .el-select .el-input__inner")
                        if select_inputs:
                            driver.execute_script("arguments[0].click();", select_inputs[0])
                            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)

                    if userid_list:
                        logging.info(f"从 el-select 获取到 {len(userid_list)} 个用户: {userid_list}")
                        return userid_list
            except Exception as e:
                logging.debug(f"el-select 获取失败: {e}")

            # 方式三：从页面源码正则匹配所有13位户号
            page_source = driver.page_source or ""
            all_ids = list(set(re.findall(r'\b(\d{13})\b', page_source)))
            if all_ids:
                logging.info(f"从页面源码正则匹配到 {len(all_ids)} 个用户: {all_ids}")
                return all_ids

            logging.error("所有方式均未能获取用户 ID 列表")
            return []
        except Exception as e:
            logging.error(f"获取用户 ID 列表异常: {e}")
            return []

    def _get_electric_balance(self, driver):
        try:
            try:
                # 定位是否有"应交金额"标题（确认是后缴费账户）
                title_text = driver.find_element(By.XPATH, "//p[contains(@class, 'balance_title') and contains(text(), '应交金额')]").text
                if "应交金额" in title_text:
                    # 后缴费账户：需要查找"账户余额"，而不是"应交金额"
                    # 查找包含"账户余额"的balance_title元素，然后获取其内部的金额
                    balance_content = driver.find_element(By.XPATH, "//p[contains(@class, 'balance_title') and contains(text(), '账户余额')]")
                    # 提取数字部分
                    balance_text = re.sub(r'[^\d.]', '', balance_content.text)
                    if balance_text:
                        return float(balance_text)
            except Exception as e:
                # 后缴费账户解析失败，继续尝试预缴费账户逻辑
                pass

            # 2. 预缴费账户的"账户余额"（原逻辑）
            balance_text = driver.find_element(By.CLASS_NAME, "cff8").text
            balance = balance_text.replace("元", "")
            if "欠费" in balance_text:
                return -float(balance)
            else:
                return float(balance)
        except Exception as e:
            logging.error(f"获取余额失败: {e}")
            return None

    def _get_yearly_data(self, driver):

        try:
            if datetime.now().month == 1:
                self._click_button(driver, By.XPATH, '//*[@id="pane-first"]/div[1]/div/div[1]/div/div/input')
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                span_element = driver.find_element(By.XPATH, f"//span[text() = '{datetime.now().year - 1}']")
                span_element.click()
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-first']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            # 等待数据显示
            target = driver.find_element(By.CLASS_NAME, "total")
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(EC.visibility_of(target))
        except Exception as e:
            logging.error(f"年度数据获取失败: {e}")
            return None, None

        # 获取数据
        try:
            yearly_usage = driver.find_element(By.XPATH, "//ul[@class='total']/li[1]/span").text
        except Exception as e:
            logging.error(f"年度用电量数据获取失败: {e}")
            yearly_usage = None

        try:
            yearly_charge = driver.find_element(By.XPATH, "//ul[@class='total']/li[2]/span").text
        except Exception as e:
            logging.error(f"年度电费数据获取失败: {e}")
            yearly_charge = None

        return yearly_usage, yearly_charge

    def _get_yesterday_usage(self, driver):
        """获取最近一次用电量"""
        try:
            # 点击日用电量 tab
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
            # 等待数据表格出现（兼容多种滚动类名）
            usage_element = driver.find_element(By.XPATH,"""//*[@id="pane-second"]/div[2]/div[2]/div[1]/div[3]/table/tbody/tr[1]/td[2]/div""")
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(EC.visibility_of(usage_element)) # 等待用电量出现

            # 增加是哪一天
            date_element = driver.find_element(By.XPATH,"""//*[@id="pane-second"]/div[2]/div[2]/div[1]/div[3]/table/tbody/tr[1]/td[1]/div""")
            last_daily_date = date_element.text # 获取最近一次用电量的日期
            return last_daily_date, float(usage_element.text)
        except Exception as e:
            logging.error(f"每日用电量数据获取失败: {e}")
            return None, None

    def _get_month_usage(self, driver):
        """获取每月用电量"""

        try:
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-first']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            if datetime.now().month == 1:
                self._click_button(driver, By.XPATH, '//*[@id="pane-first"]/div[1]/div/div[1]/div/div/input')
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
                span_element = driver.find_element(By.XPATH, f"//span[text() = '{datetime.now().year - 1}']")
                span_element.click()
                time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT)
            # 等待月度数据出现
            target = driver.find_element(By.CLASS_NAME, "total")
            WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(EC.visibility_of(target))
            month_element = driver.find_element(By.XPATH, "//*[@id='pane-first']/div[1]/div[2]/div[2]/div/div[3]/table/tbody").text
            month_element = month_element.split("\n")
            month_element = [x for x in month_element if x != "MAX"]
            if len(month_element) % 3 != 0:
                month_element = month_element[:-(len(month_element) % 3)]
            month_element = np.array(month_element).reshape(-1, 3)
            # 将每月的用电量保存为List
            month = []
            usage = []
            charge = []
            for i in range(len(month_element)):
                month.append(month_element[i][0])
                usage.append(month_element[i][1])
                charge.append(month_element[i][2])
            return month, usage, charge
        except Exception as e:
            logging.error(f"月度数据获取失败: {e}")
            return None,None,None

    # 增加获取每日用电量的函数
    def _get_daily_usage_data(self, driver):
        """获取每日用电量数据 (7天或30天)，通过 radio 按钮切换，失败时返回空列表"""
        try:
            fetch_days = int(os.getenv("DAILY_FETCH_DAYS", 7))
            if fetch_days not in (7, 30):
                fetch_days = 7
            logging.info(f"正在获取每日用电量数据 (最近 {fetch_days} 天)")
            # 点击"日用电量" tab
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 3)

            # 通过 radio 按钮点击 7天 或 30天
            if fetch_days == 30:
                try:
                    radio = driver.find_element(By.XPATH,
                        "//span[contains(@class,'el-radio__label') and contains(text(),'近30天')]"
                        "/preceding-sibling::span//input[@class='el-radio__original']")
                    driver.execute_script("arguments[0].click();", radio)
                    logging.info("已点击 '近30天' radio 按钮")
                except Exception:
                    try:
                        self._click_button(driver, By.XPATH,
                            "//*[@id='pane-second']//label[2]//span[@class='el-radio__input']")
                        logging.info("已点击 '近30天' 备用方案")
                    except Exception:
                        logging.warning("未找到 '近30天' radio, 使用默认数据")
            time.sleep(self.RETRY_WAIT_TIME_OFFSET_UNIT * 3)

            # 等待用电量数据出现（兼容多种滚动类名）
            usage_element = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                EC.visibility_of_element_located((
                    By.XPATH,
                    "//div[contains(@class,'el-tab-pane')]//div[contains(@class,'el-table__body-wrapper')]"
                    "//table/tbody/tr[1]/td[2]/div"
                ))
            )

            # 获取用电量数据
            days_element = driver.find_elements(By.XPATH,
                "//*[@id='pane-second']//div[contains(@class,'el-table__body-wrapper')]"
                "/table/tbody/tr")
            date = []
            usages = []
            for i in days_element:
                try:
                    day = i.find_element(By.XPATH, "td[1]/div").text
                    usage = i.find_element(By.XPATH, "td[2]/div").text
                    if usage != "":
                        usages.append(usage)
                        date.append(day)
                except Exception:
                    pass
            logging.info(f"DOM 方式成功获取 {len(date)} 天的每日用电量数据")
            return date, usages
        except Exception as e:
            logging.warning(f"DOM 方式获取每日用电量数据失败: {e}")
            return [], []

    def _get_daily_tou_data(self, driver):
        """通过展开日用电量表格行获取每日分时电量（谷/平/峰/尖）"""
        tou_rows = []
        try:
            # 找到所有展开图标并逐个点击
            expand_icons = driver.find_elements(By.CSS_SELECTOR,
                ".el-table__expand-icon")
            for icon in expand_icons:
                try:
                    driver.execute_script("arguments[0].click();", icon)
                    time.sleep(0.5)
                except Exception:
                    continue

            time.sleep(1)

            # 读取展开行中的分时电量
            expanded_cells = driver.find_elements(By.CSS_SELECTOR,
                ".el-table__expanded-cell .drop-box-left")
            for cell in expanded_cells:
                tou = {"valley_usage": 0.0, "flat_usage": 0.0, "peak_usage": 0.0, "tip_usage": 0.0}
                paragraphs = cell.find_elements(By.TAG_NAME, "p")
                for p in paragraphs:
                    text = p.text
                    try:
                        num_el = p.find_element(By.CSS_SELECTOR, ".num")
                        val = float(num_el.text)
                    except Exception:
                        continue
                    if "谷" in text:
                        tou["valley_usage"] = val
                    elif "平" in text:
                        tou["flat_usage"] = val
                    elif "峰" in text:
                        tou["peak_usage"] = val
                    elif "尖" in text:
                        tou["tip_usage"] = val
                tou_rows.append(tou)
            logging.info(f"通过展开行获取到 {len(tou_rows)} 条分时电量数据")
        except Exception as e:
            logging.warning(f"获取展开行分时电量失败: {e}")
        return tou_rows

    def _get_bill_detail(self, driver, user_id):
        """从用电量页面通过 Vue state 获取月度分时电量"""
        logging.info(f"[{user_id}] 尝试从当前页面获取电费账单分时数据...")
        try:
            # 不再跳转到会返回 403 的账单汇总页面, 直接从当前页面提取
            components = vue_state.selected_vue_data(driver)
            bill = vue_state.normalize_bill_detail(components)
            if bill.get("month"):
                logging.info(f"[{user_id}] 账单分时数据: {bill['month']}, "
                             f"谷={bill.get('valley_usage')}, 平={bill.get('flat_usage')}, "
                             f"峰={bill.get('peak_usage')}, 尖={bill.get('tip_usage')}")
                return bill
            logging.info(f"[{user_id}] Vue state 中未找到账单数据, 跳过")
            return None
        except Exception as e:
            logging.warning(f"[{user_id}] 获取账单分时数据异常: {e}")
            return None

    # ------------------------------------------------------------------
    # 交费记录 / 官方账单 / 阶梯 / 全年回补
    # ------------------------------------------------------------------
    def _get_page_user_id(self, driver) -> str:
        """优先从 Vue state 读取当前户号，避免页面源码里匹配到其它户号。"""
        try:
            for component in vue_state.selected_vue_data(driver) or []:
                data = component.get("data") or {}
                for key in (
                    "listData", "billListS", "billMonthS", "consInfoobj",
                    "consInfo", "cons", "electric", "usermasg",
                ):
                    obj = data.get(key)
                    if not isinstance(obj, dict):
                        continue
                    for id_key in ("consNo_dst", "consNoDst", "consNo", "consId"):
                        value = str(obj.get(id_key) or "").strip()
                        if re.fullmatch(r"\d{13}", value):
                            return value
        except Exception:
            pass
        return self._get_current_userid(driver)

    def _select_on_balance_page(self, driver, userid_index: int, label: str, expected_user_id: str = "") -> str:
        """余额页的账户下拉最稳定，先用它设置全局户号。"""
        driver.get(BALANCE_URL)
        time.sleep(self.PAGE_WAIT_TIME)
        try:
            WebDriverWait(driver, max(self.PAGE_WAIT_TIME, 15)).until(
                EC.presence_of_element_located((By.ID, "app"))
            )
        except Exception:
            pass
        try:
            self._choose_current_userid(driver, userid_index, target_user_id=expected_user_id)
        except Exception as exc:
            logging.warning(f"[{label}] 余额页切换户号失败（忽略）: {exc}")
        time.sleep(self.PAGE_WAIT_TIME)

        current = ""
        if expected_user_id:
            current = self._get_page_user_id(driver)
            if current and current != expected_user_id:
                logging.warning(
                    f"[{label}] 余额页户号 {current} 与目标 {expected_user_id} 不一致，重试切换"
                )
                try:
                    self._choose_current_userid(driver, userid_index, target_user_id=expected_user_id)
                    time.sleep(self.PAGE_WAIT_TIME)
                except Exception as exc:
                    logging.warning(f"[{label}] 余额页重试切换户号失败: {exc}")
                current = self._get_page_user_id(driver)
        return current

    def _open_user_page(self, driver, url: str, userid_index: int, label: str, expected_user_id: str = ""):
        # 先在余额页切换到目标户号，再打开目标页面；国网会把户号保存在全局状态里
        current = self._select_on_balance_page(driver, userid_index, label, expected_user_id)
        if expected_user_id and current and current != expected_user_id:
            raise RuntimeError(
                f"{label} 余额页户号 {current} 与目标 {expected_user_id} 不一致"
            )

        driver.get(url)
        time.sleep(self.PAGE_WAIT_TIME)
        try:
            WebDriverWait(driver, max(self.PAGE_WAIT_TIME, 15)).until(
                EC.presence_of_element_located((By.ID, "app"))
            )
        except Exception:
            pass
        time.sleep(self.PAGE_WAIT_TIME)

        if not expected_user_id:
            return
        current = self._get_page_user_id(driver)
        if current and current == expected_user_id:
            return

        logging.warning(
            f"[{label}] 目标页户号 {current or '未知'} 与目标 {expected_user_id} 不一致，尝试页面内切换"
        )
        try:
            self._choose_current_userid(driver, userid_index, target_user_id=expected_user_id)
            time.sleep(self.PAGE_WAIT_TIME)
        except Exception as exc:
            logging.warning(f"[{label}] 页面内切换户号失败: {exc}")
        current = self._get_page_user_id(driver)
        if current and current != expected_user_id:
            raise RuntimeError(
                f"{label} 页面户号 {current} 与目标 {expected_user_id} 不一致"
            )

    def _get_payment_records(self, driver, user_id: str, userid_index: int):
        """读取交费记录页的 payLists，默认查询去年至今的记录。"""
        logging.info(f"[{user_id}] 正在获取交费记录...")
        self._open_user_page(driver, PAYMENT_URL, userid_index, "payment", user_id)

        try:
            WebDriverWait(driver, max(self.PAGE_WAIT_TIME * 2, 20)).until(
                lambda d: any(
                    isinstance((component.get("data") or {}).get("payLists"), list)
                    and bool((component.get("data") or {}).get("mixinGetYuEdata"))
                    for component in vue_state.selected_vue_data(d) or []
                )
            )
        except Exception:
            logging.warning(f"[{user_id}] 交费记录组件加载超时")

        existing = vue_state.normalize_payment_records(vue_state.selected_vue_data(driver))
        start = os.getenv("PAYMENT_FETCH_START", "").strip() or f"{datetime.now().year - 1}-01-01"
        end = datetime.now().strftime("%Y-%m-%d")
        record_num = int(os.getenv("PAYMENT_FETCH_LIMIT", "200"))

        script = """
        const start = arguments[0];
        const end = arguments[1];
        const limit = arguments[2];
        const vms = Array.from(document.querySelectorAll('*'))
          .map((el) => el.__vue__)
          .filter((vm) => vm && Array.isArray(vm.payLists) && typeof vm.getSearchRecords === 'function');
        if (!vms.length) return { ok: false, reason: 'payment_vue_not_found' };
        const vm = vms[0];
        if (start) vm.begin = start;
        if (end) vm.ending = end;
        vm.pageNo = 1;
        vm.recordNum = limit || 200;
        vm.getSearchRecords();
        return { ok: true, begin: vm.begin, ending: vm.ending };
        """
        try:
            result = driver.execute_script(script, start, end, record_num)
            logging.info(f"[{user_id}] 交费记录查询参数: {result}")
        except Exception as exc:
            logging.warning(f"[{user_id}] 触发交费记录查询失败: {exc}")
            return existing

        deadline = time.time() + int(os.getenv("PAYMENT_FETCH_TIMEOUT", "40"))
        records = existing
        while time.time() < deadline:
            time.sleep(2)
            records = vue_state.normalize_payment_records(vue_state.selected_vue_data(driver))
            if records:
                break
        if len(records) < len(existing):
            records = existing
        logging.info(f"[{user_id}] 获取到 {len(records)} 条交费记录")
        return records

    def _visible_bill_entries(self, driver, year: int):
        """返回 [(行元素, YYYY-MM)]，用于按月份打开账单详情。"""
        entries = []
        rows = driver.find_elements(By.XPATH, BILL_LIST_ROW)
        for row in rows:
            try:
                text = row.text or ""
            except Exception:
                continue
            match = re.search(r"(\d{4})[/-](\d{1,2})[/-]\d{1,2}", text)
            if match:
                entries.append((row, f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"))
                continue
            match = re.search(r"(\d{1,2})月", text)
            if match:
                entries.append((row, f"{year:04d}-{int(match.group(1)):02d}"))
        return entries

    def _select_bill_year(self, driver, target_year: int) -> bool:
        try:
            active = driver.find_element(By.XPATH, BILL_ACTIVE_YEAR).text.strip()
            if active == f"{target_year}年":
                return True
            option = WebDriverWait(driver, self.DRIVER_IMPLICITY_WAIT_TIME).until(
                EC.element_to_be_clickable((By.XPATH, bill_year_option(target_year)))
            )
            driver.execute_script("arguments[0].click();", option)
            time.sleep(self.PAGE_WAIT_TIME)
            WebDriverWait(driver, 20).until(
                EC.visibility_of_element_located((By.XPATH, BILL_CARD))
            )
            return True
        except Exception as exc:
            logging.warning(f"切换到账单年份 {target_year} 失败: {exc}")
            return False

    def _expand_bill_summary(self, driver):
        for _ in range(6):
            buttons = driver.find_elements(By.XPATH, BILL_EXPAND_BUTTON)
            if not buttons:
                return
            try:
                previous_count = len(driver.find_elements(By.XPATH, BILL_LIST_ROW))
                driver.execute_script("arguments[0].click();", buttons[0])
                time.sleep(5)
                current_count = len(driver.find_elements(By.XPATH, BILL_LIST_ROW))
                if current_count <= previous_count:
                    return
            except Exception:
                return

    def _open_bill_row(self, driver, row) -> bool:
        try:
            arrow = row.find_element(By.XPATH, BILL_ROW_DETAIL_ARROW)
            driver.execute_script("arguments[0].click();", arrow)
            time.sleep(self.PAGE_WAIT_TIME)
            WebDriverWait(driver, 20).until(
                EC.visibility_of_element_located((By.XPATH, BILL_DETAIL_CYCLE))
            )
            return True
        except Exception as exc:
            logging.warning(f"打开账单详情失败: {exc}")
            return False

    def _save_official_bill(self, user_id: str, detail: dict, fallback_month: str = ""):
        month = normalize_month(detail.get("month")) or fallback_month
        if not month:
            return None
        detail["month"] = month
        usage = to_float(detail.get("usage"))
        charge = to_float(detail.get("charge"))
        tou_values = [
            to_float(detail.get(key))
            for key in ("valley_usage", "flat_usage", "peak_usage", "tip_usage")
        ]
        if usage is None and charge is None and all(value is None for value in tou_values):
            logging.warning(f"[{user_id}] {month} 账单详情没有电量/电费数据，暂不标记为官方账单")
            return None
        self.db.ensure_connected(user_id)
        self.db.insert_monthly_bill(detail)
        self.db.insert_monthly_data({
            "month": month,
            "total_usage": detail.get("usage"),
            "total_charge": detail.get("charge"),
            "valley_usage": detail.get("valley_usage"),
            "flat_usage": detail.get("flat_usage"),
            "peak_usage": detail.get("peak_usage"),
            "tip_usage": detail.get("tip_usage"),
            "source": "official",
        })
        # 账单里的 yearPq/yearAmt 只是“该账单月为止”的累计，不能直接写年度表；
        # 年度汇总统一从月度表重新计算，避免 1 月账单把全年数据覆盖成 29 度。
        self.db.refresh_yearly_from_monthly(month[:4])
        logging.info(
            f"[{user_id}] 官方账单已保存: {month}, 电量={detail.get('usage')}, "
            f"电费={detail.get('charge')}, 阶梯={detail.get('ladder_num') or '-'}"
        )
        return detail

    def _sync_bill_months(self, driver, user_id: str, userid_index: int, target_months: list[str]):
        if not target_months:
            return {}
        target_year = int(target_months[0][:4])
        logging.info(f"[{user_id}] 正在同步官方账单月份: {target_months}")
        self._open_user_page(driver, BILL_SUMMARY_URL, userid_index, "bill_summary", user_id)
        try:
            WebDriverWait(driver, 20).until(
                EC.visibility_of_element_located((By.XPATH, BILL_CARD))
            )
        except Exception:
            logging.warning(f"[{user_id}] 电费账单页面加载失败")
            return {}

        self._select_bill_year(driver, target_year)
        self._expand_bill_summary(driver)
        entries = self._visible_bill_entries(driver, target_year)
        if not entries:
            logging.warning(f"[{user_id}] 账单页面没有可用的月份行")
            return {}
        logging.info(f"[{user_id}] 账单页可见月份: {[month for _, month in entries]}")

        saved = {}
        for month in target_months:
            # driver.back() 后账单列表会重绘，之前拿到的行元素会失效，必须重新读取
            entries = self._visible_bill_entries(driver, target_year)
            entry_map = {entry_month: row for row, entry_month in entries}
            row = entry_map.get(month)
            if row is None:
                logging.warning(f"[{user_id}] 账单页未找到月份 {month}，跳过")
                continue
            if not self._open_bill_row(driver, row):
                # 页面可能刚好重绘，重新取一次行元素再试
                entries = self._visible_bill_entries(driver, target_year)
                entry_map = {entry_month: new_row for new_row, entry_month in entries}
                row = entry_map.get(month)
                if row is None or not self._open_bill_row(driver, row):
                    continue
            try:
                components = vue_state.selected_vue_data(driver)
                detail = vue_state.normalize_bill_detail_full(components, month)
                if not detail.get("month"):
                    detail["month"] = month
                if normalize_month(detail.get("month")) == month:
                    saved[month] = self._save_official_bill(user_id, detail, month)
                else:
                    logging.warning(
                        f"[{user_id}] 账单详情月份({detail.get('month')})与目标({month})不一致，仍按目标月份保存"
                    )
                    saved[month] = self._save_official_bill(user_id, detail, month)
            finally:
                try:
                    driver.back()
                    time.sleep(self.PAGE_WAIT_TIME)
                    WebDriverWait(driver, 20).until(
                        EC.visibility_of_element_located((By.XPATH, BILL_CARD))
                    )
                    self._expand_bill_summary(driver)
                except Exception as exc:
                    logging.warning(f"[{user_id}] 返回账单列表失败: {exc}")
                    break
        return saved

    def _get_tier_snapshot(self, driver, user_id: str, userid_index: int):
        logging.info(f"[{user_id}] 正在获取官方阶梯用电状态...")
        self._open_user_page(driver, STEP_URL, userid_index, "step", user_id)
        deadline = time.time() + int(os.getenv("TIER_SYNC_TIMEOUT", "40"))
        snapshot = {}
        while time.time() < deadline:
            components = vue_state.selected_vue_data(driver) or []
            snapshot = vue_state.normalize_tier_snapshot(components)
            if snapshot.get("one_level_pq") is not None or snapshot.get("level_flag") is not None:
                break
            time.sleep(2)
        if snapshot.get("one_level_pq") is None and snapshot.get("level_flag") is None:
            logging.warning(f"[{user_id}] 未获取到阶梯数据（可能不是阶梯用户）")
            return None
        snapshot["snapshot_date"] = datetime.now().strftime("%Y-%m-%d")
        snapshot["source"] = "official"
        logging.info(
            f"[{user_id}] 官方阶梯: levelFlag={snapshot.get('level_flag')}, "
            f"oneLevelPq={snapshot.get('one_level_pq')}, twoLevelPq={snapshot.get('two_level_pq')}, "
            f"totalYearPq={snapshot.get('total_year_pq')}, totalMonthPq={snapshot.get('total_month_pq')}, "
            f"ladderNum={snapshot.get('ladder_num')}"
        )
        return snapshot

    def _query_daily_range(self, driver, start_date: str, end_date: str):
        script = """
        const start = arguments[0];
        const end = arguments[1];
        const candidates = Array.from(document.querySelectorAll('*'))
          .map((el) => el.__vue__)
          .filter((vm) => vm && typeof vm.getdata === 'function' && Array.isArray(vm.sevenEleList));
        const vm = candidates.find((item) =>
          Object.prototype.hasOwnProperty.call(item, 'start') &&
          Object.prototype.hasOwnProperty.call(item, 'end')
        ) || candidates[0];
        if (!vm) return { ok: false, reason: 'daily_vue_component_not_found' };
        vm.start = start;
        vm.end = end;
        vm.dateradio = '';
        vm.sevenEleList = [];
        vm.getdata();
        return { ok: true, start: vm.start, end: vm.end };
        """
        result = driver.execute_script(script, start_date, end_date)
        if not result or not result.get("ok"):
            logging.warning(f"日用电量区间查询组件未找到: {result}")
            return []
        deadline = time.time() + int(os.getenv("DAILY_RANGE_TIMEOUT", "90"))
        rows = []
        while time.time() < deadline:
            time.sleep(3)
            rows = driver.execute_script(
                """
                const candidates = Array.from(document.querySelectorAll('*'))
                  .map((el) => el.__vue__)
                  .filter((vm) => vm && Array.isArray(vm.sevenEleList));
                const vm = candidates.find((item) =>
                  Object.prototype.hasOwnProperty.call(item, 'start') &&
                  Object.prototype.hasOwnProperty.call(item, 'end')
                ) || candidates[0];
                if (!vm) return [];
                return (vm.sevenEleList || []).map((row) => ({
                  date: row.day,
                  total_usage: row.dayElePq,
                  valley_usage: row.thisVPq,
                  flat_usage: row.thisNPq,
                  peak_usage: row.thisPPq,
                  tip_usage: row.thisTPq,
                }));
                """
            ) or []
            if rows:
                break
        return rows

    def _fetch_daily_range(self, driver, user_id: str, userid_index: int, start_date: str, end_date: str):
        logging.info(f"[{user_id}] 正在回补日用电量 {start_date} ~ {end_date}")
        self._open_user_page(driver, ELECTRIC_USAGE_URL, userid_index, "daily_range", user_id)
        try:
            self._click_button(driver, By.XPATH, "//div[@class='el-tabs__nav is-top']/div[@id='tab-second']")
            time.sleep(self.PAGE_WAIT_TIME)
        except Exception:
            pass

        rows = self._query_daily_range(driver, start_date, end_date)
        normalized = []
        for row in rows:
            row_date = normalize_date(row.get("date"))
            if not row_date:
                continue
            values = [
                to_float(row.get("total_usage")),
                to_float(row.get("valley_usage")),
                to_float(row.get("flat_usage")),
                to_float(row.get("peak_usage")),
                to_float(row.get("tip_usage")),
            ]
            if all(value is None for value in values):
                continue
            total = values[0]
            if total is None:
                total = sum(value or 0.0 for value in values[1:])
            normalized.append({
                "date": row_date,
                "total_usage": total,
                "valley_usage": values[1],
                "flat_usage": values[2],
                "peak_usage": values[3],
                "tip_usage": values[4],
            })
        logging.info(f"[{user_id}] 区间查询返回 {len(rows)} 行，有效 {len(normalized)} 行")
        return normalized

    def _year_backfill_needed(self, year: int) -> bool:
        if year in self._backfilled_years:
            return False
        if os.getenv("FORCE_YEAR_BACKFILL", "false").lower() == "true":
            return True
        today = date.today()
        if today.year != year:
            return False
        start = date(year, 1, 1)
        end = today - timedelta(days=1)
        if end < start:
            return False
        expected = (end - start).days + 1
        summary = self.db.get_daily_summary(start.isoformat(), end.isoformat())
        covered = int(summary.get("count") or 0)
        needed = covered < max(int(expected * 0.8), 10)
        if needed:
            logging.info(
                f"[backfill] {year} 年日数据仅 {covered}/{expected} 天，需要回补"
            )
        return needed

    def _backfill_year(self, driver, user_id: str, userid_index: int, year: int):
        today = date.today()
        start_text = os.getenv("YEAR_BACKFILL_START", f"{year}-01-01")
        try:
            start = date.fromisoformat(start_text)
        except ValueError:
            start = date(year, 1, 1)
        end = today - timedelta(days=1)
        if end < start:
            return 0
        chunk_days = max(int(os.getenv("YEAR_BACKFILL_CHUNK_DAYS", "90")), 7)
        touched_months = set()
        total_rows = 0
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            rows = self._fetch_daily_range(
                driver, user_id, userid_index, cursor.isoformat(), chunk_end.isoformat()
            )
            self.db.ensure_connected(user_id)
            for row in rows:
                if self.db.insert_daily_data(row):
                    touched_months.add(row["date"][:7])
                    total_rows += 1
            cursor = chunk_end + timedelta(days=1)

        self.db.ensure_connected(user_id)
        for month in sorted(touched_months):
            self.db.sync_calculated_month_from_daily(month)
        self.db.refresh_yearly_from_monthly(str(year))
        self._backfilled_years.add(year)
        logging.info(f"[{user_id}] 年度日数据回补完成: 写入 {total_rows} 条，涉及月份 {sorted(touched_months)}")
        return total_rows

    def _missing_bill_months(self, year: int) -> list[str]:
        today = date.today()
        last_closed = today.replace(day=1) - timedelta(days=1)
        months = []
        cursor = date(year, 1, 1)
        while cursor <= last_closed:
            months.append(cursor.strftime("%Y-%m"))
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        # 1 月 1 日时要补查上一年 12 月的账单
        if last_closed.year < year:
            months.insert(0, last_closed.strftime("%Y-%m"))
        missing = [month for month in dict.fromkeys(months) if not self.db.has_official_bill(month)]
        limit = int(os.getenv("BILL_SYNC_MAX_MONTHS", "12"))
        return missing[:limit]

    def _sync_extras(self, driver, user_id: str, userid_index: int, user_name: str = ""):
        """交费记录、官方账单、阶梯状态、全年数据回补；返回 HA 需要的附加数据。"""
        extras = {}
        if self.db is None:
            return extras
        if not self.db.connect_user_db(user_id):
            logging.warning(f"[{user_id}] 附加数据同步跳过：数据库连接失败")
            return extras

        today = date.today()
        year = today.year
        try:
            if os.getenv("YEAR_BACKFILL_ENABLED", "true").lower() == "true" and self._year_backfill_needed(year):
                try:
                    self._backfill_year(driver, user_id, userid_index, year)
                except Exception as exc:
                    logging.warning(f"[{user_id}] 年度日数据回补失败: {exc}")

            if os.getenv("BILL_SYNC_ENABLED", "true").lower() == "true":
                try:
                    if today.day == 1:
                        logging.info(f"[{user_id}] 今天是每月 1 号，检查上个月电费账单")
                    target_months = self._missing_bill_months(year)
                    if target_months:
                        self._sync_bill_months(driver, user_id, userid_index, target_months)
                    else:
                        logging.info(f"[{user_id}] 本年度官方账单已齐全，无需同步")
                except Exception as exc:
                    logging.warning(f"[{user_id}] 官方账单同步失败: {exc}")

            if os.getenv("PAYMENT_FETCH_ENABLED", "true").lower() == "true":
                try:
                    self.db.ensure_connected(user_id)
                    latest_payment = self.db.get_latest_payment()
                    payment_interval = int(os.getenv("PAYMENT_FETCH_INTERVAL_DAYS", "7"))
                    should_fetch_payment = latest_payment is None or today.day == 1
                    if not should_fetch_payment and payment_interval > 0:
                        last_pay_date = str(latest_payment.get("pay_date") or "")[:10]
                        try:
                            should_fetch_payment = (
                                today - date.fromisoformat(last_pay_date)
                            ).days >= payment_interval
                        except ValueError:
                            should_fetch_payment = True
                    if should_fetch_payment:
                        records = self._get_payment_records(driver, user_id, userid_index)
                        self.db.ensure_connected(user_id)
                        for record in records:
                            record["user_name"] = user_name
                            self.db.insert_payment(record)
                        if records:
                            logging.info(f"[{user_id}] 交费记录已写入 {len(records)} 条")
                    else:
                        logging.info(f"[{user_id}] 交费记录最近已同步，跳过")
                except Exception as exc:
                    logging.warning(f"[{user_id}] 交费记录同步失败: {exc}")

            if os.getenv("TIER_SYNC_ENABLED", "true").lower() == "true":
                try:
                    self.db.ensure_connected(user_id)
                    snapshot = self.db.get_latest_tier_snapshot()
                    should_fetch_tier = (
                        today.day == 1
                        or snapshot is None
                        or str(snapshot.get("snapshot_date") or "")[:7] != today.strftime("%Y-%m")
                    )
                    if should_fetch_tier:
                        snapshot = self._get_tier_snapshot(driver, user_id, userid_index)
                        if snapshot:
                            self.db.ensure_connected(user_id)
                            self.db.insert_tier_snapshot(snapshot)
                    else:
                        logging.info(f"[{user_id}] 本月官方阶梯已同步，跳过")
                except Exception as exc:
                    logging.warning(f"[{user_id}] 官方阶梯状态同步失败: {exc}")
            try:
                self.db.ensure_connected(user_id)
                self.db.touch_user_data(user_id)
            except Exception as exc:
                logging.warning(f"[{user_id}] 更新数据更新时间失败: {exc}")
        finally:
            self.db.close_connect()
        return extras

    @staticmethod
    def _format_month(value, year_hint: str):
        """把国网页面上的月份文本格式化为 YYYY-MM。

        页面可能出现 "1月1日-1月31日"、"01"、"2026-09" 等格式；
        没有年份时使用调用方传入的页面年份，避免 1 月查询上年数据时年份错位。
        """
        text = str(value or "").strip()
        # 页面常见格式："2026-01-01 00:00:00.0-2026-01-31 00:00:00.0"
        # 或 "2026-08-01-2026-08-31"，取第一个年月即可
        match = re.search(r"(\d{4})[-/](\d{1,2})", text)
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}"
        match = re.search(r'(\d{1,2})月', text)
        if match:
            return f"{year_hint}-{int(match.group(1)):02d}"
        if re.fullmatch(r'\d{1,2}', text):
            return f"{year_hint}-{int(text):02d}"
        return normalize_month(text)

    def _save_user_data(self, user_id, balance, enhanced_balance,
                        last_daily_date, last_daily_usage,
                        date_list, usage_list,
                        month, month_usage, month_charge,
                        yearly_charge, yearly_usage,
                        tou_data=None, bill_tou_data=None, user_name=""):
        if not self.db.connect_user_db(user_id):
            logging.error(f"[{user_id}] 数据库连接失败, 数据未写入")
            return

        # 1 月页面默认展示上一年数据；Vue state 的 year 是最可靠的页面年份
        page_year = normalize_year((tou_data or {}).get("year")) or str(
            datetime.now().year - 1 if datetime.now().month == 1 else datetime.now().year
        )
        # 日常只核对最近 N 天；首次全年回补由 _backfill_year 单独完成
        reconcile_days = max(int(os.getenv("DAILY_RECONCILE_DAYS", "7")), 1)
        daily_cutoff = (datetime.now() - timedelta(days=reconcile_days)).date().isoformat()
        try:
            self.db.upsert_user(user_id, self._username, user_name)
            logging.info(f"[{user_id}] 用户信息已更新 (user_name={user_name})")

            # 写入余额日志
            if balance is not None:
                bal_data = {"balance": balance, "user_name": user_name}
                if enhanced_balance:
                    bal_data.update({
                        "as_of": enhanced_balance.get("as_of"),
                        "amount_due": enhanced_balance.get("amount_due"),
                    })
                self.db.insert_balance_log(bal_data)
                logging.info(f"[{user_id}] 余额日志已写入: {balance} 元")

            # 写入每日用电量（DOM 方式）
            if date_list:
                for i in range(len(date_list)):
                    row_date = normalize_date(date_list[i])
                    if row_date and row_date < daily_cutoff:
                        continue
                    try:
                        self.db.insert_daily_data({
                            "date": date_list[i],
                            "total_usage": to_float(usage_list[i]),
                            "user_name": user_name,
                        })
                    except Exception as e:
                        logging.debug(f"[{user_id}] 日用电 {date_list[i]} 写入失败 (可能已存在): {e}")
                logging.info(f"[{user_id}] 每日用电量已写入 {len(date_list)} 条")

            # 写入 Vue state 分时日用电量（覆盖 DOM 数据中的分时字段）
            if tou_data and tou_data.get("daily"):
                tou_count = 0
                for row in tou_data["daily"]:
                    row_date = normalize_date(row.get("date"))
                    if row_date and row_date < daily_cutoff:
                        continue
                    try:
                        row["user_name"] = user_name
                        self.db.insert_daily_data(row)
                        tou_count += 1
                    except Exception as e:
                        logging.debug(f"[{user_id}] 分时日用电 {row.get('date')} 写入失败: {e}")
                logging.info(f"[{user_id}] Vue state 分时日用电已写入 {tou_count} 条")

            # 写入月度用电量（DOM 方式，属于页面计算值）
            if month:
                for i in range(len(month)):
                    try:
                        # 将 "1月1日-1月31日" 格式转为 "2026-01"
                        m_formatted = self._format_month(month[i], page_year)
                        self.db.insert_monthly_data({
                            "month": m_formatted,
                            "total_usage": to_float(month_usage[i]),
                            "total_charge": to_float(month_charge[i]),
                            "user_name": user_name,
                            "source": "calculated",
                        })
                    except Exception as e:
                        logging.debug(f"[{user_id}] 月度 {month[i]} 写入失败: {e}")
                logging.info(f"[{user_id}] 月度用电量已写入 {len(month)} 条")

            # 写入 Vue state 分时月用电量
            if tou_data and tou_data.get("months"):
                for m_row in tou_data["months"]:
                    try:
                        m_row["user_name"] = user_name
                        m_row["source"] = "calculated"
                        if normalize_month(m_row.get("month")) is None:
                            m_row["month"] = self._format_month(m_row.get("month"), page_year)
                        self.db.insert_monthly_data(m_row)
                    except Exception as e:
                        logging.debug(f"[{user_id}] 分时月度 {m_row.get('month')} 写入失败: {e}")
                logging.info(f"[{user_id}] Vue state 分时月用电已写入 {len(tou_data['months'])} 条")

            # 写入账单分时月用电量（官方账单优先级最高）
            if bill_tou_data and bill_tou_data.get("month"):
                try:
                    self.db.insert_monthly_data({
                        "month": bill_tou_data["month"],
                        "total_usage": bill_tou_data.get("usage"),
                        "total_charge": bill_tou_data.get("charge"),
                        "valley_usage": bill_tou_data.get("valley_usage"),
                        "flat_usage": bill_tou_data.get("flat_usage"),
                        "peak_usage": bill_tou_data.get("peak_usage"),
                        "tip_usage": bill_tou_data.get("tip_usage"),
                        "user_name": user_name,
                        "source": "official",
                    })
                    logging.info(f"[{user_id}] 账单分时月度数据已写入: {bill_tou_data['month']}")
                except Exception as e:
                    logging.warning(f"[{user_id}] 账单分时月度写入失败: {e}")

            # 写入年度用电量（DOM 方式）
            # 1 月页面展示的是上一年数据，年度归属需要跟随页面
            year = page_year
            if yearly_usage is not None or yearly_charge is not None:
                try:
                    self.db.insert_yearly_data({
                        "year": year,
                        "total_usage": to_float(yearly_usage),
                        "total_charge": to_float(yearly_charge),
                        "user_name": user_name,
                        "source": "calculated",
                    })
                    logging.info(f"[{user_id}] 年度用电量已写入: {year}")
                except Exception as e:
                    logging.warning(f"[{user_id}] 年度用电量写入失败: {e}")

            # 从 Vue state 获取分时年度汇总
            if tou_data and tou_data.get("year"):
                try:
                    self.db.insert_yearly_data({
                        "year": tou_data["year"],
                        "total_usage": to_float(tou_data.get("yearly_usage")),
                        "total_charge": to_float(tou_data.get("yearly_charge")),
                        "user_name": user_name,
                        "source": "calculated",
                    })
                    logging.info(f"[{user_id}] Vue state 年度数据已写入: {tou_data['year']}")
                except Exception as e:
                    logging.warning(f"[{user_id}] Vue state 年度写入失败: {e}")

            # 账单中的年度汇总（官方值）
            if bill_tou_data and (bill_tou_data.get("year_usage") is not None
                                  or bill_tou_data.get("year_charge") is not None):
                try:
                    bill_year = normalize_year(bill_tou_data.get("month")) or year
                    self.db.insert_yearly_data({
                        "year": bill_year,
                        "total_usage": bill_tou_data.get("year_usage"),
                        "total_charge": bill_tou_data.get("year_charge"),
                        "user_name": user_name,
                        "source": "official",
                    })
                    logging.info(f"[{user_id}] 账单年度汇总已写入: {bill_year}")
                except Exception as e:
                    logging.warning(f"[{user_id}] 账单年度汇总写入失败: {e}")

            # 数据清理
            self.db.cleanup_old_data()
            self.db.touch_user_data(user_id)
            logging.info(f"[{user_id}] 数据清理完成")

        except Exception as e:
            logging.error(f"[{user_id}] 数据保存过程出错: {e}")
        finally:
            self.db.close_connect()

if __name__ == "__main__":
    with open("bg.jpg", "rb") as f:
        test1 = f.read()
        print(type(test1))
        print(test1)
