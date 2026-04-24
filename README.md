# KIS Trading Bot

한국투자증권 KIS Developers REST API 기반 3분봉 자동매매 봇.

## 구조
- **PortfolioAgent** — 텔레그램으로 종목/시드/비중 관리
- **AnalysisAgent** — 3분봉 RSI/볼린저밴드/이동평균으로 BUY 시그널 생성
- **ExecutionAgent** — 즉시 시장가 매수, +3/+5/+10% 분할 익절, -5% 손절

## 리스크 규칙
- 동시 매수 최대 3종목
- 하루 총 손실 -10% 도달 시 매매 중단 + 전종목 청산
- 9:00~9:10 매매 금지
- 15:10~15:20 장마감 강제 청산

## 실행
`python main.py` (Windows: `run_bot.bat` 별도 생성 가능)

## 설정
```bash
cp .env.example .env   # 키 값 채우기
pip install -r requirements.txt
python main.py
```

## 텔레그램 명령
- `/add <코드> <비중> [이름]` — 관심종목 추가
- `/remove <코드>` — 제거
- `/seed <금액>` — 시드 설정
- `/weight <코드> <비중>` — 비중 변경
- `/list`, `/status`

## 백테스트
```bash
python -m backtest.run_backtest 005930,035720 2025-01-01 2025-03-01
```

## 테스트
```bash
pytest
```

## 주의
- `.env` 파일은 절대 커밋하지 말 것 (`.gitignore` 포함됨)
- 기본값은 `KIS_ENV=PAPER` (모의투자) — 실전 전환 시 반드시 충분히 검증
