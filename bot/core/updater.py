"""GitHub 版本檢查 — 用 git ls-remote 比對 HEAD vs origin/main。

設計:
- 不做 git fetch(會動到本地 reflog)。改用 ls-remote 直接讀遠端 ref。
- check_for_updates() 是純讀操作,不改本地 repo 狀態。
- perform_update() 才會跑 git pull --ff-only。
- 失敗(沒 git / 不在 repo / 網路掛了)都回 None,呼叫端自行 fallback。
"""
from __future__ import annotations

import logging
import os
from typing import NamedTuple

from bot.core.async_io import run_subprocess

log = logging.getLogger(__name__)


class UpdateStatus(NamedTuple):
    has_update: bool
    local_commit: str | None
    remote_commit: str | None
    error: str | None = None


def _is_git_repo() -> bool:
    return os.path.isdir(os.path.join(os.getcwd(), ".git"))


async def get_local_commit() -> str | None:
    """目前 HEAD 的 SHA。不是 git repo 或 git 不在 PATH 都回 None。"""
    if not _is_git_repo():
        return None
    rc, out, _ = await run_subprocess(
        ["git", "rev-parse", "HEAD"], timeout=10.0,
    )
    if rc == 0 and out:
        return out.strip()
    return None


async def get_remote_commit(branch: str = "main") -> str | None:
    """遠端 origin/<branch> 的最新 SHA。透過 git ls-remote(不會 fetch)。"""
    if not _is_git_repo():
        return None
    rc, out, _ = await run_subprocess(
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"], timeout=15.0,
    )
    if rc != 0 or not out:
        return None
    line = out.strip().split("\n", 1)[0]
    parts = line.split()
    return parts[0] if parts else None


async def check_for_updates(branch: str = "main") -> UpdateStatus:
    """單次檢查。不改 repo 狀態。"""
    if not _is_git_repo():
        return UpdateStatus(False, None, None, "not a git repo")

    local = await get_local_commit()
    remote = await get_remote_commit(branch)
    if local is None:
        return UpdateStatus(False, None, remote, "could not read local commit")
    if remote is None:
        return UpdateStatus(False, local, None, "could not read remote (network?)")
    return UpdateStatus(local != remote, local, remote, None)


async def perform_update(branch: str = "main") -> tuple[bool, str]:
    """git pull --ff-only。回傳 (success, message)。

    --ff-only 意思:本地 commit 領先或不能 fast-forward 都不會做 merge,
    避免在使用者本地有 uncommitted changes 時把 repo 弄亂。
    """
    if not _is_git_repo():
        return False, "not a git repo"
    rc, out, err = await run_subprocess(
        ["git", "pull", "--ff-only", "origin", branch], timeout=60.0,
    )
    output = ((out or "") + (err or "")).strip()
    if rc != 0:
        return False, f"git pull 失敗(rc={rc}): {output[:200]}"
    return True, output[:200]
