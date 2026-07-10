# HANDOFF — 동료 웹 접속 시스템화

> 목적: 신뢰하는 동료들이 각자 기기에서 **미니PC 웹앱(Tailscale `100.100.141.24:8000`)**에
> 접속해 스승님 픽을 등록할 수 있게 만든다.
> 이 문서 하나로 노트북 담당자가 바로 착수할 수 있도록, **모든 항목을 실제 코드·git·
> 실행결과로 재검증**해 기록했다. 추측 없음. 미확인 항목은 명시.
> 작성 시점 기준일: 2026-07-09 (미니PC에서 검증).

> **[갱신 2026-07-10, 미니PC] 배포 완료 ✅ — 아래 [3](a)~(d) 전부 실행됨.** 실측 결과:
> - **(a) 상주**: 웹앱 `0.0.0.0:8000` 가동 중(`Get-NetTCPConnection` 확인). 시작프로그램 VBS
>   (`trading-bot-paper.vbs`)에 ③웹앱 라인 포함 — 재부팅 자동 기동. 이제 미니PC 상주 = 3프로세스
>   (main_tracker / paper_runner --market-schedule / uvicorn webapp).
> - **(사전작업) `.env`에 `WEB_SHARED_KEY` 설정 완료** — 키 값은 오너·미니PC `.env`에만
>   (문서에 평문 기록 안 함). settings 로드 검증됨(ascii, len 6).
> - **(b) 방화벽**: 규칙 `trading-bot webapp (Tailscale only)` **적용·Enabled 확인** —
>   Inbound Allow TCP 8000, `-InterfaceAlias Tailscale` + `-RemoteAddress 100.64.0.0/10`
>   3중 스코핑(오너가 관리자 PowerShell로 실행). Wi-Fi/인터넷 쪽 8000은 기본 Block 유지.
> - **(c) Tailscale 공유**: 오너가 admin console에서 동료 계정(chojaesng97@gmail.com) 초대,
>   **수락 확인됨**. ⚠️ 동료 폰에 Tailscale 앱 설치·로그인·토글 ON이 **아직 미완**(마지막 단계)
>   — 완료되면 폰 브라우저에서 `http://100.100.141.24:8000` 바로 접속 가능.
> - **(d) 기능 검증(스모크 9항목 전부 통과, 2026-07-10 실측)**: GET / 200(무키) / 등록·삭제
>   무키·오키 **401** / 정키 등록 200 + `registered_by` 표시 / 삭제 200 / 삭제 후 잔존 0 /
>   Tailscale IP 직접 접속 200. 테스트 섹터는 즉시 삭제 — paper 유니버스 로그 오염 0 확인.
> - **남은 것**: 동료 폰 앱 연결(위 c) + 첫 실접속 확인뿐. 서버 측 작업 없음.

> **[갱신 2026-07-09, 노트북]** [3]의 미결정 2건은 오너가 결정, 노트북에서 구현 완료
> (브랜치 `feature/web-colleague-access`): ① 공유 비밀번호 = `.env`의 `WEB_SHARED_KEY`
> (영문·숫자만, 미설정 시 등록·삭제 전부 401) — `/api` 변경 요청 전체를 미들웨어로 보호,
> ② 등록자 스탬프 `[web:이름]`(기본 황파파) + `GET /api/picks`의 `registered_by`로 UI 표시.
> 따라서 아래 [2]의 "무인증·[web] 하드코딩" 서술과 라인번호는 **작성 시점(e57db70) 기준**이다.
> 배포 절차 [3](a)~(d)는 그대로 유효하며, (a) 전에 **미니PC .env에 WEB_SHARED_KEY 설정**이 추가로 필요.

---

## [1] 저장소 현재 상태 (git 실측)

| 항목 | 값 |
|---|---|
| 현재 브랜치 | `main` |
| HEAD | `e57db70` — feat(paper): 초과수익에 절대수익 병기 + 손실회피 태그 (2026-07-08 15:26) |
| origin/main 대비 | **ahead 0 / behind 0** (완전 동기) |
| uncommit 변경 | `data/daily_data.py`·`flow_data.py`·`sector_models.py`·`stock_master.py` 4개 — **내용 변경 아니라 LF↔CRLF 줄바꿈 차이뿐**(`git diff --numstat` 비어 있음). 무시 가능. |

최근 커밋 5개:
```
e57db70 feat(paper): 초과수익에 절대수익 병기 + 손실회피 태그
7096662 fix(paper): 팩트 알림 독립 리뷰 반영 (재시도 폭풍·블로킹·취약성 정리)
cceb367 feat(paper): 텔레그램 팩트 알림 배선 (1단계)
4eed808 fix(paper): 상주 루프 파일 로그 싱크 추가 - hidden 프로세스 로그 소실 방지
625a140 feat(paper): 운영 전환 A안 — 라이브 유니버스 + 주도주 필터 상주 + 독립 리뷰 10건 반영
```

---

## [2] 웹앱 현황 (코드로 재검증 — 라인번호 포함)

파일: `webapp/server.py` (총 **379줄**, LF 기준 `wc -l`).

### 2-1. 실행 방식 — 순수 ASGI 앱, host/port는 CLI 인자
- `webapp/server.py`에 `uvicorn.run` / `if __name__ == "__main__"` **없음**. 파일은 `app.mount(...)`
  (`server.py:379`)로 끝나는 **순수 ASGI 앱**. → host/port는 **코드가 아니라 실행 명령이 정한다.**
- 문서화된 실행 명령 `webapp/server.py:8`:
  ```
  .venv/Scripts/python.exe -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000
  ```
- `.claude/launch.json` **없음**. 시작프로그램 VBS(`...\Startup\trading-bot-paper.vbs`)에는
  **웹앱 미포함** — 현재 상주는 `main_tracker.py` + `python -m strategy.paper_runner --market-schedule`
  둘뿐. **→ 웹앱은 수동 기동 대상.**
- **실측: 현재 웹앱 미기동** (8000 포트 리슨 없음, uvicorn 프로세스 0개).
- **결론: 지금 상태로는 `127.0.0.1`(localhost) 바인딩이라, 웹앱을 띄워도 미니PC 외부(=Tailscale 포함)에서 접속 불가.**

### 2-2. 인증 — 전무
- `webapp/server.py`에 `add_middleware`/`api_key`/`authorization`/`password`/`login`/`@app.middleware`
  **0건**(grep 확인). `Depends(...)`는 전부 의존성 주입(`get_store`/`get_master`/`get_kis`/`get_http`)일 뿐 인증 아님.
- 무인증 라우트:
  - `server.py:302` `@app.post("/api/picks")` — 섹터+종목 등록
  - `server.py:349` `@app.post("/api/picks/remove-stock")` — 종목 삭제
  - `server.py:361` `@app.post("/api/picks/remove-sector")` — 섹터 삭제
- **결론: 접근 가능한 누구나 등록·삭제 가능. 현재 유일한 접근제어 = localhost 바인딩뿐.**

### 2-3. 등록자 추적 — 없음 (단, raw_input 재활용 가능)
- 요청 모델 `server.py:83` `class RegisterIn` = `sector_name`, `pick_date`, `stocks`뿐 — **author 필드 없음.**
- `server.py:336`: `raw_input="[web]"` **하드코딩**.
- DB 스키마에 등록자 컬럼 없음:
  - `sector_stocks` (author/user 컬럼 없음)
  - `sector_pick_events` (`scripts/migrations/m001_phase25_tracking.py:57` — `event_id, sector_name,
    registered_at_kst, is_sector_repick, prev_event_id, days_since_last_sector_pick,
    total_sector_pick_count`; author/user **없음**)
- `sector_picks.raw_input`이 **유일한 자유텍스트 필드**(현재 "[web]"). → **스키마 변경 없이** 여기에
  등록자를 스탬프 가능(예: `RegisterIn`에 `author` 추가 → `raw_input=f"[web:{author}]"`).
  종목 단위 정밀 추적을 원하면 새 컬럼 필요.

### 2-4. 재검증 결과 — 기존 조사와 달라진 점
**웹앱 관련 사실(2-1~2-3)은 기존 조사와 동일, 변동 없음.** (repo HEAD만 그새 `e57db70`로 전진 —
알파 라벨링 커밋이 main에 병합됨. 웹앱 코드 자체는 무변경.)

---

## [3] 앞으로 할 작업 (노트북 담당자 수행) — 목표·순서만. 지금 구현 금지

**목표:** 동료들이 각자 기기에서 미니PC 웹앱 `http://100.100.141.24:8000` 에 접속해 픽 등록.

**작업 순서:**
- **(a)** 웹앱을 `0.0.0.0`으로 **상주 기동** — 시작프로그램 VBS에 uvicorn 라인 추가:
  ```
  sh.Run "...\.venv\Scripts\python.exe -m uvicorn webapp.server:app --host 0.0.0.0 --port 8000", 0, False
  ```
  (코드 수정 0줄 — 실행 인자만 `127.0.0.1`→`0.0.0.0`)
- **(b)** Windows 방화벽 **8000 인바운드 허용** — **Tailscale 인터페이스 한정 권장**.
  ⚠️ **현재 방화벽 규칙 상태 미확인** — 미니PC에서 확인 후 규칙 추가 필요할 가능성 높음.
- **(c)** Tailscale에서 미니PC **공유(Share)** — 동료 tailnet에 노출.
- **(d)** 동료 기기에서 `http://100.100.141.24:8000` 접속·등록 **테스트**.

**미결정 사항 2개 (노트북 세션에서 오너가 결정 예정 — 지금 구현하지 말 것):**
1. **인증**: Tailscale 신뢰만으로 갈지 vs 최소 인증(공유 비밀번호/키) 추가할지.
   (붙이려면 코드 작업 — 현재 무인증)
2. **등록자 표시**: `RegisterIn`에 `author` 추가 → `raw_input`에 `[web:이름]` 스탬프.
   **스키마 변경 없음**(2-3 참고). 넣을지 말지 결정 대기.

---

## [4] 주의·운영 제약 (반드시 인지)

- **기기 경계**: `load_universe()`(`strategy/paper_runner.py:189`)가 `settings.DB_PATH`(=미니PC 로컬
  `db/trading.db`)를 읽는다. → **픽 등록은 미니PC 웹앱에서 한 것만 모의투자에 반영된다.**
  `db/trading.db`는 **gitignore라 기기 간 동기화 안 됨** — 노트북/다른 기기 웹앱에 등록해도 안 넘어온다.
  **∴ 동료 접속도 반드시 "미니PC에서 도는 웹앱"에 붙어야 한다** (그래서 0.0.0.0 상주화가 미니PC 작업).
- **상주 재시작**: 미니PC에는 `main_tracker` + `paper_runner --market-schedule`가 상주 중.
  웹앱 상주 추가/코드 변경 반영엔 재시작이 필요할 수 있음. **장중(08:00~16:00) 재시작은 피할 것**
  (paper 기록 사이클·수집에 영향).
- **보안**: host를 `0.0.0.0`으로 여는 것 = **tailnet 내 무인증 노출**. 인증 결정([3] 미결정 1) 전까지는
  "tailnet = 신뢰 동료만"이라는 전제를 인지하고 진행. tailnet 밖(공용 인터넷)엔 절대 노출 금지.
- **실주문 무관**: 웹앱은 종목 등록/검색만 한다(`server.py:3-5`). KIS 주문·텔레그램 발송 경로 건드리지 않음.

---

## [5] 노트북 담당자 시작 안내 (복붙용)

> **역할 분담**: 노트북에서는 **코드 작업만**(예: 인증/author 스탬프 구현·테스트). 실제 배포
> (0.0.0.0 상주화·방화벽·Tailscale 공유)는 **미니PC에서** 해야 한다([4] 기기 경계 참고).

**저장소 최신화 (노트북):**
```powershell
cd C:\trading-bot
git fetch origin
git status                 # 'behind'면 아래 pull 먼저 (pull 없이 작업 시작 금지)
git checkout main
git pull origin main
git branch --show-current  # main 확인
```

**웹앱 로컬 실행 (노트북 — 코드 테스트용, localhost):**
```powershell
# 노트북 venv는 Python 3.14 (CLAUDE.md §4). 경로: .\.venv\Scripts\python.exe
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000
# 브라우저: http://127.0.0.1:8000
```
- 노트북에서 `--host 0.0.0.0`으로 열어도 되지만, **노트북 trading.db는 미니PC와 별개**라 여기 등록분은
  모의투자에 반영 안 됨([4]). 노트북은 **코드 검증 전용**, 실제 동료 접속은 미니PC 배포로.

**작업 브랜치 만들기 (main 직접 커밋 금지, force push 금지 — CLAUDE.md §1·§2):**
```powershell
git checkout -b feature/web-colleague-access
# 작업 → 커밋 전 독립 리뷰(/code-review) → 커밋 → push → 오너 승인 후 머지
```

---

### 참고 문서 (저장소 내)
- `CLAUDE.md` — 기기 동기화·커밋 전 리뷰·실행환경 표준 규칙
- `PROJECT_HANDOFF.md` — 프로젝트 전체 현황(모의투자 운영 전환 A안 등)
- `MINIPC_PAPER_SETUP.md` — 미니PC 상주 셋업(웹앱은 여기 미포함 — 이번 작업으로 추가)
- `.claude-memory/` — 누적 의사결정(인덱스 `MEMORY.md`)
