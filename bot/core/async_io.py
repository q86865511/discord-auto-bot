"""把同步 I/O 包成非同步,避免阻塞 asyncio event loop。

把 `open()`, `os.path.*`, `subprocess.run` 之類的呼叫包成 await-able。
做法統一用 `asyncio.to_thread()`(Python 3.9+),不引入 aiofiles 等依賴。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any


async def read_text(path: str | Path, encoding: str = "utf-8") -> str:
    """非同步讀整檔。檔案不存在會丟 FileNotFoundError(讓 caller 處理)。"""
    return await asyncio.to_thread(_read_text_sync, path, encoding)


def _read_text_sync(path: str | Path, encoding: str) -> str:
    with open(path, encoding=encoding) as f:
        return f.read()


async def write_text(path: str | Path, content: str, encoding: str = "utf-8") -> None:
    """非同步原子寫入(寫到 .tmp 再 rename)。"""
    await asyncio.to_thread(_atomic_write_sync, path, content, encoding)


def _atomic_write_sync(path: str | Path, content: str, encoding: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding=encoding) as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)   # 原子替換


async def read_json(path: str | Path) -> Any:
    """非同步讀 JSON。失敗會丟對應 exception(json.JSONDecodeError / OSError)。"""
    text = await read_text(path)
    return json.loads(text)


async def write_json(path: str | Path, data: Any, indent: int = 2) -> None:
    """非同步原子寫 JSON。"""
    text = json.dumps(data, ensure_ascii=False, indent=indent, default=str)
    await write_text(path, text)


async def file_exists(path: str | Path) -> bool:
    return await asyncio.to_thread(os.path.exists, path)


async def file_size(path: str | Path) -> int | None:
    """檔案大小;不存在或無法取得回傳 None。"""
    def _get():
        try:
            return os.path.getsize(path)
        except OSError:
            return None
    return await asyncio.to_thread(_get)


async def remove_file(path: str | Path) -> bool:
    """安全刪除單一檔案;不存在或失敗回傳 False。"""
    def _del():
        try:
            os.remove(path)
            return True
        except OSError:
            return False
    return await asyncio.to_thread(_del)


async def run_subprocess(
    cmd: list[str], timeout: float = 60.0,
) -> tuple[int, str, str]:
    """非同步跑外部命令,回傳 (returncode, stdout, stderr)。"""
    import subprocess
    def _run():
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", timeout=timeout,
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired:
            return -1, "", f"TIMEOUT after {timeout}s"
        except FileNotFoundError:
            return -1, "", f"command not found: {cmd[0] if cmd else '?'}"
    return await asyncio.to_thread(_run)
