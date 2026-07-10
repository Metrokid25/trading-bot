# 웹 동료 공유 접근 (2026-07-09, 노트북)

- 오너 결정: 웹앱 공유 인증 = **공유 비밀번호**, 등록자 표시 = **추가(기본값 황파파)**.
- 구현(브랜치 `feature/web-colleague-access`):
  - `/api` 하위 변경 요청(POST 등) 전체를 **미들웨어**로 보호 — `X-Web-Key` 헤더 vs `.env` `WEB_SHARED_KEY`.
    미설정=전부 401(안전 기본값). **키는 영문·숫자만** — 한글 키는 브라우저가 헤더 전송 자체를 못 함(latin-1 제약).
  - 등록자: `RegisterIn.author` → `raw_input="[web:이름]"` 스탬프(스키마 변경 없음).
    `GET /api/picks`의 `registered_by` + UI 섹터 제목 "등록: 이름" 표시.
- **오너 결정(2026-07-09)**: 기존 활성 섹터에 종목 추가 시 **최초 등록자만 기록(현행 유지)** 확정.
  추가자 개별 기록(덮어쓰기/종목단위 컬럼)은 하지 않음 — "중요한 거 아니니깐".
- **배포 완료(2026-07-10, 미니PC)**: .env `WEB_SHARED_KEY` 설정(값은 미니PC .env에만) →
  웹앱 0.0.0.0:8000 상주(VBS ③라인, 재부팅 자동) → 방화벽 규칙 "trading-bot webapp (Tailscale only)"
  적용(TCP 8000, InterfaceAlias Tailscale + RemoteAddress 100.64.0.0/10 — 관리자 필요해 오너 실행) →
  Tailscale 공유·동료(chojaesng97@gmail.com) 초대 수락. 스모크 9항목 통과(401/200/registered_by/삭제/IP 바인딩).
  잔여: 동료 폰 Tailscale 앱 연결만. 상세 `HANDOFF_웹공유.md` 상단 갱신 블록.
