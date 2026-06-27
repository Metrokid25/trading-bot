---
name: data-accumulation-machine
description: 트레이딩봇 데이터 적립은 노트북에서 한다 (PC 아님)
metadata:
  type: project
---

트레이딩봇의 Phase 2.5 데이터 적립(일봉/분봉 수집)은 **노트북**에서 돌리기로 결정 (2026-06-27). PC를 매일 켜둘 수 없어서. 따라서 노트북의 `db/trading.db`가 정식 누적 DB가 된다.

**Why:** KIS 분봉은 당일만 제공돼 소멸성 → 매 거래일 장중~장마감에 켜둔 기기에서 수집해야 하고, 그 기기를 노트북으로 정함. PC에 5월부터 쌓였을 수 있는 데이터는 접근 불가 + db/ gitignore라 노트북으로 못 가져옴(일봉은 과거 backfill 가능, 분봉은 불가).
**How to apply:** 평일 장중 노트북을 켜두고 수집기를 돌린다. db/는 gitignore라 git 동기화 안 됨 — 노트북이 단일 소스. 수집 파이프라인 조립 현황은 [[trading-bot-purpose]] 참고.
