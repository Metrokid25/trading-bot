"""한글 종목명 ↔ 종목코드 매핑.

KRX `kind.krx.co.kr/corpgeneral/corpList.do` 마스터를 내려받아 로컬 JSON으로 캐시한다.
KOSPI + KOSDAQ 전 종목을 포함. KIS 공식 API에는 이름 검색 엔드포인트가 없어 보완 목적.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
from loguru import logger

from config import settings

_CODE_RE = re.compile(r"^\d{6}$")
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_KRX_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType={mkt}"


class StockMaster:
    """한글/영문 종목명 ↔ 6자리 종목코드 조회."""

    def __init__(self, cache_path: Path | None = None) -> None:
        self._by_name: dict[str, str] = {}  # normalized name → code
        self._by_code: dict[str, str] = {}  # code → original name
        self._cache_path = cache_path or Path(settings.DB_PATH).parent / "stock_master.json"
        self._lock = asyncio.Lock()
        self._loaded = False
        self._load_disk()

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", s).lower()

    def _load_disk(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            self._by_code = data.get("by_code", {})
            self._by_name = {self._norm(n): c for c, n in self._by_code.items()}
            self._loaded = bool(self._by_code)
            logger.info(f"[StockMaster] 캐시 로드: {len(self._by_code)}종목")
        except Exception as e:
            logger.warning(f"[StockMaster] 캐시 로드 실패: {e}")

    def _save_disk(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({"by_code": self._by_code}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[StockMaster] 캐시 저장 실패: {e}")

    async def refresh(self) -> int:
        """KRX에서 KOSPI+KOSDAQ 마스터를 재다운로드해 캐시를 갱신한다."""
        by_code: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            for mkt in ("stockMkt", "kosdaqMkt"):
                try:
                    r = await client.get(_KRX_URL.format(mkt=mkt))
                    r.encoding = "euc-kr"
                    by_code.update(self._parse(r.text))
                except Exception as e:
                    logger.warning(f"[StockMaster] KRX {mkt} 다운로드 실패: {e}")
        if not by_code:
            return 0
        async with self._lock:
            self._by_code = by_code
            self._by_name = {self._norm(n): c for c, n in by_code.items()}
            self._loaded = True
            self._save_disk()
        logger.info(f"[StockMaster] KRX 마스터 갱신: {len(by_code)}종목")
        return len(by_code)

    @staticmethod
    def _parse(html: str) -> dict[str, str]:
        """KRX corpList HTML에서 (code → name) 추출."""
        out: dict[str, str] = {}
        for row in _ROW_RE.findall(html):
            tds = _TD_RE.findall(row)
            if len(tds) < 3:
                continue
            name = _TAG_RE.sub("", tds[0]).strip()
            code = _TAG_RE.sub("", tds[2]).strip()
            if name and _CODE_RE.match(code):
                out[code] = name
        return out

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.refresh()

    async def resolve(self, query: str) -> tuple[str, str] | None:
        """입력을 (code, name)으로 해석. 실패 시 None.

        - 6자리 숫자: 코드로 간주
        - 그 외: 이름 정확 일치 → 부분 일치 1건 우선
        """
        q = query.strip()
        if not q:
            return None
        if _CODE_RE.match(q):
            await self._ensure_loaded()
            return q, self._by_code.get(q, "")

        await self._ensure_loaded()
        key = self._norm(q)
        if key in self._by_name:
            code = self._by_name[key]
            return code, self._by_code[code]
        matches = self._partial(key, limit=2)
        if len(matches) == 1:
            code, name = matches[0]
            return code, name
        return None

    def _partial(self, key: str, limit: int = 5) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for norm_name, code in self._by_name.items():
            if key in norm_name:
                out.append((code, self._by_code[code]))
                if len(out) >= limit:
                    break
        return out

    async def search(self, query: str, limit: int = 5) -> list[tuple[str, str]]:
        """부분 일치 후보 목록."""
        await self._ensure_loaded()
        q = query.strip()
        if not q:
            return []
        if _CODE_RE.match(q):
            name = self._by_code.get(q)
            return [(q, name)] if name else []
        return self._partial(self._norm(q), limit=limit)

    async def close(self) -> None:
        pass
