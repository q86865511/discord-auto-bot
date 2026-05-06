"""Email 通知 — async wrapper for smtplib。

設計:
- 內部用 smtplib(stdlib);非同步介面是把 blocking call 包進 to_thread
- 失敗時記具體錯誤類別(SMTP/timeout/auth),不再吞成單一 Exception
- caller 用 send_email(email_cfg, subject, body) 即可,設定 disabled 自動 noop
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import socket
from email.mime.text import MIMEText
from typing import Any

log = logging.getLogger(__name__)


def _send_sync(email_cfg: Any, subject: str, body: str) -> bool:
    """blocking 寄信。回傳是否成功。

    `email_cfg` 可以是 EmailConfig dataclass、或 dict(向後相容)。
    """
    enabled = _g(email_cfg, "enabled", False)
    if not enabled:
        return False

    user = _g(email_cfg, "user", "") or ""
    pwd  = _g(email_cfg, "password", "") or ""
    to   = _g(email_cfg, "to", "") or ""
    if not (user and pwd and to):
        log.warning("Email 設定不完整(user/password/to 缺一),略過寄送")
        return False

    host = _g(email_cfg, "smtp_host", "smtp.gmail.com")
    port = int(_g(email_cfg, "smtp_port", 587))

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = user
        msg["To"]      = to
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(user, pwd)
            server.send_message(msg)
        log.info("Email 已寄出: %s", subject)
        return True
    except smtplib.SMTPAuthenticationError as e:
        # 不要 log 帳密!只 log error code 與 message
        log.error("SMTP 認證失敗(可能是 App Password 錯/未開二階): code=%s",
                  getattr(e, "smtp_code", "?"))
    except smtplib.SMTPRecipientsRefused as e:
        log.error("SMTP 收件人被拒: %s", getattr(e, "recipients", {}))
    except smtplib.SMTPException as e:
        log.error("SMTP 寄送失敗: %s", e)
    except (socket.gaierror, socket.timeout, OSError) as e:
        log.error("SMTP 網路錯誤: %s", e)
    except Exception as e:    # noqa: BLE001
        log.exception("SMTP 未知錯誤: %s", e)
    return False


def _g(obj: Any, key: str, default: Any) -> Any:
    """兼容 dict / dataclass。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


async def send_email(email_cfg: Any, subject: str, body: str) -> bool:
    """非同步寄信(在 executor 跑 blocking smtplib)。"""
    return await asyncio.to_thread(_send_sync, email_cfg, subject, body)
