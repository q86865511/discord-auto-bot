"""SQLite 資料庫管理層。

設計目標:
- 取代原本的 config.json / slot_analysis.json / gambling_history.json
- 內部用 stdlib `sqlite3`(零外部依賴)
- 寫入用 WAL mode + 短交易,讀取與寫入可並行
- async 介面包成 `to_thread`,不會阻塞 event loop
- 對外只暴露 `Database` 類,callers 不直接寫 SQL

Schema:
- meta(key, value)               全域 key/value(version、created_at 等)
- config(section, payload)       每個設定區段一筆 JSON;敏感欄位個別加密
- slot_analysis(payload)          single row(id=1)
- history(id, ts, bet, ...)      下注紀錄,每筆一 row,有 index
- secrets(key, value)             加密過的單獨敏感字串(如 email password)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from .constants import DB_PATH, HISTORY_MAX_LEN
from .crypto import Cipher

log = logging.getLogger(__name__)


SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS config (
    section TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS secrets (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slot_analysis (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    bet    INTEGER NOT NULL,
    before_balance INTEGER NOT NULL,
    after_balance  INTEGER NOT NULL,
    change_value   INTEGER NOT NULL,
    result         TEXT NOT NULL,
    lines_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts);

-- 股票價格快照 - 由 stock_loop 定期寫入
CREATE TABLE IF NOT EXISTS stock_prices (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_ts ON stock_prices(ts);
CREATE INDEX IF NOT EXISTS idx_stock_symbol_ts ON stock_prices(symbol, ts);

-- 使用者持股快照 - 由 stock_loop 在抓 /portfolio 時更新
CREATE TABLE IF NOT EXISTS stock_holdings (
    symbol     TEXT PRIMARY KEY,
    shares     REAL NOT NULL,
    avg_cost   REAL NOT NULL DEFAULT 0,
    last_seen  TEXT NOT NULL
);

-- 股票相關新聞 - 由 stock_loop 點「近期新聞」button 抓回來。
-- UNIQUE(symbol, news_date, title) — 避免重複寫入同一則新聞,讓
-- upsert_news_items 能用 INSERT OR IGNORE 偵測「真的新加入」的項目。
CREATE TABLE IF NOT EXISTS stock_news (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    news_date  TEXT NOT NULL,
    title      TEXT NOT NULL,
    fetched_ts TEXT NOT NULL,
    UNIQUE(symbol, news_date, title)
);
CREATE INDEX IF NOT EXISTS idx_stock_news_symbol ON stock_news(symbol);
CREATE INDEX IF NOT EXISTS idx_stock_news_id ON stock_news(id DESC);
"""


class Database:
    """單例:整個 app 共用一個 Database 物件。

    對外介面全部是 async,內部用 `asyncio.to_thread` 執行 sqlite3 同步呼叫。
    SQLite 連線使用 WAL 模式,允許多 reader 並行 + 單 writer。
    """

    def __init__(self, path: str = DB_PATH, cipher: Cipher | None = None):
        self.path = path
        self._cipher = cipher
        self._lock = asyncio.Lock()      # 序列化寫入(SQLite WAL 仍允許並行讀)
        self._init_done = False

    # ── 連線 helpers ─────────────────────────────────────────────────
    @contextlib.contextmanager
    def _conn(self):
        """同步開啟一個 sqlite3 連線。不要在 async 路徑直接呼叫,要用 to_thread。"""
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    # ── 初始化 ───────────────────────────────────────────────────────
    async def init(self) -> None:
        """建立 schema(如不存在)+ 寫入版本資訊。可多次呼叫(idempotent)。"""
        if self._init_done:
            return
        await asyncio.to_thread(self._init_sync)
        self._init_done = True

    def _init_sync(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA_SQL)
            existing = c.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                c.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    ("created_at", str(time.time())),
                )
        log.info("Database 初始化完成: %s", self.path)

    # ── 設定區段(每個 section 一筆 JSON) ────────────────────────────
    async def get_config_section(self, section: str) -> dict | None:
        return await asyncio.to_thread(self._get_section_sync, section)

    def _get_section_sync(self, section: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT payload FROM config WHERE section=?",
                (section,),
            ).fetchone()
            if row is None:
                return None
            try:
                return json.loads(row["payload"])
            except json.JSONDecodeError:
                log.error("config 區段 %s JSON 損毀", section)
                return None

    async def get_all_config(self) -> dict:
        """回傳 {section: data} 的 dict。"""
        return await asyncio.to_thread(self._get_all_config_sync)

    def _get_all_config_sync(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT section, payload FROM config").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["section"]] = json.loads(r["payload"])
            except json.JSONDecodeError:
                continue
        return out

    async def set_config_section(self, section: str, data: dict) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_section_sync, section, data)

    def _set_section_sync(self, section: str, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO config(section, payload, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(section) DO UPDATE SET
                  payload=excluded.payload, updated_at=excluded.updated_at""",
                (section, payload, now),
            )

    # ── 加密 secrets(email password、dashboard password 等) ─────────
    async def get_secret(self, key: str) -> str:
        """回傳解密後的 secret;不存在或解密失敗回空字串。"""
        return await asyncio.to_thread(self._get_secret_sync, key)

    def _get_secret_sync(self, key: str) -> str:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM secrets WHERE key=?", (key,),
            ).fetchone()
        if row is None:
            return ""
        ciphertext = row["value"]
        if self._cipher is None:
            return ciphertext   # 未啟用加密(理論上不會發生)
        return self._cipher.decrypt(ciphertext)

    async def set_secret(self, key: str, plaintext: str) -> None:
        """空字串會刪掉這筆 secret(=「移除密碼」)。"""
        async with self._lock:
            await asyncio.to_thread(self._set_secret_sync, key, plaintext)

    def _set_secret_sync(self, key: str, plaintext: str) -> None:
        with self._conn() as c:
            if not plaintext:
                c.execute("DELETE FROM secrets WHERE key=?", (key,))
                return
            ciphertext = (self._cipher.encrypt(plaintext)
                          if self._cipher is not None else plaintext)
            c.execute(
                """INSERT INTO secrets(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, ciphertext),
            )

    async def has_secret(self, key: str) -> bool:
        return await asyncio.to_thread(self._has_secret_sync, key)

    def _has_secret_sync(self, key: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM secrets WHERE key=?", (key,),
            ).fetchone()
        return row is not None

    # ── Slot analysis(整包 JSON;單一 row) ──────────────────────────
    async def load_slot_analysis(self) -> dict | None:
        return await asyncio.to_thread(self._load_slot_sync)

    def _load_slot_sync(self) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT payload FROM slot_analysis WHERE id=1",
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            log.error("slot_analysis JSON 損毀")
            return None

    async def save_slot_analysis(self, sa: dict) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_slot_sync, sa)

    def _save_slot_sync(self, sa: dict) -> None:
        payload = json.dumps(sa, ensure_ascii=False)
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO slot_analysis(id, payload, updated_at) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  payload=excluded.payload, updated_at=excluded.updated_at""",
                (payload, now),
            )

    # ── History(每筆一 row) ──────────────────────────────────────
    async def load_history(self, limit: int | None = None) -> list[dict]:
        return await asyncio.to_thread(self._load_history_sync, limit)

    def _load_history_sync(self, limit: int | None) -> list[dict]:
        with self._conn() as c:
            if limit:
                # 取最後 N 筆,再依時間升序回傳
                rows = c.execute(
                    "SELECT * FROM (SELECT ts, bet, before_balance, after_balance, "
                    "change_value, result, lines_json FROM history "
                    "ORDER BY id DESC LIMIT ?) ORDER BY ts ASC",
                    (int(limit),),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT ts, bet, before_balance, after_balance, "
                    "change_value, result, lines_json "
                    "FROM history ORDER BY id ASC"
                ).fetchall()
        out = []
        for r in rows:
            try:
                lines = json.loads(r["lines_json"]) if r["lines_json"] else []
            except json.JSONDecodeError:
                lines = []
            out.append({
                "ts":     r["ts"],
                "bet":    r["bet"],
                "before": r["before_balance"],
                "after":  r["after_balance"],
                "change": r["change_value"],
                "result": r["result"],
                "lines":  lines,
            })
        return out

    async def append_history(self, record: dict) -> None:
        """新增一筆下注紀錄。並維持表 size <= HISTORY_MAX_LEN(刪掉最舊的)。"""
        async with self._lock:
            await asyncio.to_thread(self._append_history_sync, record)

    def _append_history_sync(self, record: dict) -> None:
        lines_json = (json.dumps(record.get("lines", []), ensure_ascii=False)
                      if record.get("lines") else None)
        with self._conn() as c:
            c.execute(
                """INSERT INTO history
                (ts, bet, before_balance, after_balance, change_value, result, lines_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.get("ts"),
                    int(record.get("bet", 0)),
                    int(record.get("before", 0)),
                    int(record.get("after", 0)),
                    int(record.get("change", 0)),
                    record.get("result", ""),
                    lines_json,
                ),
            )
            # 修剪到 HISTORY_MAX_LEN — 每 50 筆才修剪一次,降低成本
            count = c.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            if count > HISTORY_MAX_LEN + 50:
                excess = count - HISTORY_MAX_LEN
                c.execute(
                    "DELETE FROM history WHERE id IN "
                    "(SELECT id FROM history ORDER BY id ASC LIMIT ?)",
                    (excess,),
                )

    async def clear_history(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._clear_history_sync)

    def _clear_history_sync(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM history")
            c.execute("DELETE FROM sqlite_sequence WHERE name='history'")

    async def reset_slot_analysis(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._reset_slot_sync)

    def _reset_slot_sync(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM slot_analysis WHERE id=1")

    # ── 批次 import(用於從 JSON 一次性遷移) ─────────────────────────
    async def bulk_import_history(self, records: Iterable[dict]) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._bulk_import_sync, list(records))

    def _bulk_import_sync(self, records: list[dict]) -> int:
        if not records:
            return 0
        rows = []
        for r in records:
            lines_json = (json.dumps(r.get("lines", []), ensure_ascii=False)
                          if r.get("lines") else None)
            rows.append((
                r.get("ts"),
                int(r.get("bet", 0)),
                int(r.get("before", 0)),
                int(r.get("after", 0)),
                int(r.get("change", 0)),
                r.get("result", ""),
                lines_json,
            ))
        with self._conn() as c:
            c.executemany(
                """INSERT INTO history
                (ts, bet, before_balance, after_balance, change_value, result, lines_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    # ── Stock prices ─────────────────────────────────────────────────
    async def append_stock_prices(self, ts: str, prices: dict[str, float]) -> int:
        """批次寫入單次抓到的所有股票價格。回傳寫入筆數。"""
        if not prices:
            return 0
        async with self._lock:
            return await asyncio.to_thread(self._append_stock_prices_sync, ts, prices)

    def _append_stock_prices_sync(self, ts: str, prices: dict[str, float]) -> int:
        rows = [(ts, sym, float(p)) for sym, p in prices.items()]
        with self._conn() as c:
            c.executemany(
                "INSERT INTO stock_prices (ts, symbol, price) VALUES (?, ?, ?)",
                rows,
            )
        return len(rows)

    async def load_stock_history(
        self, symbol: str | None = None, limit: int = 500,
    ) -> list[dict]:
        """讀取股票歷史。symbol=None 表全部 symbols。"""
        return await asyncio.to_thread(self._load_stock_history_sync, symbol, limit)

    def _load_stock_history_sync(self, symbol, limit):
        with self._conn() as c:
            if symbol:
                rows = c.execute(
                    "SELECT ts, symbol, price FROM stock_prices "
                    "WHERE symbol=? ORDER BY id DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT ts, symbol, price FROM stock_prices "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in reversed(rows)]   # 升序回傳

    async def upsert_stock_holding(
        self, symbol: str, shares: float, avg_cost: float, ts: str,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._upsert_holding_sync, symbol, shares, avg_cost, ts,
            )

    def _upsert_holding_sync(self, symbol, shares, avg_cost, ts):
        with self._conn() as c:
            c.execute(
                """INSERT INTO stock_holdings (symbol, shares, avg_cost, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    shares=excluded.shares,
                    avg_cost=excluded.avg_cost,
                    last_seen=excluded.last_seen""",
                (symbol, float(shares), float(avg_cost), ts),
            )

    # ── 股票新聞 ─────────────────────────────────────────────────────
    async def migrate_news_dates_to_iso_if_needed(self) -> int:
        """一次性把舊 stock_news.news_date 從 `YYYY/M/D` 變 `YYYY-MM-DD`,
        字串排序才正確。跑過一次後 meta 記錄,不重複跑。回傳 migrate 筆數。
        """
        already = await self.get_meta("news_dates_migrated_v1")
        if already:
            return 0
        n = await asyncio.to_thread(self._migrate_news_dates_sync)
        await self.set_meta("news_dates_migrated_v1", "1")
        return n

    def _migrate_news_dates_sync(self) -> int:
        import re as _re
        import sqlite3 as _sq
        n = 0
        with self._conn() as c:
            # 1) 刪除 date 空白(舊版 parser 抓不到 date 留下的爛資料,UI
            #    會 fallback 到 fetched_ts 顯示成 yyyy-mm-dd 假日期)
            c.execute(
                "DELETE FROM stock_news WHERE news_date IS NULL "
                "OR TRIM(news_date) = ''"
            )
            n += c.rowcount

            # 2) Normalize YYYY/M/D 變 ISO YYYY-MM-DD
            rows = c.execute(
                "SELECT id, news_date FROM stock_news"
            ).fetchall()
            for r in rows:
                raw = r["news_date"] or ""
                if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                    continue
                parts = _re.split(r"[/\-]", raw.strip())
                if len(parts) != 3:
                    continue
                try:
                    iso = (f"{int(parts[0]):04d}-"
                           f"{int(parts[1]):02d}-{int(parts[2]):02d}")
                except ValueError:
                    continue
                try:
                    c.execute(
                        "UPDATE stock_news SET news_date=? WHERE id=?",
                        (iso, r["id"]),
                    )
                    n += 1
                except _sq.IntegrityError:
                    # UNIQUE(symbol, news_date, title) — 若 normalize 後跟
                    # 既有重複,直接刪掉這筆
                    c.execute("DELETE FROM stock_news WHERE id=?", (r["id"],))
                    n += 1
        return n

    async def upsert_news_items(self, items: list[dict]) -> list[dict]:
        """寫入新聞。回傳「真的新加入」的 items(UNIQUE constraint 擋掉的不算)。

        每個 item 至少要有 symbol / date / title。fetched_ts 若無自動補當下時間。
        """
        if not items:
            return []
        async with self._lock:
            return await asyncio.to_thread(self._upsert_news_sync, items)

    def _upsert_news_sync(self, items: list[dict]) -> list[dict]:
        import sqlite3 as _sq
        from datetime import datetime as _dt
        now_str = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        new_items: list[dict] = []
        with self._conn() as c:
            for it in items:
                sym = (it.get("symbol") or "").upper()
                date = it.get("date") or ""
                title = it.get("title") or ""
                fetched = it.get("fetched_ts") or now_str
                if not sym or not title:
                    continue
                try:
                    c.execute(
                        "INSERT INTO stock_news "
                        "(symbol, news_date, title, fetched_ts) "
                        "VALUES (?, ?, ?, ?)",
                        (sym, date, title, fetched),
                    )
                    new_items.append({
                        "symbol": sym, "date": date,
                        "title": title, "fetched_ts": fetched,
                    })
                except _sq.IntegrityError:
                    pass    # 已存在
        return new_items

    async def load_recent_news(
        self, limit: int = 20, symbol: str | None = None,
    ) -> list[dict]:
        """讀最近的新聞。symbol=None 表跨所有 sym。按 id desc(最新先)。"""
        return await asyncio.to_thread(self._load_news_sync, limit, symbol)

    def _load_news_sync(self, limit, symbol):
        # 跨 sym 排序:news_date DESC(ISO YYYY-MM-DD 字串排序正確)→ id DESC
        # 確保「最新日期的新聞」優先,避免某 sym 全部 id 高就霸佔列表。
        with self._conn() as c:
            if symbol:
                rows = c.execute(
                    "SELECT symbol, news_date, title, fetched_ts FROM stock_news "
                    "WHERE symbol=? "
                    "ORDER BY news_date DESC, id DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT symbol, news_date, title, fetched_ts FROM stock_news "
                    "ORDER BY news_date DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    async def load_stock_holdings(self) -> list[dict]:
        return await asyncio.to_thread(self._load_holdings_sync)

    def _load_holdings_sync(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT symbol, shares, avg_cost, last_seen FROM stock_holdings "
                "WHERE shares > 0 ORDER BY symbol"
            ).fetchall()
        return [dict(r) for r in rows]

    async def clear_stock_holdings(self) -> None:
        """清空所有 holdings(每次抓 portfolio 後重寫,避免賣掉的還在裡面)。"""
        async with self._lock:
            await asyncio.to_thread(self._clear_holdings_sync)

    def _clear_holdings_sync(self):
        with self._conn() as c:
            c.execute("DELETE FROM stock_holdings")

    # ── Meta ─────────────────────────────────────────────────────────
    async def get_meta(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get_meta_sync, key)

    def _get_meta_sync(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_meta_sync, key, value)

    def _set_meta_sync(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )


# ── 全域單例 ──────────────────────────────────────────────────────────
_INSTANCE: Database | None = None


def get_db() -> Database:
    if _INSTANCE is None:
        raise RuntimeError("Database 尚未初始化,請先呼叫 init_db()")
    return _INSTANCE


async def init_db(cipher: Cipher | None = None, path: str = DB_PATH) -> Database:
    """初始化全域 db 實例。callers(主要是 main.py)只呼叫一次。"""
    global _INSTANCE
    _INSTANCE = Database(path, cipher)
    await _INSTANCE.init()
    return _INSTANCE
