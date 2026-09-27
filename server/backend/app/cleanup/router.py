"""切片 5 API：在线清理、备份轮换、恢复锁（管理员）。"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Request

from ..planning.errors import error
from .service import CleanupService


def create_cleanup_router(
    *,
    cleanup: CleanupService,
    resolve_user: Callable[[Request], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["slice5"])

    def _admin(request: Request):
        user = resolve_user(request)
        if not getattr(user, "is_admin", False):
            raise error("admin_only", "admin only", status=403)
        return user

    @router.post("/cleanup/run")
    def run_cleanup(request: Request) -> dict:
        _admin(request)
        return cleanup.run_online_cleanup()

    @router.post("/backup")
    def create_backup(request: Request) -> dict:
        _admin(request)
        return cleanup.create_backup()

    @router.post("/restore/begin")
    def restore_begin(request: Request) -> dict:
        user = _admin(request)
        return cleanup.begin_restore(operator_id=user.user_id)

    @router.post("/restore/replay")
    def restore_replay(request: Request) -> dict:
        user = _admin(request)
        return cleanup.replay_tombstones(operator_id=user.user_id)

    @router.post("/restore/verify")
    def restore_verify(request: Request) -> dict:
        user = _admin(request)
        return cleanup.verify_and_unlock(operator_id=user.user_id)

    @router.get("/restore/state")
    def restore_state(request: Request) -> dict:
        _admin(request)
        return {"restore_locked": cleanup.is_restore_locked()}

    return router
