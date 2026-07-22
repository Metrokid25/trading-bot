"""국내 주식·ETF 한글 종목명 ↔ 종목코드 매핑.

KRX 상장법인 목록과 KIS ETF 마스터를 내려받아 로컬 JSON으로 캐시한다.
KOSPI + KOSDAQ 주식과 ETF를 포함한다.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from pathlib import Path

import httpx
from loguru import logger

from config import settings

# KRX 신형 단축코드는 숫자 4자리 + 영문/숫자 1자리 + 숫자 1자리 형식도 사용한다.
# 예: SOL AI반도체TOP2플러스 0167A0. 기존 숫자 6자리도 같은 패턴에 포함된다.
_CODE_RE = re.compile(r"^\d{4}[0-9A-Z]\d$")
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_KRX_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&marketType={mkt}"
_KIS_KOSPI_MASTER_URL = (
    "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
)
_KIS_MASTER_TAIL_LEN = 228
_ETF_GROUP_CODE = "EF"
_CACHE_VERSION = 3


class StockMaster:
    """한글/영문 종목명 ↔ 6자리 영숫자 종목코드 조회."""

    def __init__(self, cache_path: Path | None = None) -> None:
        self._by_name: dict[str, str] = {}  # normalized name → code
        self._by_code: dict[str, str] = {}  # code → original name
        self._types: dict[str, str] = {}  # code → stock | etf
        self._cache_path = cache_path or Path(settings.DB_PATH).parent / "stock_master.json"
        self._lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._load_task_lock = asyncio.Lock()
        self._load_task: asyncio.Task[int] | None = None
        self._cache_version: int | None = None
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
            self._cache_version = data.get("version")
            self._by_code = data.get("by_code", {})
            saved_types = data.get("types", {})
            self._types = {
                code: saved_types.get(code, "stock") for code in self._by_code
            }
            self._by_name = {self._norm(n): c for c, n in self._by_code.items()}
            self._loaded = bool(self._by_code) and self._cache_version == _CACHE_VERSION
            suffix = "" if self._loaded else " (구버전 — 첫 검색 시 갱신)"
            logger.info(f"[StockMaster] 캐시 로드: {len(self._by_code)}종목{suffix}")
        except Exception as e:
            logger.warning(f"[StockMaster] 캐시 로드 실패: {e}")

    def _save_disk(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(
                    {
                        "version": _CACHE_VERSION,
                        "by_code": self._by_code,
                        "types": self._types,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self._cache_version = _CACHE_VERSION
        except Exception as e:
            logger.warning(f"[StockMaster] 캐시 저장 실패: {e}")

    async def refresh(self) -> int:
        """KRX 상장법인 + KIS ETF 마스터를 병합해 캐시를 갱신한다.

        KRX corpList에는 ETF가 없어서 KIS 공식 KOSPI 마스터의 EF 상품군을
        별도로 읽는다. ETF 다운로드만 실패하면 직전 ETF 캐시를 유지한다.
        """
        async with self._refresh_lock:
            return await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> int:
        """호출자가 ``_refresh_lock``을 보유한 상태에서 실제 갱신 수행."""
        stocks: dict[str, str] = {}
        etfs: dict[str, str] | None = None
        stock_sources_ok = True
        async with httpx.AsyncClient(timeout=30.0) as client:
            for mkt in ("stockMkt", "kosdaqMkt"):
                try:
                    r = await client.get(_KRX_URL.format(mkt=mkt))
                    r.encoding = "euc-kr"
                    parsed = self._parse(r.text)
                    if not parsed:
                        raise ValueError("주식 0종목")
                    stocks.update(parsed)
                except Exception as e:
                    stock_sources_ok = False
                    logger.warning(f"[StockMaster] KRX {mkt} 다운로드 실패: {e}")
            try:
                r = await client.get(_KIS_KOSPI_MASTER_URL)
                r.raise_for_status()
                etfs = self._parse_kis_etf_master(r.content)
                if not etfs:
                    raise ValueError("ETF 0종목")
            except Exception as e:
                logger.warning(f"[StockMaster] KIS ETF 마스터 다운로드 실패: {e}")

        # 한 시장만 성공한 부분 마스터로 기존 KOSPI/KOSDAQ 전체를 덮지 않는다.
        if not stock_sources_ok:
            return 0

        cached_etfs = {
            code: name
            for code, name in self._by_code.items()
            if self._types.get(code) == "etf"
        }
        etf_source_ok = etfs is not None
        if etfs is None:
            etfs = cached_etfs
        # 최신 캐시의 ETF는 원본 장애 때 완전본으로 재사용할 수 있다. 하지만 구형
        # 캐시는 영문 혼합 ETF가 빠져 있으므로 v3로 승격하지 않고 다음 요청에서 재시도.
        cache_complete = bool(etfs) and (
            etf_source_ok or self._cache_version == _CACHE_VERSION
        )
        by_code = {**stocks, **etfs}
        async with self._lock:
            self._by_code = by_code
            self._types = {
                **{code: "stock" for code in stocks},
                **{code: "etf" for code in etfs},
            }
            self._by_name = {self._norm(n): c for c, n in by_code.items()}
            self._loaded = cache_complete
            # 최초 ETF 수신 실패를 정상 최신 캐시로 굳히지 않는다. 메모리의 주식
            # 검색은 허용하되 다음 검색에서 ETF 다운로드를 다시 시도한다.
            if cache_complete:
                self._save_disk()
            else:
                logger.warning("[StockMaster] ETF 없는 불완전 캐시 — 저장하지 않고 재시도")
        logger.info(
            f"[StockMaster] 마스터 갱신: 주식 {len(stocks)} + ETF {len(etfs)}"
        )
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

    @staticmethod
    def _parse_kis_etf_master(payload: bytes) -> dict[str, str]:
        """KIS kospi_code.mst ZIP에서 ETF(EF) 단축코드와 한글명을 추출."""
        out: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [name for name in archive.namelist() if name.endswith("kospi_code.mst")]
            if not names:
                raise ValueError("kospi_code.mst 없음")
            raw = archive.read(names[0]).decode("cp949")

        for row in raw.splitlines():
            if len(row) <= _KIS_MASTER_TAIL_LEN + 21:
                continue
            prefix = row[:-_KIS_MASTER_TAIL_LEN]
            tail = row[-_KIS_MASTER_TAIL_LEN:]
            # 실파일은 고정폭 꼬리 앞에 구분 공백 1자가 붙는다. 테스트용/과거
            # 변형처럼 공백이 없는 레코드도 함께 허용한다.
            group_code = tail[1:3] if tail.startswith(" ") else tail[:2]
            if group_code != _ETF_GROUP_CODE:
                continue
            code = prefix[:9].strip()
            name = prefix[21:].strip()
            if _CODE_RE.match(code) and name:
                out[code] = name
        return out

    def instrument_type(self, code: str) -> str:
        """검색 UI 표시용 상품 유형. 알 수 없는 코드는 stock으로 호환 처리."""
        return self._types.get(code, "stock")

    async def ensure_loaded(self) -> None:
        """동시에 들어온 최초 요청들이 하나의 갱신 작업을 공유하도록 보장."""
        if self._loaded:
            return
        async with self._load_task_lock:
            if self._load_task is None or self._load_task.done():
                self._load_task = asyncio.create_task(self.refresh())
            task = self._load_task
        try:
            # 한 HTTP 요청이 취소돼도 다른 대기자의 공용 갱신은 계속한다.
            await asyncio.shield(task)
        finally:
            if task.done():
                async with self._load_task_lock:
                    if self._load_task is task:
                        self._load_task = None

    async def _ensure_loaded(self) -> None:
        await self.ensure_loaded()

    async def resolve(self, query: str) -> tuple[str, str] | None:
        """입력을 (code, name)으로 해석. 실패 시 None.

        - 6자리 KRX 영숫자 단축코드: 코드로 간주
        - 그 외: 이름 정확 일치 → 부분 일치 1건 우선
        """
        q = query.strip()
        if not q:
            return None
        code_query = q.upper()
        if _CODE_RE.match(code_query):
            await self._ensure_loaded()
            return code_query, self._by_code.get(code_query, "")

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
        code_query = q.upper()
        if _CODE_RE.match(code_query):
            name = self._by_code.get(code_query)
            return [(code_query, name)] if name else []
        return self._partial(self._norm(q), limit=limit)

    async def close(self) -> None:
        pass
