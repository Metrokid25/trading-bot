"""페이퍼 텔레그램 팩트 알림 — 발송 게이팅·중복차단·상한·포맷 (네트워크 없음)."""
import sqlite3
from datetime import date

import pytest

from strategy import paper_notify
from strategy.paper_notify import (
    notify_events, _fmt_trade, _fmt_summary, _reason_kr,
    fmt_account_return, fmt_outperf,
    _split_telegram_text,
)

DAY = date(2026, 7, 6)


def _make_con():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE paper_notified ("
              "key TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL,"
              " sent_at TEXT NOT NULL)")
    # 요약이 당일 매매·v2 건별 통계를 조회하는 테이블
    c.execute("CREATE TABLE paper_trades ("
              " strategy TEXT, code TEXT, name TEXT, opened_on TEXT,"
              " closed_on TEXT, ret_gross REAL, ret_net REAL, detail TEXT,"
              " recorded_at TEXT)")
    return c


@pytest.fixture()
def con():
    return _make_con()


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


def test_long_summary_splits_without_omitting_text(monkeypatch):
    monkeypatch.setattr(paper_notify, "TELEGRAM_TEXT_LIMIT", 20)
    text = "첫 번째 줄입니다\n두 번째 줄입니다\n세 번째 줄은 조금 더 깁니다"
    chunks = _split_telegram_text(text)
    assert all(len(chunk) <= 20 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


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


def test_fmt_account_return_shows_total_nav_and_market_gap():
    s = fmt_account_return(0.9959, 1.1192)
    assert "총수익 -0.41%" in s
    assert "자산 100→99.59" in s
    assert "시장 참고차 -12.33%p" in s


def test_summary_intuitive_market_comparison(sent):
    """누적 성적을 모바일 고정폭 표로 보여준다."""
    con_ = _make_con()
    summ = {"v2_leader": {"trades": 0, "day_ret": -0.0559, "equity": 0.955},
            "gm_v3": {"closed_today": 0, "open_positions": 0, "equity": 1.0},
            "gm_v3_r13": {"closed_today": 0, "open_positions": 0, "equity": 0.955},
            "bench_bh": {"day_ret": -0.0559, "equity": 0.902, "stocks": 72}}
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    assert "시장(등록 72종목 보유 시)" in msg
    assert "<pre>모드       건수  승률     평균      누적" in msg
    assert "v2_lead" in msg and "건별" in msg
    assert "BASE" in msg and "+0.00%" in msg
    assert "+R13" in msg and "-4.50%" in msg
    assert "시장차: BASE +9.8p · +R13 +5.3p" in msg
    assert "시장차 +는 시장보다 양호, -는 부진" in msg


def test_summary_lists_today_trades_with_reason(sent):
    """오늘 매매 종목·수익률·익절/손절 사유가 요약에 나온다."""
    con_ = _make_con()
    con_.executemany(
        "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?)",
        [("gm_v3", "226320", "잇츠한불", "2026-07-06", DAY.isoformat(),
          -0.008, -0.009, "R10", "t"),
         ("v2", "161890", "한국콜마", DAY.isoformat(), DAY.isoformat(),
          0.0105, 0.0055, "2TP/BE", "t"),
         ("gm_v3", "035720", "카카오", "2026-07-06", DAY.isoformat(),
          0.0, 0.0, "R8|EOR", "t")])                  # EOR 은 제외돼야 함
    summ = {"gm_v3": {"closed_today": 1, "open_positions": 1, "equity": 0.99},
            "bench_bh": {"day_ret": 0.0, "equity": 0.9, "stocks": 70}}
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    assert "잇츠한불: -0.9% 🔴손절" in msg
    assert "한국콜마: +0.5% 🟢익절" in msg
    assert "카카오" not in msg                        # EOR 제외
    assert "BASE" in msg and "-0.90%" in msg and "-1.00%" in msg
    assert "보유: BASE 1종목" in msg


def test_summary_gm3_shows_winrate(sent):
    """gm_v3 계열에도 건수·평균·승률 표기 (EOR 제외) — 2026-07-14 오너 지시."""
    con_ = _make_con()
    con_.executemany(
        "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?)",
        [("gm_v3", "A", "에이", "2026-07-01", "2026-07-02",
          0.05, 0.045, "R8", "t"),
         ("gm_v3", "B", "비", "2026-07-01", "2026-07-03",
          -0.01, -0.009, "R10", "t"),
         ("gm_v3", "C", "씨", "2026-07-01", DAY.isoformat(),
          0.0, 0.0, "R8|EOR", "t"),                   # EOR → 통계 제외
         ("gm_v3", "D", "디", "2026-08-01", "2026-08-01",
          0.1, 0.09, "R8", "t")])                     # 미래일 → 제외
    summ = {"gm_v3": {"closed_today": 0, "open_positions": 1, "equity": 1.035},
            "bench_bh": {"day_ret": 0.0, "equity": 0.9, "stocks": 70}}
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    assert "BASE" in msg and "+1.80%" in msg and "+3.50%" in msg
    assert "시장차: BASE +13.5p" in msg
    assert "보유: BASE 1종목" in msg


def test_summary_no_trades_says_watching(sent):
    con_ = _make_con()
    summ = {"gm_v3": {"closed_today": 0, "open_positions": 0, "equity": 1.0},
            "bench_bh": {"day_ret": 0.01, "equity": 1.01, "stocks": 70}}
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    assert "관망" in msg


def test_summary_lists_every_today_trade_without_ellipsis(sent):
    con_ = _make_con()
    con_.executemany(
        "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?)",
        [("gm_v3", f"{i:06d}", f"종목{i}", DAY.isoformat(), DAY.isoformat(),
          -0.01, -0.009, "R10", "t") for i in range(17)])
    summ = {
        "v2": {"trades": 0, "day_ret": 0.0, "equity": 1.0},
        "gm_v3": {"closed_today": 17, "open_positions": 0, "equity": 0.9},
        "bench_bh": {"day_ret": 0.0, "equity": 0.9, "stocks": 70},
    }
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    assert "종목0" in msg and "종목16" in msg
    assert "… 외" not in msg


@pytest.mark.parametrize(
    ("trades", "expected"),
    [
        (0, "⚙️ v2 상태: 페이퍼 가동 중 · 오늘 매매 없음(관망)"),
        (3, "⚙️ v2 상태: 페이퍼 가동 중 · 오늘 3건 매매"),
    ],
)
def test_summary_shows_v2_runtime_status(sent, trades, expected):
    con_ = _make_con()
    summ = {
        "v2": {"trades": trades, "day_ret": 0.0, "equity": 1.0},
        "bench_bh": {"day_ret": 0.0, "equity": 1.0, "stocks": 70},
    }
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    assert expected in msg


def test_summary_v2_uses_per_trade_stats_not_compounding(sent):
    """v2 는 직렬복리 누적(+51% 착시) 대신 건수·평균·승률로 표기."""
    con_ = _make_con()
    con_.executemany(
        "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?)",
        [("v2", f"{i:06d}", f"종목{i}", "2026-07-03", "2026-07-03",
          0.05, 0.04 if i < 2 else -0.02, "2TP", "t") for i in range(3)])
    summ = {
        "v2": {"trades": 0, "day_ret": 0.0, "equity": 1.51},   # 착시 누적치
        "gm_v3_r13": {"closed_today": 0, "open_positions": 0, "equity": 0.956},
        "bench_bh": {"day_ret": 0.0, "equity": 0.87, "stocks": 75},
    }
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    assert "v2" in msg and "3" in msg and "67%" in msg
    assert "+2.00%" in msg and "건별" in msg
    assert "+51" not in msg                           # 착시 누적치 미표기
    assert "+R13" in msg                              # 변형 축 축약 라벨


def test_account_portfolio_axes_are_primary_total_period_return(sent):
    con_ = _make_con()
    con_.execute(
        "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?)",
        ("gm_v3_joined", "A", "교정축종목", DAY.isoformat(), DAY.isoformat(),
         0.01, 0.005, "R8", "t"))
    con_.execute(
        "INSERT INTO paper_trades VALUES (?,?,?,?,?,?,?,?,?)",
        ("v2_qv", "B", "품질축종목", DAY.isoformat(), DAY.isoformat(),
         0.02, 0.015, "1TP/BE", "t"))
    summ = {
        "account_period_start": "2026-07-04",
        "v2_portfolio": {"trades": 2, "equity": 1.01},
        "v2_leader_portfolio": {"trades": 1, "equity": 1.02},
        "v2_qv": {"trades": 1, "equity": 1.01},
        "v2_qv_portfolio": {"trades": 1, "equity": 1.01},
        "gm_v3_joined": {"closed_today": 1, "equity": 1.03},
        "v4r_joined": {"closed_today": 1, "equity": 0.99},
        "bench_v2_portfolio": {"equity": 0.98},
        "bench_joined": {"equity": 0.97},
        "bench_bh": {"day_ret": 0.0, "equity": 0.9, "stocks": 70},
    }
    notify_events(con_, DAY, 1, [], [], summ)
    msg = next(t for t in sent if "페이퍼 마감" in t)
    assert "총기간 봇 수익 07-04~07-06" in msg
    assert "v2 기본(대표): 총수익 +1.00% · 자산 100→101.00" in msg
    assert "v2 주도섹터" not in msg
    assert "v2 품질필터" not in msg
    assert "동시작 시장 참고치: -2.00%" in msg
    assert "노출 비매칭" in msg
    assert "실주문 손익 아님" in msg
    assert "v2_qv" not in msg
    assert "gm_v3_joined" not in msg
    assert "v4r_joined" not in msg
    assert "교정축종목" not in msg
    assert "품질축종목" not in msg


def test_fmt_trade_has_prices():
    msg = _fmt_trade(DAY, _trade("005930", "삼성전자", entry=71000, exit_=74550, ret=0.05))
    assert "71,000" in msg and "74,550" in msg and "+5.00%" in msg
