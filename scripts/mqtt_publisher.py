"""基于 paho-mqtt 的轻量发布客户端。

只负责连接、重连、发布和 LWT，不关心 Home Assistant 的实体模型。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

import paho.mqtt.client as mqtt


class MqttPublisher:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        client_id: str = "sgcc_electricity_new",
        qos: int = 1,
        retain: bool = True,
        publish_timeout_seconds: float = 10.0,
        availability_topic: str = "",
        tls: bool = False,
    ) -> None:
        self.host = (host or "").strip()
        self.port = int(port or 1883)
        self.username = (username or "").strip()
        self.password = password or ""
        self.client_id = (client_id or "sgcc_electricity_new").strip()
        self.qos = max(0, min(int(qos), 2))
        self.retain = bool(retain)
        self.publish_timeout_seconds = max(float(publish_timeout_seconds), 0.1)
        self.availability_topic = (availability_topic or "").strip()
        self.tls = bool(tls)

        self._client = None
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._last_connect_attempt = None
        self._connect_backoff_seconds = 10.0

    def enabled(self) -> bool:
        return bool(self.host)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ------------------------------------------------------------------
    # paho callbacks
    # ------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self._connected.set()
            logging.info("MQTT 已连接到 %s:%s", self.host, self.port)
            if self.availability_topic:
                client.publish(
                    self.availability_topic,
                    "online",
                    qos=self.qos,
                    retain=True,
                )
        else:
            self._connected.clear()
            logging.error(
                "MQTT 连接被拒绝（%s）：请检查 MQTT 用户名/密码或 broker 权限。",
                reason_code,
            )

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self._connected.clear()
        if reason_code != 0:
            logging.warning("MQTT 连接意外断开（%s），将在下次发布时重连。", reason_code)

    # ------------------------------------------------------------------
    # connection management
    # ------------------------------------------------------------------
    def _ensure_client(self):
        if not self.enabled():
            return None

        with self._lock:
            just_connected = False
            if self._client is None:
                client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id=self.client_id,
                )
                if self.username:
                    client.username_pw_set(self.username, self.password or None)
                if self.tls:
                    client.tls_set()
                if self.availability_topic:
                    client.will_set(
                        self.availability_topic,
                        "offline",
                        qos=self.qos,
                        retain=True,
                    )
                client.reconnect_delay_set(min_delay=1, max_delay=60)
                client.on_connect = self._on_connect
                client.on_disconnect = self._on_disconnect

                now = time.monotonic()
                if (
                    self._last_connect_attempt is not None
                    and now - self._last_connect_attempt < self._connect_backoff_seconds
                ):
                    return None
                self._last_connect_attempt = now
                try:
                    # 必须先 connect 再 loop_start：paho 的 loop_start 会立即尝试
                    # reconnect，如果此时 host 还没设置会抛出 Invalid host。
                    client.connect(self.host, self.port, keepalive=60)
                except Exception as exc:
                    logging.warning(
                        "MQTT 连接失败 %s:%s（%s），稍后重试。",
                        self.host,
                        self.port,
                        exc,
                    )
                    return None
                client.loop_start()
                self._client = client
                just_connected = True

            if self._connected.is_set():
                return self._client

            client = self._client

            # 已创建过客户端但当前未连接：限制重试等待频率，避免每个传感器都等 5 秒
            if not just_connected:
                now = time.monotonic()
                if (
                    self._last_connect_attempt is not None
                    and now - self._last_connect_attempt < self._connect_backoff_seconds
                ):
                    return None
                self._last_connect_attempt = now

        # 不要在锁内等待回调，否则 on_connect 无法执行
        if self._connected.wait(timeout=5.0):
            return client

        logging.warning(
            "MQTT 连接超时 %s:%s，请检查地址、端口和认证配置。",
            self.host,
            self.port,
        )
        return None

    def publish(self, topic: str, payload: Any, retain: Optional[bool] = None) -> bool:
        if not self.enabled():
            logging.debug("未配置 MQTT_HOST，跳过发布 %s", topic)
            return False
        if not topic:
            return False

        client = self._ensure_client()
        if client is None:
            return False

        if retain is None:
            retain = self.retain
        if isinstance(payload, (dict, list, tuple)):
            payload_text = json.dumps(payload, ensure_ascii=False)
        elif isinstance(payload, bool):
            payload_text = "true" if payload else "false"
        else:
            payload_text = str(payload)

        try:
            info = client.publish(topic, payload_text, qos=self.qos, retain=retain)
            info.wait_for_publish(timeout=self.publish_timeout_seconds)
            if not info.is_published():
                self._connected.clear()
                logging.warning("MQTT 发布超时: %s", topic)
                return False
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                self._connected.clear()
                logging.warning(
                    "MQTT 发布失败: %s (%s)",
                    topic,
                    mqtt.error_string(info.rc),
                )
                return False
            logging.debug("MQTT 已发布 %s: %s", topic, payload_text[:200])
            return True
        except Exception as exc:
            self._connected.clear()
            logging.warning("MQTT 发布异常 %s: %s", topic, exc)
            return False

    def publish_availability(self, online: bool) -> None:
        if not self.availability_topic:
            return
        self.publish(
            self.availability_topic,
            "online" if online else "offline",
            retain=True,
        )

    def close(self, publish_offline: bool = True) -> None:
        client = self._client
        if client is None:
            return
        try:
            if publish_offline and self._connected.is_set() and self.availability_topic:
                try:
                    client.publish(
                        self.availability_topic,
                        "offline",
                        qos=self.qos,
                        retain=True,
                    ).wait_for_publish(timeout=2.0)
                except Exception:
                    pass
            client.disconnect()
            client.loop_stop()
        finally:
            self._client = None
            self._connected.clear()
