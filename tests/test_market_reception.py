"""Real local identity/card services and isolated market SQLite; all content synthetic."""
import threading
import time
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from backend.auth.models import LoginSessionRow, UserRow
from backend.auth.service import AuthService
from backend.characters.models import CharacterCardRow
from backend.characters.package import export_package
from backend.characters.schemas import Profile
from backend.market_link import BindingStore, MarketError
from backend.market_router import create_market_router
from backend.market_receiver import MarketReceiver
from test_market_bridge import env  # noqa: F401
from test_market_registration import market  # noqa: F401
from test_market_uploads import signed_client


@pytest.fixture
def reception(env, market, monkeypatch):
    cards, folder = env
    auth = AuthService(cards.db, cards.audit)
    store = BindingStore(folder / "market.json")
    suffix = uuid.uuid4().hex[:10]
    market_id = "synthetic-web-a-" + suffix
    web = signed_client(market, market_id)
    email = market_id + "@example.invalid"
    local_a = auth.ensure_market_identity(market_id, email, "Synthetic A")
    token = web.post("/api/devices").json()["token"]
    store.save(local_a, market_id, "https://market.invalid", token)
    raw_b, local_b = auth.issue_market_session("synthetic-b-" + suffix, "synthetic-b-" + suffix + "@example.invalid", "Synthetic B")
    source = cards.create(local_b, Profile(name="Synthetic transferable"))
    package = export_package(cards, local_b, source["card_id"])
    remote_card = "card_synthetic_remote"
    with market.db() as conn:
        conn.execute("INSERT INTO cards(id,owner_id,title,cover_name,package_name,package_sha,size,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (remote_card, market_id, "Synthetic remote", "cover.png", "package.zip", "synthetic", len(package), market.iso(market.now())))
    card_dir = market.STORAGE / remote_card
    card_dir.mkdir()
    (card_dir / "package.zip").write_bytes(package)

    class Client:
        fail_report = False
        downloads = 0
        def __init__(self, base_url):
            self.base_url = base_url
        def call(self, method, path, token=None, **kwargs):
            with TestClient(market.app, base_url="https://market.invalid") as http:
                response = http.request(method, path, headers={"Authorization": "Bearer " + token} if token else {}, **kwargs)
            if response.status_code >= 400:
                detail = response.json().get("detail", {})
                raise MarketError(detail.get("error_code", "MARKET_ERROR"), response.status_code, "Synthetic error")
            return response
        def pending_imports(self, token):
            return self.call("GET", "/api/local/imports", token).json()["items"]
        def download(self, token, card_id, **kwargs):
            Client.downloads += 1
            return self.call("GET", "/api/cards/" + card_id + "/package", token).content
        def report_import(self, token, job_id, status, message=""):
            if Client.fail_report:
                raise MarketError("MARKET_UNREACHABLE", 502, "Synthetic unavailable")
            return self.call("POST", "/api/local/imports/" + job_id, token, json={"status": status, "message": message})
        def bind_status(self, code):
            return self.call("GET", "/api/bind/status", params={"code": code}).json()
        def revoke(self, token):
            self.call("DELETE", "/api/local/authorization", token)
        def set_reception(self, token, enabled):
            self.call("PATCH", "/api/local/authorization", token, json={"enabled": enabled})

    monkeypatch.setattr("backend.market_receiver.MarketClient", Client)
    monkeypatch.setattr("backend.market_router.MarketClient", Client)
    monkeypatch.setattr("backend.market_router.BindingStore", lambda: store)
    def resolve(request):
        raw = request.cookies.get("session_token")
        if not raw:
            raise HTTPException(401)
        return auth.resolve_session(raw)
    app = FastAPI()
    router = create_market_router(cards, resolve, base_url="https://market.invalid", auth=auth)
    app.include_router(router)
    from fastapi.responses import JSONResponse
    @app.exception_handler(MarketError)
    async def error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": {"error_code": exc.code}})
    local = TestClient(app)
    local.cookies.set("session_token", raw_b)
    receiver = router.reception
    receiver.interval = .05
    def request_job():
        receiver.drain(local_a)  # genuine receiver heartbeat before the web import
        response = web.post("/api/imports", json={"card_id": remote_card})
        assert response.status_code == 202
        return response.json()["job_id"]
    yield dict(cards=cards, auth=auth, store=store, web=web, local=local, receiver=receiver,
               a=local_a, b=local_b, token=token, request_job=request_job, client=Client, market=market)
    receiver.close()
    local.close()
    web.close()


def test_logged_out_reception_and_a_b_isolation(reception):
    r = reception
    job = r["request_job"]()
    r["local"].cookies.clear()
    r["receiver"].drain(r["a"])
    assert r["web"].get("/api/imports/" + job).json()["status"] == "done"
    assert len(r["cards"].list(r["a"])) == 1
    assert len(r["cards"].list(r["b"])) == 1  # only the synthetic source card
    assert r["receiver"].notices(r["b"])["cards"] == []
    assert len(r["receiver"].notices(r["a"])["cards"]) == 1


def test_receive_authorization_does_not_switch_b_or_open_a_session(reception):
    r = reception
    with r["cards"].db.session() as sess:
        before = sess.scalar(select(func.count()).select_from(LoginSessionRow))
    started = r["local"].post("/api/market/bind/start?purpose=receive").json()
    code = started["code"]
    assert r["local"].get("/api/market/bind/status", params={"code": code}).status_code == 409
    r["web"].get("/bind", params={"code": code, "purpose": "receive"})
    assert r["web"].post("/api/bind/confirm", json={"code": code}).status_code == 200
    cookie = r["local"].cookies["session_token"]
    result = r["local"].get("/api/market/bind/status", params={"code": code, "purpose": "receive"})
    assert result.json()["bound"] is True and result.json()["current_account"] is False
    assert "set-cookie" not in result.headers
    assert r["local"].cookies["session_token"] == cookie
    assert r["auth"].resolve_session(cookie).user_id == r["b"]
    with r["cards"].db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(LoginSessionRow)) == before
    assert r["store"].get(r["a"])["purpose"] == "receive"
    assert r["local"].get("/api/market/bind/status?purpose=receive&code=not-locally-created").status_code == 410
    assert r["local"].get("/api/market/bind/status", params={"code": code, "purpose": "receive"}).json()["bound"]
    reception_token = r["store"].get(r["a"])["token"]
    with TestClient(r["market"].app) as limited:
        headers = {"Authorization": "Bearer " + reception_token}
        assert limited.get("/api/local/imports", headers=headers).status_code == 200
        assert limited.delete("/api/cards/card_synthetic_remote", headers=headers).status_code == 403
        assert limited.get("/api/cards?scope=mine", headers=headers).status_code == 403


def test_restart_and_failed_ack_do_not_duplicate_cards(reception):
    r = reception
    job = r["request_job"]()
    r["client"].fail_report = True
    r["receiver"].drain(r["a"])
    assert len(r["cards"].list(r["a"])) == 1
    r["client"].fail_report = False
    restarted = MarketReceiver(r["cards"], r["store"])
    restarted.drain(r["a"])
    assert r["web"].get("/api/imports/" + job).json()["status"] == "done"
    assert r["client"].downloads == 1
    assert len(r["cards"].list(r["a"])) == 1


def test_pause_revoke_disabled_user_and_current_account_controls(reception):
    r = reception
    # B cannot pause A by adding an owner parameter.
    assert r["local"].patch("/api/market/reception?owner=" + r["a"], json={"enabled": False}).status_code == 409
    assert r["store"].get(r["a"])["enabled"]
    r["receiver"].configure(r["a"], enabled=False)
    assert r["web"].post("/api/imports", json={"card_id": "card_synthetic_remote"}).status_code == 409
    r["receiver"].configure(r["a"], enabled=True)
    job = r["request_job"]()
    with r["cards"].db.session() as sess:
        sess.get(UserRow, r["a"]).status = "disabled"
    r["receiver"].drain(r["a"])
    assert r["web"].get("/api/imports/" + job).json()["status"] == "pending"
    with r["cards"].db.session() as sess:
        sess.get(UserRow, r["a"]).status = "approved"
    r["receiver"].configure(r["a"], revoke=True)
    assert r["store"].get(r["a"]) is None
    with pytest.raises(MarketError) as rejected:
        r["client"]("").pending_imports(r["token"])
    assert rejected.value.status == 401


def test_pause_during_download_prevents_publication(reception, monkeypatch):
    r = reception
    r["request_job"]()
    original = r["client"].download
    def download(client, *args, **kwargs):
        payload = original(client, *args, **kwargs)
        r["store"].set_enabled(r["a"], False)
        return payload
    monkeypatch.setattr(r["client"], "download", download)
    r["receiver"].drain(r["a"])
    assert r["cards"].list(r["a"]) == []


def test_lifecycle_receives_without_any_browser_polling(reception):
    r = reception
    job = r["request_job"]()
    r["local"].cookies.clear()
    r["receiver"].start()
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if r["web"].get("/api/imports/" + job).json()["status"] == "done":
                break
            time.sleep(.05)
        assert r["web"].get("/api/imports/" + job).json()["status"] == "done"
    finally:
        r["receiver"].close()
    assert not r["receiver"].thread.is_alive()


def test_atomic_claim_and_receiver_owned_ack(reception):
    r = reception
    job = r["request_job"]()
    token_two = r["web"].post("/api/devices").json()["token"]
    with TestClient(r["market"].app) as first, TestClient(r["market"].app) as second:
        one = {"Authorization": "Bearer " + r["token"]}
        two = {"Authorization": "Bearer " + token_two}
        barrier = threading.Barrier(2)
        results = []
        def claim(client, headers):
            barrier.wait()
            results.append((headers, client.get("/api/local/imports", headers=headers).json()["items"]))
        threads = [threading.Thread(target=claim, args=(first, one)),
                   threading.Thread(target=claim, args=(second, two))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert sum(len(items) for _, items in results) == 1
        winner = next(headers for headers, items in results if items)
        loser = two if winner == one else one
        assert first.post("/api/local/imports/" + job, headers=loser, json={"status": "done"}).status_code == 409
        assert first.post("/api/local/imports/" + job, headers=winner, json={"status": "done"}).status_code == 200
        assert first.post("/api/local/imports/" + job, headers=winner, json={"status": "failed"}).status_code == 200
        assert r["web"].get("/api/imports/" + job).json()["status"] == "done"


def test_interrupted_local_receipt_is_not_reimported(reception):
    r = reception
    job = r["request_job"]()
    with r["receiver"].journal() as conn:
        conn.execute("INSERT INTO receipts(owner,origin,job_id,status) VALUES(?,?,?,'started')",
                     (r["a"], "https://market.invalid", job))
    r["receiver"].drain(r["a"])
    assert r["client"].downloads == 0
    assert r["cards"].list(r["a"]) == []
    assert r["web"].get("/api/imports/" + job).json()["status"] == "failed"


def test_concurrent_account_saves_and_late_revoke_are_isolated(reception, monkeypatch):
    r = reception
    original = r["client"].revoke
    def revoke(client, token):
        original(client, token)
        r["store"].save(r["a"], r["a"], "https://market.invalid", "synthetic-fresh-token")
    monkeypatch.setattr(r["client"], "revoke", revoke)
    r["receiver"].configure(r["a"], revoke=True)
    assert r["store"].get(r["a"])["token"] == "synthetic-fresh-token"
    assert r["store"].get(r["a"])["enabled"]
    threads = [threading.Thread(target=r["store"].save, args=(
        "synthetic-extra-" + str(i), "market-" + str(i), "https://market.invalid", "synthetic-token-" + str(i)))
        for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(r["store"].owners()) == 5


def test_stream_cancellation_closes_response(monkeypatch):
    from backend.market_link import MarketClient
    stop = threading.Event()
    closed = []
    class Stream:
        status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append(True)
        def iter_bytes(self, size):
            stop.set()
            yield b"synthetic"
    monkeypatch.setattr("backend.market_link.httpx.stream", lambda *args, **kwargs: Stream())
    with pytest.raises(MarketError) as result:
        MarketClient("https://market.invalid").download("synthetic", "synthetic", cancel=stop)
    assert result.value.code == "IMPORT_CANCELLED" and closed == [True]
