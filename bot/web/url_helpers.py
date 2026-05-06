"""Dashboard URL helpers(LAN IP 偵測 / 本機 / LAN URL)。"""
from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.core.config import BotConfig

log = logging.getLogger(__name__)

_lan_ip_cache: list[str] = []


def detect_lan_ip() -> str:
    """嘗試找本機 LAN IPv4;連不上回 'localhost'。Cache 結果避免重複呼叫。"""
    if _lan_ip_cache:
        return _lan_ip_cache[0]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except (OSError, socket.timeout, socket.gaierror) as e:
        log.debug("無法偵測 LAN IP: %s", e)
        ip = "localhost"
    _lan_ip_cache.append(ip)
    return ip


def dashboard_local_url(config: "BotConfig") -> str:
    return f"http://127.0.0.1:{config.dashboard.port}/"


def dashboard_lan_url(config: "BotConfig") -> str:
    host = config.dashboard.host
    port = config.dashboard.port
    if host == "0.0.0.0":
        ip = detect_lan_ip()
    else:
        ip = host
    return f"http://{ip}:{port}/"
