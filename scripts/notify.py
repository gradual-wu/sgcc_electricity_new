import typing as typ
import os
import logging
import time
import requests
import io

class PushplusNotify(typ.NamedTuple):

    def __call__(self, user_id, balance):
        BALANCE = float(os.getenv("BALANCE", 10.0))
        logging.info(f"检查电费余额。当余额低于 {BALANCE} 元时，将发送通知")
        if balance < BALANCE :
            PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN").split(",")
            success = False
            for token in PUSHPLUS_TOKEN:
                title = "电费余额不足提醒"
                content = (f"您用户号{user_id}的当前电费余额为：{balance}元，请及时充值。" )
                url = ("http://www.pushplus.plus/send?token="+ token+ "&title="+ title+ "&content="+ content)
                resp = requests.get(url)
                success = resp.status_code == 200 or success
                logging.info(
                    f"用户 {user_id} 当前余额 {balance} 元低于 {BALANCE} 元，已发送通知，请注意查收并及时充值。"
                )
            return success
        return False

class UrlPushNotify(typ.NamedTuple):

    def __call__(self, user_id, balance):
        BALANCE = float(os.getenv("BALANCE", 10.0))
        logging.info(f"检查电费余额。当余额低于 {BALANCE} 元时，将发送通知")
        if balance < BALANCE :
            url = os.getenv("PUSH_URL")
            full_url = f"{url}"
            resp = requests.post(full_url, json={"user_id": user_id, "balance": balance})
            logging.info(
                f"用户 {user_id} 当前余额 {balance} 元低于 {BALANCE} 元，已发送通知，请注意查收并及时充值。"
            )
            return resp.status_code == 200
        return False

class UrlLoginQrCodeNotify(typ.NamedTuple):

    def __call__(self, qrcode: bytes, reason: str) -> bool:
        sent = False

        # 方式1：自定义 URL 推送（保留兼容）
        url = os.getenv("PUSH_QRCODE_URL")
        if url:
            try:
                files = {'file': ("qrcode.png", io.BytesIO(qrcode), 'image/png')}
                resp = requests.post(url, files=files, data={"reason": reason}, timeout=15)
                logging.info(f"推送二维码到自定义URL，状态码: {resp.status_code}")
                sent = resp.status_code == 200
            except Exception as e:
                logging.warning(f"自定义URL推送失败: {e}")

        # 方式2：HA 持久通知（图片内嵌，利用现有的 HASS_URL + HASS_TOKEN）
        hass_url = os.getenv("HASS_URL", "").rstrip("/")
        hass_token = os.getenv("HASS_TOKEN", "")
        if hass_url and hass_token:
            try:
                ts = int(time.time())
                image_markdown = ""
                try:
                    # 保存图片到 HA 的 www 目录，使其可通过 {HASS_URL}/local/ 访问
                    www_dir = os.getenv("HASS_WWW_DIR", "/config/www")
                    os.makedirs(www_dir, exist_ok=True)
                    qr_path = os.path.join(www_dir, "sgcc_login_qr.png")
                    with open(qr_path, "wb") as f:
                        f.write(qrcode)
                    logging.info(f"二维码已保存到 {qr_path}")
                    image_markdown = f"![登录二维码](/local/sgcc_login_qr.png?t={ts})"
                except Exception as e:
                    logging.warning(f"保存二维码到 HA www 目录失败: {e}")
                    image_markdown = "二维码已保存到 /data/login_qr_code.png，请在容器内查看。"

                message = (
                    f"**触发原因**: {reason}\n\n"
                    f"请用**国家电网 App** 扫描下方二维码登录，"
                    f"二维码将在约 3 分钟内过期：\n\n"
                    f"{image_markdown}"
                )
                headers = {
                    "Authorization": f"Bearer {hass_token}",
                    "Content-Type": "application/json",
                }
                resp = requests.post(
                    f"{hass_url}/api/services/persistent_notification/create",
                    headers=headers,
                    json={
                        "title": "⚡ 国网需要扫码登录",
                        "message": message,
                        "notification_id": "sgcc_login_qrcode",
                    },
                    timeout=10,
                )
                if resp.status_code in (200, 201):
                    logging.info("✅ 已通过 HA 持久通知推送登录二维码，请打开 HA 界面查看通知（左下角铃铛）")
                    sent = True
                else:
                    logging.warning(f"HA 通知发送失败，状态码: {resp.status_code}，响应: {resp.text[:200]}")
            except Exception as e:
                logging.warning(f"HA 通知发送异常: {e}")
        else:
            logging.warning("HASS_URL 或 HASS_TOKEN 未配置，跳过 HA 通知")

        if not sent:
            logging.warning("所有推送方式均失败，二维码已保存到 /data/login_qr_code.png")
        return sent
