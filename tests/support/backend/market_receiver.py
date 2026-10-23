"""Account-scoped market reception independent of browser login and navigation."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager

from .auth.models import UserRow
from .characters.package import import_package
from .market_link import BindingStore, MarketClient, MarketError


class MarketReceiver:
    def __init__(self, cards, store: BindingStore, *, interval: float = 15):
        self.cards, self.store, self.interval = cards, store, interval
        self.stop = threading.Event()
        self.thread = None
        self.jobs: dict[str, threading.Thread] = {}
        self.due: dict[str, float] = {}
        self.locks: dict[str, threading.Lock] = {}
        self.errors: dict[str, str] = {}
        self.registry = threading.Lock()

    @contextmanager
    def journal(self):
        path = self.store.path.with_suffix(".jobs.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS receipts(
                owner TEXT NOT NULL, origin TEXT NOT NULL, job_id TEXT NOT NULL,
                status TEXT NOT NULL, card_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(owner, origin, job_id))""")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _allowed(self, owner: str, item: dict) -> bool:
        current = self.store.get(owner)
        if self.stop.is_set() or not current or not current["enabled"] or current["generation"] != item["generation"]:
            return False
        with self.cards.db.session() as session:
            user = session.get(UserRow, owner)
            return bool(user and user.status == "approved")

    def notices(self, owner: str) -> dict:
        if not self.store.path.with_suffix(".jobs.sqlite3").exists():
            return {"cards": [], "imported": 0}
        with self.journal() as conn:
            rows = conn.execute("SELECT job_id,card_id FROM receipts WHERE owner=? AND status='done' "
                                "ORDER BY rowid DESC LIMIT 100", (owner,)).fetchall()
        return {"cards": [dict(row) for row in rows], "imported": len(rows)}

    def drain(self, owner: str):
        with self.registry:
            lock = self.locks.setdefault(owner, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            item = self.store.get(owner)
            if not item or not self._allowed(owner, item):
                return
            client = MarketClient(item["base_url"])
            jobs = client.pending_imports(item["token"])
            self.errors.pop(owner, None)
            for job in jobs[:5]:
                job_id, card_id = job["job_id"], job["card_id"]
                if not self._allowed(owner, item):
                    break
                with self.journal() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    receipt = conn.execute("SELECT * FROM receipts WHERE owner=? AND origin=? AND job_id=?",
                                           (owner, item["base_url"], job_id)).fetchone()
                    if receipt is None:
                        conn.execute("INSERT INTO receipts(owner,origin,job_id,status) VALUES(?,?,?,'started')",
                                     (owner, item["base_url"], job_id))
                if receipt is not None:
                    status = "done" if receipt["status"] == "done" else "failed"
                    client.report_import(item["token"], job_id, status,
                                         "" if status == "done" else "上次导入中断，请检查对应账号的人物列表后重新导入。")
                    continue
                try:
                    payload = client.download(item["token"], card_id, timeout=20, cancel=self.stop)
                    with self.store.guard():
                        if not self._allowed(owner, item):
                            return
                        self.cards.audit.append("connector.bound", actor_id=owner, result="requested",
                                                object_type="market_import", object_id=job_id)
                        card = import_package(self.cards, owner, payload)
                        self.cards.audit.append("connector.bound", actor_id=owner, result="success",
                                                object_type="market_import", object_id=job_id)
                        with self.journal() as conn:
                            conn.execute("UPDATE receipts SET status='done',card_id=? "
                                         "WHERE owner=? AND origin=? AND job_id=?",
                                         (card["card_id"], owner, item["base_url"], job_id))
                except Exception:
                    if self.stop.is_set():
                        return
                    self.cards.audit.append("connector.bound", actor_id=owner, result="failed",
                                            object_type="market_import", object_id=job_id)
                    with self.journal() as conn:
                        conn.execute("UPDATE receipts SET status='failed' WHERE owner=? AND origin=? AND job_id=?",
                                     (owner, item["base_url"], job_id))
                    client.report_import(item["token"], job_id, "failed", "本机导入未完成，请在对应账号中检查后重试。")
                    self.errors[owner] = "IMPORT_FAILED"
                    continue
                client.report_import(item["token"], job_id, "done")
        except MarketError as exc:
            self.errors[owner] = exc.code
        except Exception:
            self.errors[owner] = "RECEPTION_UNAVAILABLE"
        finally:
            lock.release()

    def configure(self, owner: str, *, enabled: bool | None = None, revoke: bool = False):
        try:
            return self._configure(owner, enabled=enabled, revoke=revoke)
        except Exception:
            self.cards.audit.append("connector.revoked" if revoke else "connector.bound",
                                    actor_id=owner, result="failed", object_type="market_reception")
            raise

    def _configure(self, owner: str, *, enabled: bool | None = None, revoke: bool = False):
        with self.store.guard():
            item = self.store.get(owner)
            self.cards.audit.append("connector.revoked" if revoke else "connector.bound",
                                    actor_id=owner, result="requested", object_type="market_reception")
            if item:
                self.store.set_enabled(owner, False)
        if item:
            client = MarketClient(item["base_url"])
            if revoke:
                client.revoke(item["token"])
            else:
                client.set_reception(item["token"], bool(enabled))
            with self.store.guard():
                current = self.store.get(owner)
                # A fresh authorization must not be cleared by a late revoke response.
                if current and current["token"] == item["token"]:
                    if revoke:
                        self.store.clear(owner)
                    else:
                        self.store.set_enabled(owner, bool(enabled))
        elif not revoke:
            raise MarketError("BIND_REQUIRED", 409, "请先开启账号接收。")
        self.cards.audit.append("connector.revoked" if revoke else "connector.bound",
                                actor_id=owner, result="success", object_type="market_reception")
        self.due.pop(owner, None)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()

        def run():
            while not self.stop.is_set():
                try:
                    for owner in self.store.owners():
                        if sum(task.is_alive() for task in self.jobs.values()) >= 4:
                            break
                        active = self.jobs.get(owner)
                        if (active and active.is_alive()) or self.due.get(owner, 0) > time.monotonic():
                            continue
                        self.due[owner] = time.monotonic() + self.interval
                        task = threading.Thread(target=self.drain, args=(owner,), daemon=True,
                                                name="market-reception-account")
                        self.jobs[owner] = task
                        task.start()
                except Exception:
                    pass
                self.stop.wait(1)
        self.thread = threading.Thread(target=run, daemon=True, name="market-reception")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        deadline = time.monotonic() + 3
        for task in list(self.jobs.values()):
            task.join(timeout=max(0, deadline - time.monotonic()))
