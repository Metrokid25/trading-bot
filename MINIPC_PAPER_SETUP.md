# 미니PC 모의투자(paper) 셋업 가이드 — Claude Code 용 자체완결 문서

> 이 문서는 **미니PC에서 처음 여는 Claude Code 세션**이 사전 맥락 없이 읽고
> 셋업 → 검증 → 상주 가동까지 완료할 수 있도록 쓰였다. 2026-07-05 작성.

---

## 1. 왜 이 작업을 하는가 (맥락 요약)

- trading-bot 은 멘토 매매철학 기반 KOSPI/KOSDAQ 자동매매 봇. 현재 실매매 아님.
- 백테스트 결론: **알파 미입증** (gm_v3 비용후 양의 기대값이나 동일 유니버스
  buy&hold +116% 에 미달). 합의: **forward(모의투자)만이 답**.
- 그래서 매 거래일 **생존 3전략 + 벤치마크**를 자동 기록하는 페이퍼 하네스를
  구축했고(`strategy/paper_runner.py`, 커밋 798e644), **상시 가동 지정 기기를
  미니PC로 결정**했다 (노트북은 이동용이라 상주 부적합).
- **최종 관문**: paper_start 부터 최소 **20거래일** 후 "벤치마크(동일 유니버스
  동일가중 B&H) 대비 초과수익 존재 여부" 판정. 알파 없음도 유효한 결론.

### 절대 규칙 (운영 헌장 발췌 — 위반 금지)
1. 실전 주문 경로는 오너 명시 승인 없이 활성화 금지. 페이퍼까지만.
2. 성과는 **벤치마크 대비 초과수익으로만** 판단. 절대손익 단독 보고 금지.
3. **paper.db 는 이 미니PC 한 대에서만 기록** (지정 1대 원칙). 노트북/PC 에서
   `paper_runner` 기록 실행 금지 — 원장이 갈라진다.
   (노트북에 paper.db 가 초기화돼 있었으나 기록 0건 상태로 폐기됨 — 무시.)
4. 이 봇은 자기 소유 DB(paper.db, toss_candles.db, trading.db)에만 쓴다.
5. 커밋/푸시는 사람 승인 후에만.

---

## 2. 셋업 절차 (순서대로)

### 2-1. 리포 + 파이썬
```powershell
git clone https://github.com/Metrokid25/trading-bot.git C:\trading-bot
cd C:\trading-bot
python -m venv .venv          # Python 3.12+ (노트북은 3.14 사용 중)
.venv\Scripts\python.exe -m pip install -r requirements.txt
```
- Windows 콘솔 한글 깨짐 방지: 실행 시 `PYTHONIOENCODING=utf-8` (아래 명령들 포함됨).

### 2-2. .env (git 으로 안 옮겨짐 — 노트북에서 파일 복사)
노트북 `C:\trading-bot\.env` 를 미니PC 같은 위치로 복사. 페이퍼에 필요한 키:
- `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` — 토스 캔들(핵심 데이터 소스)
- `KIS_REAL_APP_KEY/SECRET` — gm_v3 워밍업용 과거 일봉 보충(읽기 전용)
- **`TOSS_PROXY` 는 빈 값으로** — 미니PC는 집 고정 회선 직결이라 터널 불필요.
  (노트북 .env 에는 `TOSS_PROXY=socks5://127.0.0.1:1080` 이 있음 — 복사 후 지울 것)

### 2-3. 토스 IP 등록 (오너가 브라우저에서)
```powershell
curl.exe -s https://api.ipify.org   # 미니PC 공인 IP 확인
```
→ 이 IP를 토스 개발자센터 허용 목록에 **추가** 등록 (기존 AWS IP 43.203.43.96 은
노트북 개발용으로 유지). 집 회선 IP는 보통 수 주~수 개월 유지되나, 바뀌면
`403 {"error":"access_denied","error_description":"IP address not allowed"}` 가
나므로 그때 재등록.

### 2-4. trading.db 스키마 (선택이지만 권장)
```powershell
$env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python.exe scripts\migrations\migration_runner.py
```
- 페이퍼 자체는 trading.db 를 안 쓰지만(유니버스는 `universe_snapshot.json`),
  `main_tracker.py` 의 수집 파이프라인 단계가 스키마 없으면 에러 로그를 뿜는다
  (best-effort 라 페이퍼 기록은 계속됨). 깔끔하게 하려면 1회 실행.

### 2-5. 토스 연결 검증
```powershell
$env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python.exe scripts\probe_toss_candles.py --pages 1
```
기대 출력: `[auth] access_token 획득` + 봉 200개. 403 이면 2-3 재확인.

### 2-6. 페이퍼 초기화 (기록 시작점 설정, 1회)
```powershell
$env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python.exe -m strategy.paper_runner --init 2026-07-06
```
- **paper_start = 2026-07-06 (월)** 로 확정된 값. 다른 날짜로 바꾸지 말 것.
- 만약 셋업이 7-06 이후에 끝났다면: init 은 그대로 2026-07-06 으로 하고,
  놓친 거래일을 오래된 날부터 순서대로 소급 기록:
  `... -m strategy.paper_runner --day 2026-07-06` → `--day 2026-07-07` → ...
  (토스가 historical 을 주므로 소급해도 데이터 동일. 단 **순서대로만** — 러너가
  역순 기록을 거부한다.)

---

## 3. 상주 가동 (매일 자동)

### 방식 A — main_tracker 상주 (권장)
```powershell
$env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python.exe main_tracker.py
```
- 매일 16:00 KST: 수집 파이프라인(best-effort) → **페이퍼 기록** 자동 실행.
- 미니PC 절전 금지(전원 옵션에서 꺼둠). 재부팅 시 자동 시작하려면 작업
  스케줄러에 "로그온 시" 위 명령 등록.

### 방식 B — 작업 스케줄러로 페이퍼만
매 거래일 20:10 에 `-m strategy.paper_runner` 실행 등록 (20:05 이후면 애프터
포함 완전한 하루가 캐시에 확정됨).

### 일상 확인
```powershell
$env:PYTHONIOENCODING='utf-8'; .venv\Scripts\python.exe -m strategy.paper_runner --report
```
- 매 거래일 `v2 / v2_leader / gm_v3 / bench_bh` 4행 + 벤치마크 대비 초과수익(%p).
- 하루 누락 시: 다음날 `--day YYYY-MM-DD` 로 소급 (순서대로만).

---

## 4. 하네스가 기록하는 것 (이해용)

| strategy | 내용 |
|---|---|
| v2 | 프리장(NXT 08:00~08:50) 급등 → 눌림 지지·다지기 → 아침고점 재돌파 진입, 5분할 익절+본절보호+손절 -4% (당일 스캘핑, 3분봉) |
| v2_leader | v2 + 주도섹터 필터(신호일 d-1 기준 최근 5거래일 수익률 1위 섹터만) |
| gm_v3 | 멘토 룰엔진 R1~R12 (일봉 스윙, 다음날 시가 체결, 매일 전체 리플레이=멱등) |
| bench_bh | **동결 유니버스**(universe_snapshot.json, 9섹터 50종목) 동일가중 buy&hold — 알파 판정 기준선 |

명시 가정(paper_meta 에 스탬프됨): 비용 0.25%/편도, gm_v3 미청산 포지션은
MTM(EOR)로 equity 반영·실청산 집계 제외, 벤치마크 첫날 무봉 종목 영구 제외,
애프터 급변/프리장 갭 규칙 미반영(보수적).

데이터 흐름: 토스 Open API 1분봉(과거 1년+, NXT 프리장 실체결 포함 — KIS/
트뷰/크레온은 불가한 유일 소스) → `db/toss_candles.db` 캐시(증분) → 전략 판정
→ `db/paper.db`(WAL) 기록. KIS 는 gm_v3 워밍업 일봉 보충(읽기 전용)만.

---

## 5. 트러블슈팅

| 증상 | 원인/조치 |
|---|---|
| 403 "IP address not allowed" | 집 IP 변경 → 토스에 새 IP 등록 (2-3) |
| "day < 마지막 기록일 — 소급 기록 불가" | 날짜 역순 실행 — 오래된 날부터 순서대로 |
| "TOSS_CLIENT_ID ... 비어 있음" | .env 미복사/오타 (2-2) |
| gm_v3 "일봉 부족으로 제외" 경고 | KIS 키 확인 (워밍업 보충 실패) — 며칠 지나면 자체 데이터로 해소 |
| 16:00 실행분 주의 | 20:05 전 수집은 '미완료'로 취급되어 다음 실행 때 자동 재수집 (설계된 동작) |

## 6. 기기 역할 정리 (2026-07-05 기준)

| 기기 | 역할 |
|---|---|
| **미니PC** | **모의투자 지정 기기** (paper.db 원장, 상주 가동) — 이 문서 |
| 노트북 | 개발/백테스트 (이동용). 회사 테더링에서 토스 쓸 땐 AWS 터널(`TOSS_PROXY`) |
| PC(데스크탑) | 백테스트 보조. 유니버스 정합 시 `sync_universe.py --import` 필요 |
| AWS EC2 (43.203.43.96) | 노트북용 고정 IP 출구 (서울, t3.micro, 크레딧 차감) |
