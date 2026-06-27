---
name: laptop-run-env
description: 노트북에서 트레이딩봇 실행 시 venv/인코딩 주의점
metadata: 
  node_type: memory
  type: project
  originSessionId: 9ccc5d3b-07ea-40eb-acd9-25c20010d3fa
---

이 노트북에서는 시스템 기본 `python`이 3.12인데 프로젝트 `.venv`는 Python 3.14다. 그냥 `python`을 쓰면 venv가 아닌 3.12로 잡히므로, 봇/테스트/스크립트 실행은 반드시 `./.venv/Scripts/python.exe`로 해야 한다.

Windows 콘솔에서 한글/이모지 출력이 cp949로 깨지므로 출력이 있는 스크립트는 `PYTHONIOENCODING=utf-8`을 앞에 붙여 실행한다.

**Why:** venv는 다른 PC에서 만들어져 동기화된 것이라 시스템 python 버전과 다르다. 잘못된 인터프리터로 실행하면 의존성 누락 에러가 난다.
**How to apply:** `PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe main.py` 형태로 실행. 관련 [[trading-bot-secrets-setup]].

로컬 웹 대시보드(FastAPI, 토스 스타일 종목 등록/삭제)는 `webapp/`에 있고 다음으로 실행: `./.venv/Scripts/python.exe -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000` → 브라우저 http://127.0.0.1:8000 . fastapi/uvicorn/pandas_market_calendars 의존성은 2026-06-27 노트북 venv에 설치 완료.
