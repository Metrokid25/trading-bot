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
import time as _time
from datetime import timedelta

import httpx
from loguru import logger

from config import settings
from core.time_utils import now_kst, to_db_iso

DAILY_CAP = 40         # 하루 개별 트레이드 알림 상한 (요약 제외)
RETENTION_DAYS = 90    # paper_notified 보관 일수 (무한증가 방지)
_SEND_ATTEMPTS = 3     # 전송 재시도 (429/5xx/네트워크) — 한 호출 내에서만

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    # 모듈 수명 클라이언트 재사용 (매 알림마다 새 TLS 핸드셰이크 방지).
    global _client
    if _client is None:
        _client = httpx.Client(timeout=8.0)
    return _client


def _send(text: str) -> bool:
    """텔레그램 sendMessage — 한 호출 내 짧은 재시도만. 실패 시 False.

    호출측(notify_events)은 '전송 시도 = 마킹'(mark-before-send)이라 여기서
    False 를 반환해도 다음 사이클에 재발송하지 않는다(재시도 폭풍·중복 방지).
    일시 장애는 아래 in-call 재시도(429/5xx/네트워크)로 흡수한다.
    """
    tok = settings.TELEGRAM_BOT_TOKEN
    chat = settings.TELEGRAM_CHAT_ID
    if not tok or ":" not in tok or "your_bot_token" in tok.lower() or not chat:
        logger.warning("[paper][tg] 토큰/채널 미설정 — 알림 스킵")
        return False
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    for attempt in range(_SEND_ATTEMPTS):
        try:
            r = _get_client().post(url, json=payload)
            if r.status_code == 200:
                return True
            if r.status_code == 429 or r.status_code >= 500:
                # 레이트리밋/서버오류만 재시도 (retry_after 있으면 상한 5s)
                wait = 1.0 * (attempt + 1)
                try:
                    wait = min(float(r.json().get("parameters", {})
                                     .get("retry_after", wait)), 5.0)
                except Exception:
                    pass
                if attempt < _SEND_ATTEMPTS - 1:
                    _time.sleep(wait)
                    continue
            logger.error("[paper][tg] 발송 실패 HTTP {}: {}", r.status_code, r.text[:200])
            return False
        except Exception as exc:
            if attempt < _SEND_ATTEMPTS - 1:
                _time.sleep(0.6 * (attempt + 1))
                continue
            logger.error("[paper][tg] 발송 예외: {}", exc)
            return False
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


def fmt_outperf(strat_eq: float, bench_eq: float) -> str:
    """누적 초과수익 표기 — 절대수익 병기 + 출처 태그.

    '초과수익(알파)'은 벤치가 빠진 만큼(손실회피)과 실제 매매수익이 섞인다.
    전략 절대수익을 나란히 보여 출처를 드러내고, 매매수익이 없거나 마이너스면
    '손실회피/손실방어'로 명시한다 (절대손익 단독 오독 방지, 헌장 절대규칙②).
    """
    strat_abs = strat_eq - 1.0
    bench_abs = bench_eq - 1.0
    alpha = strat_eq - bench_eq
    if abs(strat_abs) < 0.0005 and alpha > 0.0005:
        tag = " → 전량 손실회피(매매수익 0)"
    elif strat_abs < -0.0005 and alpha > 0.0005:
        tag = " → 손실방어(전략도 하락, 벤치보다 덜)"
    else:
        tag = ""
    return (f"전략 {strat_abs:+.2%} · 벤치 {bench_abs:+.2%} · "
            f"초과 {alpha:+.2%}p{tag}")


# summary dict 에서 전략이 아닌 메타 키 — 이 외 dict 값은 전부 전략으로 간주해
# 출력하므로, 전략 축이 늘어나도(GM3_VARIANTS 등) 자동으로 요약에 포함된다.
_SUMMARY_META_KEYS = {"day", "universe", "finalized", "skipped", "bench_bh"}


def _fmt_summary(day, finalized: int, summary: dict) -> str:
    """일일 요약 — 돌고 있는 전략 전부 한 줄씩 (2026-07-14 오너 지시).

    각 줄 = 누적 절대수익 · 벤치 대비 초과(%p) · 오늘 활동. 절대수익·벤치
    병기 원칙(헌장 절대규칙②) 유지, 손실회피/방어는 압축 태그로.
    """
    bench = summary.get("bench_bh", {})
    tag = "(확정)" if finalized else "(잠정)"
    bench_eq = bench.get("equity", 1.0)
    lines = [
        f"📊 페이퍼 마감 {day} {tag}",
        f"벤치: 당일 {bench.get('day_ret', 0.0):+.2%} · 누적 {bench_eq - 1:+.2%} "
        f"({bench.get('stocks', 0)}종목)",
    ]
    for name, s in summary.items():
        if name in _SUMMARY_META_KEYS or not isinstance(s, dict):
            continue
        eq = s.get("equity", 1.0)
        strat_abs = eq - 1.0
        alpha = eq - bench_eq
        if "trades" in s:                       # v2 계열 (당일 스캘핑)
            act = f"오늘 {s['trades']}건"
        else:                                   # gm_v3 계열 (스윙)
            act = (f"청산 {s.get('closed_today', 0)}"
                   f"·보유 {s.get('open_positions', 0)}")
        if abs(strat_abs) < 0.0005 and alpha > 0.0005:
            src = " (손실회피)"
        elif strat_abs < -0.0005 and alpha > 0.0005:
            src = " (방어)"
        else:
            src = ""
        lines.append(f"{name}: 누적 {strat_abs:+.2%} · 초과 {alpha:+.2%}p · {act}{src}")
    return "\n".join(lines)


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
        # 오래된 알림 이력 프루닝 (무한증가 방지)
        con.execute("DELETE FROM paper_notified WHERE day < ?",
                    ((now_kst().date() - timedelta(days=RETENTION_DAYS)).isoformat(),))
        con.commit()

        sent = 0
        capped = con.execute(
            "SELECT COUNT(*) FROM paper_notified WHERE day=? AND kind IN ('trade','gm3exit')",
            (day_s,),
        ).fetchone()[0]

        def _emit(key: str, kind: str, text: str) -> None:
            """mark-before-send: 시도 전에 마킹해 재시도 폭풍·중복 전송을 차단한다
            (at-most-once). 일시 장애는 _send 의 in-call 재시도로 흡수."""
            nonlocal sent
            _mark_sent(con, key, day, kind)
            if _send(text):
                sent += 1
            else:
                logger.warning("[paper][tg] 발송 실패(마킹됨·재발송 안 함): {}", key)

        # 1) 주도주(v2_leader) 트레이드 — 진입가 + 익절/손절 + 수익률
        for r in leader_rows:
            key = f"trade:v2_leader:{r['code']}:{day_s}"
            if _already_sent(con, key):
                continue
            if capped >= DAILY_CAP:
                logger.warning("[paper][tg] 일일 상한({}) 도달 — 트레이드 알림 억제", DAILY_CAP)
                break
            _emit(key, "trade", _fmt_trade(day, r))
            capped += 1

        # 2) gm_v3 오늘 실현 청산 (EOR 제외). closed_on 은 date/str 혼용 방어로 [:10] 비교.
        for r in gm3_rows:
            if r.get("eor") or str(r.get("closed_on"))[:10] != day_s:
                continue
            key = f"gm3exit:{r['code']}:{day_s}"
            if _already_sent(con, key):
                continue
            if capped >= DAILY_CAP:
                break
            _emit(key, "gm3exit", _fmt_gm3_exit(r))
            capped += 1

        # 3) 당일 요약 (하루 1회, 상한과 무관)
        skey = f"summary:{day_s}"
        if not _already_sent(con, skey):
            _emit(skey, "summary", _fmt_summary(day, finalized, summary))

        if sent:
            logger.info("[paper][tg] {} 알림 {}건 발송", day, sent)
        return sent
    except Exception as exc:
        logger.error("[paper][tg] notify_events 예외(무시): {}", exc)
        return 0
