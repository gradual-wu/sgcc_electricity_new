"""二维码解码与重新生成。

只在本机处理，不把二维码内容（登录 token）发送到任何第三方服务。
"""

from __future__ import annotations

import io
import logging
from typing import Optional


def decode_qr_image(image_bytes: bytes) -> Optional[str]:
    """从 PNG/JPG 字节中识别二维码内容。"""
    if not image_bytes:
        return None
    try:
        import cv2
        import numpy as np

        array = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None:
            return None

        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(image)
        if data:
            return data

        # 截图里可能带背景/边框，再尝试灰度 + OTSU 二值化
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, threshold = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        data, _, _ = detector.detectAndDecode(threshold)
        return data or None
    except Exception as exc:
        logging.warning("二维码解码失败: %s", exc)
        return None


def generate_qr_png(text: str) -> Optional[bytes]:
    """把字符串重新生成干净的二维码 PNG。"""
    if not text:
        return None
    try:
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )
        qr.add_data(text)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception as exc:
        logging.warning("二维码重新生成失败: %s", exc)
        return None
