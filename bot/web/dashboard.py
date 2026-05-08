"""Web dashboard — 在 localhost (或 LAN) 顯示 bot 即時狀態 + 控制台。

Server lifecycle 入口。實際內容在 sibling modules:
    bot.web.templates    HTML / CSS / JS
    bot.web.snapshots    /api/state /api/analysis /api/logs 的資料組裝
    bot.web.handler      HTTP routing + auth + CSRF

安全強化(v2):
- HTTP Basic Auth 用 hmac.compare_digest 比對(防 timing attack)
- 0.0.0.0 + 無密碼 = 啟動失敗(由 main 層在 wizard 已修正,這裡再做最後保護)
- /api/config POST 走 schema 驗證(BotConfig.validate),拒絕不合理值
- /api/logs 過濾 password / token / secret 等敏感字
- HTML 主體經 escape;後端不再回傳含 HTML markup 的 string 欄位
- 加 CSRF 保護:所有 POST 必須有 Origin/Referer 與 Host 同源

純 Python stdlib(http.server + json),零外部依賴。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from bot.web.handler import ReusableTCPServer, make_handler

if TYPE_CHECKING:
    import asyncio

    from bot.core.config import BotConfig
    from bot.core.state import BotState

log = logging.getLogger("dashboard")


def start_dashboard_thread(
    state: BotState,
    config_provider: Callable[[], BotConfig],
    on_action: Callable[[str], dict],
    on_config_save_sync: Callable[[dict], dict],
    on_stock_trade: Callable[[str, str, int], dict] | None = None,
    host: str | None = None,
    port: int | None = None,
    main_loop: asyncio.AbstractEventLoop | None = None,
) -> threading.Thread | None:
    cfg = config_provider().dashboard
    host = host or cfg.host
    port = port or cfg.port

    # 安全保險:0.0.0.0 + 無密碼 → 拒絕啟動
    if host == "0.0.0.0" and not (cfg.password or "").strip():
        log.error("Dashboard 配置不安全(0.0.0.0 + 無密碼),拒絕啟動。"
                  "請設定密碼或改 host=127.0.0.1。")
        return None

    handler_cls = make_handler(state, config_provider, on_action,
                                on_config_save_sync, main_loop=main_loop,
                                on_stock_trade=on_stock_trade)
    try:
        server = ReusableTCPServer((host, port), handler_cls)
    except OSError as e:
        log.warning("dashboard 啟動失敗(port %d 被占用?): %s", port, e)
        return None

    if host == "0.0.0.0":
        from bot.web.url_helpers import detect_lan_ip
        log.info("dashboard 啟動 — 本機: http://127.0.0.1:%d/  / LAN: http://%s:%d/",
                 port, detect_lan_ip(), port)
    else:
        log.info("dashboard 啟動 — http://%s:%d/", host, port)

    def _serve() -> None:
        try:
            server.serve_forever(poll_interval=0.5)
        except Exception as e:    # noqa: BLE001
            log.error("dashboard server 例外: %s", e)
        finally:
            server.server_close()
            log.info("dashboard 已關閉")

    t = threading.Thread(target=_serve, daemon=True, name="dashboard")
    t.start()
    t._dashboard_server = server  # type: ignore[attr-defined]
    return t


def stop_dashboard_thread(t: threading.Thread | None) -> None:
    if t is None:
        return
    server = getattr(t, "_dashboard_server", None)
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:    # noqa: BLE001
        log.warning("關閉 dashboard 時發生錯誤", exc_info=True)
