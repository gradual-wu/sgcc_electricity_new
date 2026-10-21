from unittest.mock import patch

from notify import PushplusNotify, UrlPushNotify


class _Response:
    status_code = 200


def test_pushplus_notify(monkeypatch):
    monkeypatch.setenv("BALANCE", "100")
    monkeypatch.setenv("PUSHPLUS_TOKEN", "token1,token2")
    with patch("notify.requests.get", return_value=_Response()) as mocked:
        assert PushplusNotify()("test_user", 5.0) is True
    assert mocked.call_count == 2


def test_url_notify(monkeypatch):
    monkeypatch.setenv("BALANCE", "100")
    monkeypatch.setenv("PUSH_URL", "http://127.0.0.1/notify")
    with patch("notify.requests.post", return_value=_Response()) as mocked:
        assert UrlPushNotify()("test_user", 5.0) is True
    mocked.assert_called_once()


def test_notify_skipped_when_balance_is_enough(monkeypatch):
    monkeypatch.setenv("BALANCE", "1")
    with patch("notify.requests.get") as mocked:
        assert PushplusNotify()("test_user", 5.0) is False
    mocked.assert_not_called()
