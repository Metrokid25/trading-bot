"""페이퍼 텔레그램 팩트 알림 — 발송 게이팅·중복차단·상한·포맷 (네트워크 없음)."""
import sqlite3
from datetime import date

import pytest

from strategy import paper_notify
from strategy.paper_notify import notify_events, _fmt_trade, _fmt_summary, _reason_kr

DAY = date(2026, 7, 6)


@pytest.fixture()
def con():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE paper_notified ("
              "key TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL,"
              " sent_at TEXT NOT NULL)")
    return c


@pytest.fixture()
def sent(monkeypatch):
    """_send 를 가로채 발송된 텍스트를 수집(네트워크 차단)."""
    box: list[str] = []
    monkeypatch.setattr(paper_notify, "_send", lambda text: (box.append(text) or True))
    return box


def _trade(code, name, sector="반도체", ret=0.05, reason="2TP", entry=100, exit_=110):
    return {"code": code, "name": name, "sector": sector, "ret_net": ret,
            "reason": reason, "entry": entry, "exit": exit_}


SUMMARY = {
    "v2_leader": {"trades": 1, "day_ret": 0.05, "alpha_vs_bench": 0.06},
    "gm_v3": {"closed_today": 0, "open_positions": 2},
    "bench_bh": {"day_ret": -0.01},
}


def test_no_send_when_not_finalized(con, sent):
    n = notify_events(con, DAY, 0, [_trade("005930", "삼성전자")], [], SUMMARY)
    assert n == 0 and sent == []


def test_finalized_sends_trade_and_summary(con, sent):
    n = notify_events(con, DAY, 1, [_trade("005930", "삼성전자")], [], SUMMARY)
    assert n == 2                      # 트레이드 1 + 요약 1
    assert any("삼성전자" in t and "진입" in t for t in sent)
    assert any("페이퍼 마감" in t for t in sent)


def test_dedup_across_reruns(con, sent):
    rows = [_trade("005930", "삼성전자")]
    notify_events(con, DAY, 1, rows, [], SUMMARY)
    first = list(sent)
    # 같은 날 재기록 — 재발송 0
    n2 = notify_events(con, DAY, 1, rows, [], SUMMARY)
    assert n2 == 0
    assert sent == first               # 추가 발송 없음


def test_gm3_exit_only_today_realized(con, sent):
    gm3 = [
        {"code": "000660", "name": "하이닉스", "ret_net": -0.04,
         "detail": "R10", "closed_on": "2026-07-06", "eor": False},
        {"code": "035720", "name": "카카오", "ret_net": 0.0,
         "detail": "", "closed_on": "2026-07-06", "eor": True},        # EOR 제외
        {"code": "005380", "name": "현대차", "ret_net": 0.02,
         "detail": "R7", "closed_on": "2026-07-03", "eor": False},     # 과거일 제외
    ]
    notify_events(con, DAY, 1, [], gm3, {**SUMMARY, "v2_leader": {"trades": 0}})
    exits = [t for t in sent if "gm_v3 청산" in t]
    assert len(exits) == 1 and "하이닉스" in exits[0]


def test_daily_cap(con, sent, monkeypatch):
    monkeypatch.setattr(paper_notify, "DAILY_CAP", 3)
    rows = [_trade(f"{i:06d}", f"종목{i}") for i in range(10)]
    notify_events(con, DAY, 1, rows, [], {**SUMMARY, "v2_leader": {"trades": 10}})
    trade_msgs = [t for t in sent if "진입·청산" in t]
    assert len(trade_msgs) == 3        # 상한 3
    assert any("페이퍼 마감" in t for t in sent)   # 요약은 상한과 무관


def test_send_failure_marks_and_no_retry(con, monkeypatch):
    """전송 실패해도 마킹 → 다음 사이클에 재발송 안 함 (재시도 폭풍/중복 차단)."""
    calls: list[str] = []
    monkeypatch.setattr(paper_notify, "_send",
                        lambda text: (calls.append(text), False)[1])  # 항상 실패
    rows = [_trade("005930", "삼성전자")]
    notify_events(con, DAY, 1, rows, [], SUMMARY)
    first_calls = len(calls)
    assert first_calls >= 1                       # 시도는 함
    # 재실행 — 이미 마킹돼 재시도 안 함
    notify_events(con, DAY, 1, rows, [], SUMMARY)
    assert len(calls) == first_calls              # 추가 시도 0 = 폭풍 없음


def test_reason_kr_mapping():
    assert _reason_kr("SL") == "🛑손절"
    assert _reason_kr("2TP/BE") == "🎯익절"
    assert _reason_kr("EOD") == "⏹정리"


def test_fmt_trade_has_prices():
    msg = _fmt_trade(DAY, _trade("005930", "삼성전자", entry=71000, exit_=74550, ret=0.05))
    assert "71,000" in msg and "74,550" in msg and "+5.00%" in msg
