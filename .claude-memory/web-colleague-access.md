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

## 2026-07-22 ETF·모바일 개편 (배포 대기)

- `813d78e`로 `main` 커밋·push 완료, 미니PC는 아직 미배포.
- ETF 검색 실패 원인: KRX corpList는 상장법인만 포함. KIS 공식 KOSPI 마스터의
  `EF` 그룹(실측 864 ETF)을 기존 주식 마스터와 병합하고 캐시 v2/유형 배지 추가.
- 모바일 ≤640px: 종목표를 카드형으로 바꾸고 첫 섹터만 펼침. 390px 실측
  `scrollWidth=375`로 가로 넘침 제거. 1280px 핵심 표 891px/행 59px.
- 섹터 필터·접기, 등록 설정 접기, 검색 0건 안내, 보이는 미니차트만 지연 조회.
- 독립 리뷰 후 최초 마스터 single-flight, ETF 유형 선로딩, 미니차트 재그리기,
  빈 필터/`all` 충돌, 키보드 자동완성, 실패 갱신 공유·검색 장애 상태 초기화를 보강함.
- 전체 테스트 393 passed. 승인 후 배포 시 **장 마감 후 WMI로 웹앱 재기동**.
