"""섹터 픽 입력 텍스트 파서.

3가지 입력 패턴 지원:
1. 정식: /p YYYY-MM-DD\\n[섹터명]\\n종목 종목 ...
2. 날짜 생략: /p\\n[섹터명]\\n종목 ... (pick_date=오늘)
3. 한 줄 단일 섹터: /p 섹터명 종목 종목 ...
"""
from __future__ import annotations

import re
from datetime import datetime

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOKEN_SPLIT_RE = re.compile(r"[\s,]+")


class ParseError(ValueError):
    """/p 입력 형식 오류."""


def today_kst() -> str:
    """시스템 시간(KST 가정) 기준 YYYY-MM-DD."""
    return datetime.now().strftime("%Y-%m-%d")


def parse_pick_input(text: str) -> tuple[str, dict[str, list[str]]]:
    """입력 텍스트 → (pick_date, {sector_name: [stock_names]}).

    ParseError: 형식을 식별할 수 없음.
    """
    body = _strip_command_prefix(text)
    if not body:
        raise ParseError(
            "입력이 비어 있습니다.\n"
            "형식 예:\n/p 2026-04-22\n[섹터명]\n종목1 종목2"
        )

    lines = body.split("\n", 1)
    first_line = lines[0].strip()
    rest = lines[1] if len(lines) > 1 else ""

    # 멀티라인 입력 (패턴 1 또는 2)
    if rest.strip() or first_line.startswith("["):
        if _DATE_RE.match(first_line):
            pick_date = first_line
            body_to_parse = rest
        else:
            pick_date = today_kst()
            body_to_parse = body
        sectors = _parse_sector_body(body_to_parse)
        if not sectors:
            raise ParseError(
                "섹터를 찾을 수 없습니다. [섹터명] 헤더 필요.\n"
                "형식 예:\n/p 2026-04-22\n[섹터명]\n종목1 종목2"
            )
        return pick_date, sectors

    # 한 줄 입력 (패턴 3)
    tokens = first_line.split()
    if len(tokens) == 1 and _DATE_RE.match(tokens[0]):
        raise ParseError(
            "날짜만 입력되었습니다. 섹터/종목이 필요합니다.\n"
            "형식 예:\n/p 2026-04-22\n[섹터명]\n종목1 종목2"
        )
    if len(tokens) < 2:
        raise ParseError(
            "형식을 인식할 수 없습니다.\n"
            "단일 섹터: /p 섹터명 종목1 종목2\n"
            "다중 섹터: /p YYYY-MM-DD 이후 [섹터] 블록 나열"
        )
    if _DATE_RE.match(tokens[0]):
        raise ParseError(
            "날짜 다음에 섹터 블록이 필요합니다.\n"
            "형식 예:\n/p 2026-04-22\n[섹터명]\n종목1 종목2"
        )
    return today_kst(), {tokens[0]: tokens[1:]}


def _strip_command_prefix(text: str) -> str:
    stripped = text.lstrip()
    if not stripped:
        return ""
    if not stripped.startswith("/"):
        return stripped.strip()
    # /p 또는 /p@botname 제거
    space_idx = stripped.find(" ")
    newline_idx = stripped.find("\n")
    candidates = [i for i in (space_idx, newline_idx) if i != -1]
    cut = min(candidates) if candidates else len(stripped)
    return stripped[cut:].strip()


def _parse_sector_body(body: str) -> dict[str, list[str]]:
    sectors: dict[str, list[str]] = {}
    current: str | None = None
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            if current:
                sectors.setdefault(current, [])
            continue
        if current is None:
            continue
        tokens = [t for t in _TOKEN_SPLIT_RE.split(line) if t]
        sectors[current].extend(tokens)
    return {k: v for k, v in sectors.items() if v}
