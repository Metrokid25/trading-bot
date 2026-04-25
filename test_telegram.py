from __future__ import annotations

import os
import sys
from typing import Any

import pytest
import requests
from dotenv import load_dotenv


def send_telegram_test_message(
    token: str,
    chat_id: str,
    text: str = "트레이딩봇 연결 테스트 성공!",
    post: Any = requests.post,
) -> requests.Response:
    """Send a Telegram smoke-test message.

    This helper is intentionally not called at import time. Normal pytest runs
    use a mocked post function; live Telegram calls are opt-in only.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
    resp.raise_for_status()
    return resp


def test_send_telegram_test_message_uses_expected_request() -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200
        text = '{"ok": true}'

        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    resp = send_telegram_test_message(
        token="123456:TEST",
        chat_id="987654321",
        text="hello",
        post=fake_post,
    )

    assert resp.status_code == 200
    assert calls == [
        {
            "url": "https://api.telegram.org/bot123456:TEST/sendMessage",
            "data": {"chat_id": "987654321", "text": "hello"},
            "timeout": 10,
        }
    ]


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_TELEGRAM_TEST") != "1",
    reason="live Telegram smoke test is opt-in; set RUN_LIVE_TELEGRAM_TEST=1",
)
def test_live_telegram_smoke() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        pytest.skip("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured")

    resp = send_telegram_test_message(token, chat_id)
    assert resp.status_code == 200


def main() -> int:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 .env에 없습니다.")
        return 1

    resp = send_telegram_test_message(token, chat_id)
    print(f"Status: {resp.status_code}")
    print(resp.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
