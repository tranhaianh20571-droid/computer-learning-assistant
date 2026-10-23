"""Local bridge to the character-card market: binding, batch upload and import."""
from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .auth.service import AuthError
from .characters.package import export_package, import_package
from .market_link import BindingStore, MarketClient, MarketError, bind_start
from .market_receiver import MarketReceiver


class ReceptionIn(BaseModel):
    enabled: bool


class UploadIn(BaseModel):
    card_ids: list[str] = Field(min_length=1, max_length=20)


class ImportIn(BaseModel):
    card_id: str = Field(min_length=1, max_length=64)


class LocalLoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


def create_market_router(cards, resolve_user, base_url: str | None = None, auth=None) -> APIRouter:
    if not base_url:
        from .infrastructure.config import settings
        base_url = settings.MARKET_BASE_URL
    base_url = base_url.rstrip("/")
    router = APIRouter(prefix="/api/market", tags=["market"])
    store = BindingStore()
    client = MarketClient(base_url)
    receiver = MarketReceiver(cards, store)
    router.reception = receiver

    def owner(request: Request) -> str:
        return resolve_user(request).user_id

    def current_binding(request: Request) -> tuple[str, dict]:
        user = owner(request)
        item = store.get(user)
        if not item:
            raise MarketError("BIND_REQUIRED", 409, "请先登录市集账号。")
        return user, item

    def sync_profile(user: str, item: dict, profile: dict) -> dict:
        if profile.get("user_id") != item["market_user_id"] or auth is None:
            raise MarketError("BINDING_INVALID", 409, "市集账号不匹配，请重新登录。")
        try:
            auth.sync_market_profile(user, profile["email"], profile.get("display_name") or "")
        except AuthError as exc:
            raise MarketError(exc.code, exc.status, "本地账号不可用，请重新登录。") from None
        return {"display_name": profile.get("display_name") or ""}

    @router.get("/profile")
    def market_profile(request: Request, response: Response) -> dict:
        user, item = current_binding(request)
        profile = MarketClient(item["base_url"] or base_url).profile(item["token"])
        response.headers["Cache-Control"] = "private, no-store"
        return sync_profile(user, item, profile)

    @router.post("/web-login")
    def market_web_login(request: Request, response: Response) -> dict:
        user, item = current_binding(request)
        if item.get("purpose") == "receive":
            raise MarketError("RECEIVE_ONLY", 403, "请先登录同一市集账号，再打开市集。")
        target = (item["base_url"] or base_url).rstrip("/")
        result = MarketClient(target).web_login(item["token"])
        profile = sync_profile(user, item, result["user"])
        response.headers["Cache-Control"] = "private, no-store"
        return {**profile, "url": target + "/#login=" + quote(result["ticket"], safe="")}

    @router.get("/binding")
    def binding(request: Request) -> dict:
        user = owner(request)
        try:
            item = store.get(user)
        except MarketError as exc:
            return {"bound": False, "error": exc.message}
        if not item:
            return {"bound": False, "base_url": base_url}
        return {"bound": True, "market_user_id": item["market_user_id"], "base_url": item["base_url"],
                "receiving": item["enabled"], "receive_only": item.get("purpose") == "receive"}

    def open_session(response: Response, market_user: dict, request: Request) -> dict:
        if auth is None:
            raise MarketError("AUTH_UNAVAILABLE", 503, "本机会话服务不可用。")
        try:
            raw, local_user_id = auth.issue_market_session(
                market_user.get("user_id") or market_user.get("id") or "",
                market_user.get("email") or "",
                market_user.get("display_name") or "",
            )
        except AuthError as exc:
            raise MarketError(exc.code, exc.status, "本地账号不可用，请检查账号状态。") from None
        # Secure must follow the LOCAL request scheme; the market URL is always
        # https, and marking the cookie Secure on an http://127.0.0.1 page makes
        # browsers drop it, which looks like "please log in again".
        response.set_cookie("session_token", raw, httponly=True, samesite="lax", path="/",
                            secure=request.url.scheme == "https")
        return {"user_id": local_user_id,
                "market_user_id": market_user.get("user_id") or market_user.get("id"),
                "email": market_user.get("email"),
                "display_name": market_user.get("display_name") or ""}

    @router.post("/login")
    def local_login(body: LocalLoginIn, response: Response, request: Request) -> dict:
        """Desktop login: verify against the market, bind this device, open a local session."""
        market_user, token = client.login(body.email, body.password)
        session = open_session(response, market_user, request)
        store.save(session["user_id"], str(session.get("market_user_id") or ""), base_url, token)
        return session

    @router.post("/bind/start")
    def start(request: Request, purpose: Literal["login", "receive"] = "login") -> dict:
        # First launch has no account yet; the binding code itself is the capability.
        result = bind_start(base_url)
        if purpose == "receive":
            store.receive_challenge(result["code"], complete_owner="")
            result["url"] = base_url + "/#receive=" + result["code"]
        return result

    @router.get("/bind/status")
    def status(request: Request, response: Response, code: str,
               purpose: Literal["login", "receive"] = "login") -> dict:
        if purpose == "login" and store.receive_challenge(code):
            raise MarketError("BIND_PURPOSE_INVALID", 409, "接收授权不能用于本机登录。")
        if purpose == "receive":
            challenge = store.receive_challenge(code)
            if not challenge:
                raise MarketError("BIND_INVALID", 410, "授权链接已过期，请重新开启接收。")
            if challenge.get("owner"):
                try:
                    same = owner(request) == challenge["owner"]
                except HTTPException:
                    same = False
                return {"bound": True, "status": "confirmed", "purpose": "receive", "current_account": same}
        try:
            result = client.bind_status(code)
        except MarketError as exc:
            if exc.status in (404, 410):
                return {"bound": False, "status": "pending"}
            raise
        if result.get("status") != "confirmed":
            return {"bound": False, "status": "pending"}
        if purpose == "login" and result.get("purpose") == "receive":
            raise MarketError("BIND_PURPOSE_INVALID", 409, "接收授权不能用于本机登录。")
        if purpose == "receive":
            if auth is None:
                raise MarketError("AUTH_UNAVAILABLE", 503, "本地账号服务不可用。")
            if result.get("purpose") != "receive":
                raise MarketError("BIND_PURPOSE_INVALID", 409, "请从账号接收页面重新授权。")
            try:
                local_owner = auth.ensure_market_identity(result["user_id"], result["email"], result.get("display_name", ""))
            except AuthError as exc:
                raise MarketError(exc.code, exc.status, "本地账号不可用，请检查账号状态。") from None
            cards.audit.append("connector.bound", actor_id=local_owner, result="success", object_type="market_reception")
            store.save(local_owner, result["user_id"], base_url, result["token"], purpose="receive")
            store.receive_challenge(code, complete_owner=local_owner)
            try:
                same = owner(request) == local_owner
            except HTTPException:
                same = False
            return {"bound": True, "status": "confirmed", "purpose": "receive", "current_account": same}
        session_user = open_session(response, result, request)
        store.save(session_user["user_id"], result["user_id"], base_url, result["token"])
        return {"bound": True, "status": "confirmed",
                "market_user_id": result["user_id"], "user": session_user}

    @router.post("/upload")
    def upload(request: Request, body: UploadIn) -> dict:
        user = owner(request)
        item = store.get(user)
        if not item:
            raise MarketError("BIND_REQUIRED", 409, "请先绑定市集账号。")
        client = MarketClient(item["base_url"] or base_url)
        results = []
        for card_id in body.card_ids:
            try:
                view = cards.get(user, card_id)
                profile = view["profile"]
                package = export_package(cards, user, card_id)
                cover = None
                asset_id = profile.get("base_asset_id")
                asset = next((a for a in view.get("assets", []) if a.get("asset_id") == asset_id
                              and a.get("kind") == "image"), None)
                if not asset:
                    asset = next((a for a in view.get("assets", []) if a.get("kind") == "image"), None)
                if asset:
                    payload, mime = cards.asset(user, card_id, asset["asset_id"])
                    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".png")
                    cover = ("cover" + suffix, payload, mime)
                uploaded = client.upload(item["token"], package, cover,
                                         str(profile.get("name") or "未命名人物"), list(profile.get("tags") or []))
                results.append({"card_id": card_id, "status": "uploaded",
                                "market_card_id": uploaded.get("card_id")})
            except MarketError as exc:
                results.append({"card_id": card_id,
                                "status": "duplicate" if exc.code == "DUPLICATE_CARD" else "failed",
                                "market_card_id": exc.detail.get("card_id"),
                                "message": exc.message})
            except Exception:
                results.append({"card_id": card_id, "status": "failed",
                                "message": "卡片导出或上传失败。"})
        return {"items": results}

    @router.post("/import", status_code=201)
    def import_card(request: Request, body: ImportIn) -> dict:
        user = owner(request)
        item = store.get(user)
        if not item:
            raise MarketError("BIND_REQUIRED", 409, "请先绑定市集账号。")
        client = MarketClient(item["base_url"] or base_url)
        payload = client.download(item["token"], body.card_id)
        return import_package(cards, user, payload)

    @router.post("/imports/drain")
    def drain_imports(request: Request) -> dict:
        """Compatibility endpoint: read only this account's durable import notifications."""
        user = owner(request)
        item = store.get(user)
        if not item:
            return {"bound": False, "imported": 0}
        return {"bound": True, **receiver.notices(user)}

    @router.get("/reception")
    def reception_status(request: Request) -> dict:
        user = owner(request)
        item = store.get(user)
        return {"authorized": bool(item), "enabled": bool(item and item["enabled"]),
                "error_code": receiver.errors.get(user), "base_url": base_url}

    @router.patch("/reception")
    def reception_configure(request: Request, body: ReceptionIn) -> dict:
        receiver.configure(owner(request), enabled=body.enabled)
        return reception_status(request)

    @router.delete("/reception")
    def reception_revoke(request: Request) -> dict:
        receiver.configure(owner(request), revoke=True)
        return reception_status(request)

    @router.get("/cards")
    def market_cards(request: Request) -> dict:
        owner(request)
        try:
            data = client.cards()
        except MarketError as exc:
            return {"data": [], "error": exc.message}
        return {"data": [dict(item, cover_url=base_url + item["cover_url"]) for item in data]}

    return router
