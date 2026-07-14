"""페이퍼 텔레그램 팩트 알림 — 발송 게이팅·중복차단·상한·포맷 (네트워크 없음)."""
import sqlite3
from datetime import date

import pytest

from strategy import paper_notify
from strategy.paper_notify import (
    notify_events, _fmt_trade, _fmt_summary, _reason_kr, fmt_outperf,
)

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


def test_fmt_outperf_pure_avoidance():
    # 전략 flat(거래 0) + 벤치 하락 → 초과수익 전량 손실회피
    s = fmt_outperf(1.0, 0.902)
    assert "전략 +0.00%" in s and "벤치 -9.80%" in s and "초과 +9.80%p" in s
    assert "전량 손실회피" in s


def test_fmt_outperf_loss_defense():
    # 전략도 하락(-3%)했지만 벤치(-9.8%)보다 덜 → 손실방어
    s = fmt_outperf(0.97, 0.902)
    assert "손실방어" in s and "초과 +6.80%p" in s


def test_fmt_outperf_real_gain_no_tag():
    # 전략 실제 수익(+2%) → 손실회피/방어 태그 없음
    s = fmt_outperf(1.02, 0.902)
    assert "전략 +2.00%" in s and "초과 +11.80%p" in s
    assert "손실회피" not in s and "손실방어" not in s


def test_summary_shows_absolute_and_avoidance(sent):
    con_ = sqlite3.connect(":memory:")
    con_.execute("CREATE TABLE paper_notified (key TEXT PRIMARY KEY, day TEXT,"
                 " kind TEXT, sent_at TEXT)")
    summ = {"v2_leader": {"trades": 0, "day_ret": -0.0559, "equity": 1.0,
                          "alpha_vs_bench": 0.098},
            "gm_v3": {"closed_today": 0, "open_positions": 0, "equity": 1.0},
            "bench_bh": {"day_ret": -0.0559, "equity": 0.902}}
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    assert "누적 -9.80%" in msg                 # 벤치 절대수익 병기
    assert "v2_leader: 누적 +0.00% · 초과 +9.80%p" in msg
    assert "손실회피" in msg


def test_summary_includes_every_strategy(sent):
    """돌고 있는 전략 전부(6축) + 벤치가 요약에 포함 — 축 추가 시 자동 확장."""
    con_ = sqlite3.connect(":memory:")
    con_.execute("CREATE TABLE paper_notified (key TEXT PRIMARY KEY, day TEXT,"
                 " kind TEXT, sent_at TEXT)")
    summ = {
        "day": "2026-07-14", "universe": 73, "finalized": 1,
        "v2": {"trades": 2, "day_ret": 0.01, "equity": 1.51},
        "v2_leader": {"trades": 0, "day_ret": 0.0, "equity": 0.955},
        "gm_v3": {"closed_today": 0, "open_positions": 1, "equity": 0.982},
        "gm_v3_r13": {"closed_today": 1, "open_positions": 0, "equity": 0.956},
        "gm_v3_r14": {"closed_today": 0, "open_positions": 0, "equity": 0.982},
        "gm_v3_r13r14": {"closed_today": 1, "open_positions": 0, "equity": 0.956},
        "bench_bh": {"day_ret": -0.0089, "equity": 0.8735, "stocks": 75},
    }
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    for strat in ("v2:", "v2_leader:", "gm_v3:", "gm_v3_r13:",
                  "gm_v3_r14:", "gm_v3_r13r14:", "벤치:"):
        assert strat in msg, f"{strat} 누락"
    assert "day" not in msg.splitlines()[2]      # 메타 키는 전략으로 출력 안 됨
    assert "오늘 2건" in msg                      # v2 활동 표기
    assert "청산 1·보유 0" in msg                 # gm3 변형 활동 표기


def test_fmt_trade_has_prices():
    msg = _fmt_trade(DAY, _trade("005930", "삼성전자", entry=71000, exit_=74550, ret=0.05))
    assert "71,000" in msg and "74,550" in msg and "+5.00%" in msg
