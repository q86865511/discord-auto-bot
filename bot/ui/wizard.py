"""首次啟動 / 必填欄位缺失時的引導精靈。

由 main.py 在 boot 時呼叫,遇到 guild_id / channel_id / notify_user_id 任一缺失
就要求使用者填入。Dashboard 啟用但無密碼也會強制處理(設密碼或退到 127.0.0.1)。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bot.ui.input_validation import ainput, ask_user_id

if TYPE_CHECKING:
    from bot.core.config import BotConfig

log = logging.getLogger(__name__)


async def first_run_wizard(config: BotConfig) -> bool:
    """首次啟動 / 必填欄位缺失時引導使用者填入。回傳是否完成。

    必填欄位:guild_id / channel_id / notify_user_id。
    Dashboard 密碼若空字串會強制要求(可選擇空+127.0.0.1)。
    """
    needed = []
    if not config.guild_id:
        needed.append("guild_id")
    if not config.channel_id:
        needed.append("channel_id")
    if not config.gambling.notify_user_id:
        needed.append("notify_user_id")

    pwd_missing = (config.dashboard.enabled
                   and not (config.dashboard.password or "").strip())

    if not needed and not pwd_missing:
        return True

    print()
    print("=" * 64)
    print("  🛠️  首次設定 — 請填入下列資訊(按 Enter 跳過保留現值)")
    print("=" * 64)
    print()
    print("  📌 開啟 Discord 開發者模式:使用者設定 → 進階 → 啟用「開發者模式」")
    print("      之後右鍵伺服器/頻道/使用者就會多出「複製 ID」選項")
    print()

    if "guild_id" in needed:
        print(f"  【伺服器 ID】(目前: {config.guild_id or '未設定'})")
        print("    → 對伺服器右鍵 → 複製伺服器 ID")
        v = await ask_user_id("    伺服器 ID", config.guild_id)
        if v: config.guild_id = v

    if "channel_id" in needed:
        print(f"\n  【頻道 ID】(目前: {config.channel_id or '未設定'}) — bot 會在此頻道送指令")
        print("    → 對要操作的頻道右鍵 → 複製頻道 ID")
        v = await ask_user_id("    頻道 ID", config.channel_id)
        if v: config.channel_id = v

    if "notify_user_id" in needed:
        print(f"\n  【通知對象 User ID】(目前: {config.gambling.notify_user_id or '未設定'})")
        print("    → 達成目標 / 貓娘完成時要 @ 的對象(通常填自己)")
        print("    → 對使用者右鍵 → 複製使用者 ID")
        v = await ask_user_id("    User ID", config.gambling.notify_user_id)
        if v: config.gambling.notify_user_id = v

    if pwd_missing:
        print()
        print("=" * 64)
        print("  🔒 Dashboard 安全設定")
        print("=" * 64)
        print()
        print("  Dashboard 目前是「啟用」狀態。為了安全,必須設定密碼。")
        print("  若不設密碼,監聽位址將自動改為 127.0.0.1(只本機可存取)。")
        print()

        if config.dashboard.host == "0.0.0.0":
            print("  目前 host=0.0.0.0(同 LAN 可存取)— 強烈建議設密碼")
        print()
        # 強制流程:要嘛設密碼,要嘛退到 127.0.0.1
        for _ in range(3):
            raw = (await ainput("  新密碼(空白=不設,host 自動退到 127.0.0.1): ")).rstrip("\r\n")
            if raw:
                if len(raw) < 4:
                    print("  ⚠ 密碼太短(至少 4 字),請重設")
                    continue
                config.dashboard.password = raw
                print("  ✓ 密碼已設定")
                break
            else:
                if config.dashboard.host == "0.0.0.0":
                    config.dashboard.host = "127.0.0.1"
                    print("  ✓ host 已改為 127.0.0.1(只本機可存取)")
                else:
                    print("  ✓ 維持目前 host(無密碼)")
                break

    print("=" * 64)
    return True
