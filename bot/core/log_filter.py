"""Log 敏感資訊遮罩。

掛在 logging handler 上,把 password / token / secret 等關鍵字後面的值
遮成 ***。同時提供 redact_text() 給 dashboard log viewer 用。

範例:
  原:  "smtp login user=foo@gmail.com pwd=abcd1234"
  遮:  "smtp login user=foo@gmail.com pwd=***"
"""
from __future__ import annotations

import logging
import re

# 被遮罩的關鍵字(後面跟 = / : / 空白後接 非空字串)
# 注意 (?i) 案例不敏感
_SENSITIVE_PATTERNS = [
    # password / pwd / pass / pw / token / secret / api_key / apikey / authorization
    re.compile(
        r"(?i)\b(password|pwd|passwd|pass|api[_-]?key|secret|token|"
        r"authorization|auth|bearer)\s*[:=]\s*['\"]?([^\s'\",;]+)['\"]?"
    ),
]
# Authorization Header 風格:Authorization: Bearer xxxxx
_BEARER_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)([^\s'\",;]+)"
)
# 高熵字串(40+ 連續英數)疑似 token,也一併遮掉
# 但避免誤殺 Discord ID(都是純數字,不會超過 20 位)
_LONG_TOKEN_PATTERN = re.compile(r"\b([A-Za-z0-9_\-]{40,})\b")


def redact_text(text: str) -> str:
    """把 password / token / secret 等敏感欄位的值替換成 ***。"""
    if not text:
        return text

    def _replace_kv(m: re.Match) -> str:
        return f"{m.group(1)}=***"

    out = _SENSITIVE_PATTERNS[0].sub(_replace_kv, text)
    out = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)}***", out)
    # 不對 token-like 長字串自動遮罩,因為 Discord 訊息中可能有 emoji shortcode
    # 或其他長字串,容易誤殺;只針對明確的 key=value 格式遮
    return out


class RedactingFormatter(logging.Formatter):
    """繼承 logging.Formatter 並在輸出前 redact。"""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return redact_text(msg)
