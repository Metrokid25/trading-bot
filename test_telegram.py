import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")

if not token or not chat_id:
    print("ERROR: TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 .env에 없습니다.")
    sys.exit(1)

url = f"https://api.telegram.org/bot{token}/sendMessage"
resp = requests.post(url, data={"chat_id": chat_id, "text": "트레이딩봇 연결 테스트 성공!"}, timeout=10)

print(f"Status: {resp.status_code}")
print(resp.text)
resp.raise_for_status()
