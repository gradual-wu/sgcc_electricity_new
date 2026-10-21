"""页面探针：登录后抓取余额、交费记录、电费账单、阶梯用电页面的 Vue 状态。

仅用于本地排查页面结构，不会写入数据库；输出到 data/probe/。
"""

import json
import logging
import os
import sys
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from const import BALANCE_URL, ELECTRIC_USAGE_URL, get_data_dir
from data_fetcher import DataFetcher
from error_watcher import ErrorWatcher

PAYMENT_URL = "https://95598.cn/osgweb/paymentRecord"
BILL_SUMMARY_URL = "https://95598.cn/osgweb/electricitySummary"
STEP_URL = "https://95598.cn/osgweb/stepElectricityConsumption"


VUE_DUMP_SCRIPT = r"""
const safeClone = (value) => {
  try {
    const text = JSON.stringify(value);
    if (text === undefined) return String(value);
    if (text.length > 30000) return text.slice(0, 30000) + '...[truncated]';
    return JSON.parse(text);
  } catch (e) {
    return String(value).slice(0, 2000);
  }
};
const result = [];
Array.from(document.querySelectorAll('*')).forEach((el, index) => {
  const vm = el.__vue__;
  if (!vm || !vm.$data) return;
  const data = {};
  Object.keys(vm.$data).forEach((key) => {
    if (key.startsWith('$') || key.startsWith('_')) return;
    data[key] = safeClone(vm.$data[key]);
  });
  if (!Object.keys(data).length) return;
  result.push({
    index,
    tag: el.tagName,
    id: el.id || '',
    className: String(el.className || '').slice(0, 200),
    component: (vm.$options && (vm.$options.name || vm.$options._componentTag)) || '',
    text: (el.innerText || el.textContent || '').trim().slice(0, 800),
    data,
  });
});
return JSON.stringify(result);
"""


def _dump(driver, name: str) -> None:
    out_dir = os.path.join(get_data_dir(), "probe")
    os.makedirs(out_dir, exist_ok=True)

    try:
        raw = driver.execute_script(VUE_DUMP_SCRIPT) or "[]"
        components = json.loads(raw)
    except Exception as exc:
        logging.warning("Vue 状态导出失败: %s", exc)
        components = []

    summary = []
    for component in components:
        summary.append(
            {
                "component": component.get("component"),
                "className": component.get("className"),
                "text": component.get("text"),
                "keys": sorted((component.get("data") or {}).keys()),
            }
        )

    payload = {
        "url": driver.current_url,
        "title": driver.title,
        "body_text": (driver.find_element(By.TAG_NAME, "body").text or "")[:8000],
        "components": components,
    }
    with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, f"{name}.summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    try:
        driver.save_screenshot(os.path.join(out_dir, f"{name}.png"))
    except Exception as exc:
        logging.warning("截图失败: %s", exc)

    logging.info("[probe] %s: %s 个 Vue 组件，已保存", name, len(components))
    for item in summary:
        if item["keys"]:
            logging.info(
                "[probe]   %s %s keys=%s",
                item["component"] or item["className"][:60],
                item["text"][:60].replace("\n", " "),
                ",".join(item["keys"][:40]),
            )


def _wait(driver: DataFetcher, seconds: float) -> None:
    time.sleep(max(seconds, 0))


def _open(driver, fetcher: DataFetcher, url: str) -> None:
    driver.get(url)
    _wait(driver, fetcher.RETRY_WAIT_TIME_OFFSET_UNIT)
    try:
        WebDriverWait(driver, fetcher.DRIVER_IMPLICITY_WAIT_TIME).until(
            EC.presence_of_element_located((By.ID, "app"))
        )
    except Exception:
        pass
    _wait(driver, fetcher.RETRY_WAIT_TIME_OFFSET_UNIT)


def _choose_first_user(driver, fetcher: DataFetcher) -> None:
    try:
        fetcher._choose_current_userid(driver, 0)
        _wait(driver, fetcher.RETRY_WAIT_TIME_OFFSET_UNIT)
    except Exception as exc:
        logging.warning("[probe] 切换户号失败（忽略）: %s", exc)


def _click_first(driver, xpath: str) -> bool:
    elements = driver.find_elements(By.XPATH, xpath)
    for element in elements:
        if element.is_displayed():
            driver.execute_script("arguments[0].click();", element)
            return True
    return False


def main() -> None:
    import dotenv

    dotenv.load_dotenv(verbose=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(levelname)-8s] ---- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fetcher = DataFetcher(os.getenv("PHONE_NUMBER"), os.getenv("PASSWORD"))
    data_dir = get_data_dir()
    ErrorWatcher.init(
        root_dir=os.path.join(data_dir, "errors"),
        screenshot_dir=os.path.join(data_dir, "probe"),
    )
    driver = fetcher._get_webdriver()
    ErrorWatcher.instance().set_driver(driver)
    try:
        if not fetcher._login(driver):
            logging.error("[probe] 登录失败")
            sys.exit(1)
        logging.info("[probe] 登录成功")

        user_ids = fetcher._get_user_ids(driver)
        logging.info("[probe] 户号列表: %s", user_ids)

        _open(driver, fetcher, BALANCE_URL)
        _choose_first_user(driver, fetcher)
        _dump(driver, "01_userAcc")

        _open(driver, fetcher, PAYMENT_URL)
        _choose_first_user(driver, fetcher)
        _dump(driver, "02_paymentRecord")
        # 部分省份需要手动点击查询
        for xpath in (
            "//button[contains(normalize-space(.), '查询')]",
            "//button[contains(normalize-space(.), '查 询')]",
        ):
            if _click_first(driver, xpath):
                _wait(driver, fetcher.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
                _dump(driver, "02_paymentRecord_after_query")
                break

        _open(driver, fetcher, STEP_URL)
        _choose_first_user(driver, fetcher)
        _dump(driver, "03_step")

        _open(driver, fetcher, BILL_SUMMARY_URL)
        _choose_first_user(driver, fetcher)
        _dump(driver, "04_bill_summary")
        rows = driver.find_elements(By.XPATH, "//div[contains(@class,'billList_content')]")
        logging.info("[probe] 账单列表行数: %s", len(rows))
        if rows:
            arrow = rows[0].find_elements(By.XPATH, ".//img[contains(@class,'back_right')]")
            if arrow:
                driver.execute_script("arguments[0].click();", arrow[0])
                _wait(driver, fetcher.RETRY_WAIT_TIME_OFFSET_UNIT * 2)
                _dump(driver, "05_bill_detail")

        _open(driver, fetcher, ELECTRIC_USAGE_URL)
        _choose_first_user(driver, fetcher)
        _dump(driver, "06_usage")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
