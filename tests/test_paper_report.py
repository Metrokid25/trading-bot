import sqlite3

from strategy import paper_runner


def test_report_leads_with_total_period_account_return(monkeypatch, capsys):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE paper_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO paper_meta VALUES ('paper_start', '2026-07-06')")
    con.execute(
        "CREATE TABLE paper_daily ("
        "day TEXT, strategy TEXT, n_trades INTEGER, day_ret REAL, equity REAL, "
        "finalized INTEGER)"
    )
    rows = [
        ("2026-08-04", "v2_portfolio", 1, -0.009, 0.991, 1),
        ("2026-08-04", "v2_leader_portfolio", 0, 0.0, 1.0, 1),
        ("2026-08-04", "v2_qv_portfolio", 1, -0.009, 0.991, 1),
        ("2026-08-04", "bench_v2_portfolio", 76, 0.0556, 1.0556, 1),
        ("2026-08-05", "v2_portfolio", 2, 0.0049, 0.9959, 0),
        ("2026-08-05", "v2_leader_portfolio", 0, 0.0, 1.0, 0),
        ("2026-08-05", "v2_qv_portfolio", 1, 0.0038, 0.9947, 0),
        ("2026-08-05", "bench_v2_portfolio", 76, 0.0603, 1.1192, 0),
        ("2026-08-05", "bench_bh", 76, 0.0603, 0.8693, 0),
        ("2026-08-05", "v2", 3, 0.0441, 1.3012, 0),
    ]
    con.executemany("INSERT INTO paper_daily VALUES (?,?,?,?,?,?)", rows)
    monkeypatch.setattr(paper_runner, "paper_conn", lambda: con)

    paper_runner.report()

    out = capsys.readouterr().out
    headline = out.index("총기간 봇 수익")
    details = out.index("day         strategy")
    assert headline < details
    assert "2026-08-04~2026-08-05" in out
    assert "장중 잠정" in out
    assert "v2 기본(대표)" in out and "총수익 -0.41%" in out
    assert "자산 100→99.59" in out
    assert "동시작 시장 참고치  총수익 +11.92%" in out
    assert "노출 비매칭" in out
    assert "연구용 계좌형 관찰축 (대표 봇 수익 아님)" in out
    assert "연구용 참고지표 (직렬복리·실제 계좌 총수익 아님)" in out
    assert "v2            전략 +30.12%" in out
