"""페이퍼 팩트 알림 — 텔레그램 sendMessage 경량 발송 (폴링 없음).

paper_runner.record_day 가 확정 기록(finalized=1) 시 호출한다. main.py 의
TelegramBot(폴링)과 달리 Application 을 띄우지 않고 Bot API 를 httpx 로 직접
POST 하므로 폴링 소비자 충돌이 없다(발송 전용).

설계:
- **finalized 에만 발송**: 페이퍼는 일일 리플레이라 장중 부분데이터에서는 청산가/
  수익률이 유동적이다. 20:05 이후 확정분에서만 알려 팩트를 보장한다(장중 실시간
  진입 알림은 라이브 인트라데이 엔진이 필요 — 이번 범위 아님).
- **중복 차단**: 이벤트 key 를 paper.db `paper_notified` 에 커밋 후 재발송 금지.
  5분 재기록/재시작에도 하루 1회만 나간다.
- **일일 상한**: 개별 트레이드 알림은 DAILY_CAP 개까지. 요약은 항상 발송.
- 봇/채널 = settings.TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (트레이딩봇 전용
  @zzapmoneying_bot — ai-moneyingbot RAG 와 분리).
"""
from __future__ import annotations

import sqlite3

import httpx
from loguru import logger

from config import settings
from core.time_utils import now_kst, to_db_iso

DAILY_CAP = 40   # 하루 개별 트레이드 알림 상한 (요약 제외)


def _send(text: str) -> bool:
    tok = settings.TELEGRAM_BOT_TOKEN
    chat = settings.TELEGRAM_CHAT_ID
    if not tok or ":" not in tok or "your_bot_token" in tok.lower() or not chat:
        logger.warning("[paper][tg] 토큰/채널 미설정 — 알림 스킵")
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=10.0,
        )
        if r.status_code == 200:
            return True
        logger.error("[paper][tg] 발송 실패 HTTP {}: {}", r.status_code, r.text[:200])
        return False
    except Exception as exc:
        logger.error("[paper][tg] 발송 예외: {}", exc)
        return False


def _reason_kr(reason: str) -> str:
    r = (reason or "").upper()
    if "SL" in r:
        return "🛑손절"
    if "TP" in r:
        return "🎯익절"
    return "⏹정리"


def _fmt_trade(day, r: dict) -> str:
    entry, exit_ = int(r.get("entry", 0)), int(r.get("exit", 0))
    return (
        f"🟢 진입·청산 [주도 {r['sector']}] {r['name']}({r['code']})\n"
        f"진입 {entry:,} → 청산 {exit_:,}  ({_reason_kr(r.get('reason', ''))}, "
        f"{r['ret_net']:+.2%})\n전략 v2_leader · {day}"
    )


def _fmt_gm3_exit(r: dict) -> str:
    return (
        f"📕 gm_v3 청산 {r.get('name') or r['code']}({r['code']})\n"
        f"{r['ret_net']:+.2%} · {r.get('detail') or ''}\n청산일 {r.get('closed_on')}"
    )


def _fmt_summary(day, finalized: int, summary: dict) -> str:
    lead = summary.get("v2_leader", {})
    gm3 = summary.get("gm_v3", {})
    bench = summary.get("bench_bh", {})
    alpha = lead.get("alpha_vs_bench", 0.0)
    tag = "(확정)" if finalized else "(잠정)"
    return (
        f"📊 페이퍼 마감 {day} {tag}\n"
        f"주도주 v2_leader: {lead.get('trades', 0)}종목 진입, "
        f"당일 {lead.get('day_ret', 0.0):+.2%}\n"
        f"gm_v3: 청산 {gm3.get('closed_today', 0)}종목 "
        f"(보유 {gm3.get('open_positions', 0)})\n"
        f"벤치 {bench.get('day_ret', 0.0):+.2%} · 알파 {alpha:+.2%}p"
    )


def _already_sent(con: sqlite3.Connection, key: str) -> bool:
    return con.execute(
        "SELECT 1 FROM paper_notified WHERE key=?", (key,)
    ).fetchone() is not None


def _mark_sent(con: sqlite3.Connection, key: str, day, kind: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO paper_notified(key, day, kind, sent_at) VALUES (?,?,?,?)",
        (key, day.isoformat(), kind, to_db_iso(now_kst())),
    )
    con.commit()   # 즉시 커밋 — 발송 성공분은 재기록/재시작에도 재발송 안 됨


def notify_events(con, day, finalized: int, leader_rows: list[dict],
                  gm3_rows: list[dict], summary: dict) -> int:
    """확정 기록 시 팩트 알림 발송. 반환: 실제 발송 건수. 절대 예외 전파 안 함."""
    if not finalized:
        return 0
    try:
        day_s = day.isoformat()
        sent = 0
        capped = con.execute(
            "SELECT COUNT(*) FROM paper_notified WHERE day=? AND kind IN ('trade','gm3exit')",
            (day_s,),
        ).fetchone()[0]

        # 1) 주도주(v2_leader) 트레이드 — 진입가 + 익절/손절 + 수익률
        for r in leader_rows:
            key = f"trade:v2_leader:{r['code']}:{day_s}"
            if _already_sent(con, key):
                continue
            if capped >= DAILY_CAP:
                logger.warning("[paper][tg] 일일 상한({}) 도달 — 트레이드 알림 억제", DAILY_CAP)
                break
            if _send(_fmt_trade(day, r)):
                _mark_sent(con, key, day, "trade")
                sent += 1
                capped += 1

        # 2) gm_v3 오늘 실현 청산 (EOR 제외)
        for r in gm3_rows:
            if r.get("eor") or str(r.get("closed_on")) != day_s:
                continue
            key = f"gm3exit:{r['code']}:{day_s}"
            if _already_sent(con, key):
                continue
            if capped >= DAILY_CAP:
                break
            if _send(_fmt_gm3_exit(r)):
                _mark_sent(con, key, day, "gm3exit")
                sent += 1
                capped += 1

        # 3) 당일 요약 (하루 1회, 상한과 무관)
        skey = f"summary:{day_s}"
        if not _already_sent(con, skey):
            if _send(_fmt_summary(day, finalized, summary)):
                _mark_sent(con, skey, day, "summary")
                sent += 1

        if sent:
            logger.info("[paper][tg] {} 알림 {}건 발송", day, sent)
        return sent
    except Exception as exc:
        logger.error("[paper][tg] notify_events 예외(무시): {}", exc)
        return 0
