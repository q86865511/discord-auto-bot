"""Dashboard HTTP handler & threaded TCP server。

Auth(Basic + hmac.compare_digest)、CSRF check、route table。
從 dashboard.py 拆出來,讓 dashboard.py 只負責 server lifecycle。

公開符號:
    make_handler(state, config_provider, on_action, on_config_save_sync, main_loop=None)
        → BaseHTTPRequestHandler subclass
    ReusableTCPServer  threading + reuse_address 的 TCPServer
"""
from __future__ import annotations

import hmac
import http.server
import json
import logging
import re
import socketserver
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from bot.web.snapshots import (
    build_analysis_snapshot,
    build_state_snapshot,
    build_stocks_snapshot,
    build_strategies_snapshot,
    read_log_tail,
    run_in_main_loop,
)
from bot.web.templates import (
    ANALYSIS_BODY,
    CONTROL_BODY,
    LOGS_BODY,
    OVERVIEW_BODY,
    STOCKS_BODY,
    STRATEGIES_BODY,
    html_close,
    html_shell,
)

if TYPE_CHECKING:
    import asyncio

    from bot.core.config import BotConfig
    from bot.core.state import BotState

log = logging.getLogger("dashboard")


def make_handler(
    state: BotState,
    config_provider: Callable[[], BotConfig],
    on_action: Callable[[str], dict],
    on_config_save_sync: Callable[[dict], dict],
    main_loop: asyncio.AbstractEventLoop | None = None,
):
    """工廠 — 動態產出 BaseHTTPRequestHandler subclass。

    on_config_save_sync 接收 partial config dict,**同步**回傳
    {"ok": bool, "errors": [...], "warnings": [...]}.

    main_loop:主 event loop 的 reference。若提供,/api/state 與 /api/analysis
    會 schedule 一個 coroutine 到 main loop 內(在 state.lock 保護下)組 snapshot,
    然後透過 thread-safe future 拿回結果 — 確保 dashboard thread 不會讀到
    main loop 正在更新一半的 state。

    若 main_loop=None,fallback 直接讀 state(可能拿到不一致的快照)。

    Dashboard 在 thread 中跑,不能直接 await async function;改用 sync 包裝。
    """

    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        # 縮短 server header(避免洩漏 Python 版本)
        server_version = "DashboardSrv/1.0"
        sys_version = ""

        def log_message(self, fmt, *args):  # noqa: A002, N802
            log.debug(fmt, *args)

        # ── Auth ─────────────────────────────────────────────────────
        def _check_auth(self) -> bool:
            cfg = config_provider().dashboard
            pwd = (cfg.password or "").strip()
            if not pwd:
                # 沒設密碼 — 但若 host=0.0.0.0 我們在 server start 階段就會拒
                return True
            user = (cfg.username or "admin").strip() or "admin"

            auth_hdr = self.headers.get("Authorization", "")
            if not auth_hdr.startswith("Basic "):
                self._send_401()
                return False
            import base64
            try:
                decoded = base64.b64decode(auth_hdr[6:]).decode("utf-8", "replace")
                u, _sep, p = decoded.partition(":")
            except Exception:    # noqa: BLE001
                self._send_401()
                return False
            # hmac.compare_digest:恆定時間比對,防 timing attack
            if not (hmac.compare_digest(u, user) and hmac.compare_digest(p, pwd)):
                self._send_401()
                return False
            return True

        def _send_401(self):
            body = b"Unauthorized"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="DiscordBot"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ── CSRF / Origin 檢查(POST 才需要) ────────────────────────
        def _check_csrf(self) -> bool:
            """檢查 Origin / Referer 是否與 Host 同源。

            原則:Same-Origin Policy + 額外要求 X-Requested-With Header
            (XHR / fetch 自動帶,form-submit 不會帶 → 防 CSRF form attack)。
            """
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin", "")
            referer = self.headers.get("Referer", "")
            xrw = self.headers.get("X-Requested-With", "")

            # 必須是 fetch/XHR(瀏覽器跨站 form 不會自動帶 X-Requested-With)
            if not xrw:
                return False

            # Origin / Referer 必須是 http://<host>...
            for h in (origin, referer):
                if not h:
                    continue
                # 只取 scheme://host:port 部分比對
                m = re.match(r"^(https?)://([^/]+)", h)
                if not m:
                    return False
                if m.group(2).lower() != host.lower():
                    return False
            # 至少要有一個來源 header
            return bool(origin or referer)

        # ── GET routing ──────────────────────────────────────────────
        def do_GET(self):  # noqa: N802
            if not self._check_auth():
                return
            path = self.path.split("?", 1)[0]

            routes_html = {
                "/":          ("概覽",      OVERVIEW_BODY, "overview"),
                "/index.html":("概覽",      OVERVIEW_BODY, "overview"),
                "/analysis":  ("Slot 分析", ANALYSIS_BODY, "analysis"),
                "/strategies":("策略 Backtest", STRATEGIES_BODY, "strategies"),
                "/stocks":    ("股票",      STOCKS_BODY, "stocks"),
                "/control":   ("系統設定",  CONTROL_BODY, "control"),
                "/logs":      ("即時日誌",  LOGS_BODY, "logs"),
            }
            if path in routes_html:
                title, body, active = routes_html[path]
                self._html(html_shell(title, body, active) + html_close())
                return

            if path == "/api/logs":
                lines = read_log_tail()
                self._json({"lines": lines, "count": len(lines)})
                return
            if path == "/api/state":
                self._json(run_in_main_loop(
                    main_loop, state, build_state_snapshot, state, config_provider()
                ))
                return
            if path == "/api/analysis":
                self._json(run_in_main_loop(
                    main_loop, state, build_analysis_snapshot, state
                ))
                return
            if path == "/api/strategies":
                self._json(run_in_main_loop(
                    main_loop, state, build_strategies_snapshot,
                    state, config_provider(),
                ))
                return
            if path == "/api/stocks":
                self._json(run_in_main_loop(
                    main_loop, state, build_stocks_snapshot,
                    state, config_provider(),
                ))
                return
            if path == "/api/config":
                cfg = config_provider().to_redacted_dict()
                self._json(cfg)
                return
            self._respond(404, "text/plain", b"Not Found")

        def do_POST(self):  # noqa: N802
            if not self._check_auth():
                return
            if not self._check_csrf():
                self._json({"ok": False, "message": "請求被拒(CSRF/Origin 檢查失敗)"}, code=403)
                return

            path = self.path.split("?", 1)[0]
            if path.startswith("/api/action/"):
                action = path[len("/api/action/"):]
                # 白名單
                if action not in ("toggle_pause", "reset_analysis", "restart"):
                    self._json({"ok": False, "message": f"未知動作: {action}"}, code=400)
                    return
                try:
                    result = on_action(action)
                    self._json(result)
                except Exception as e:    # noqa: BLE001
                    log.exception("action %s 失敗", action)
                    self._json({"ok": False, "message": f"錯誤: {e}"}, code=500)
                return

            if path == "/api/config":
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 64 * 1024:    # 64KB 上限
                    self._json({"ok": False, "message": "payload 過大"}, code=413)
                    return
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except json.JSONDecodeError as e:
                    self._json({"ok": False, "message": f"JSON 錯誤: {e}"}, code=400)
                    return
                if not isinstance(payload, dict):
                    self._json({"ok": False, "message": "payload 必須是 dict"}, code=400)
                    return
                _strip_invalid_numbers(payload)
                # 不允許 dashboard / email 修改密碼或 host(防鎖死自己)
                if "dashboard" in payload:
                    payload["dashboard"].pop("password", None)
                    payload["dashboard"].pop("host", None)
                    payload["dashboard"].pop("port", None)
                if "email" in payload:
                    payload["email"].pop("password", None)
                # 也不允許改 guild_id / channel_id
                payload.pop("guild_id", None)
                payload.pop("channel_id", None)

                try:
                    result = on_config_save_sync(payload)
                    if result.get("ok"):
                        self._json(result)
                    else:
                        self._json(result, code=400)
                except Exception as e:    # noqa: BLE001
                    log.exception("儲存設定失敗")
                    self._json({"ok": False, "message": f"儲存失敗: {e}"}, code=500)
                return

            self._respond(404, "text/plain", b"Not Found")

        # ── Response helpers ─────────────────────────────────────────
        def _html(self, body: str) -> None:
            self._respond(200, "text/html; charset=utf-8", body.encode("utf-8"))

        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self._respond(code, "application/json; charset=utf-8", body)

        def _respond(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


def _strip_invalid_numbers(d: Any) -> None:
    if isinstance(d, dict):
        for k in list(d.keys()):
            v = d[k]
            if isinstance(v, float) and v != v:    # NaN
                d[k] = None
            elif isinstance(v, dict):
                _strip_invalid_numbers(v)


class ReusableTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
