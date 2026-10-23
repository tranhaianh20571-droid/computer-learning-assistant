"""Logged-in username uniqueness uses isolated synthetic market accounts."""
import threading

from test_market_registration import market  # noqa: F401
from test_market_uploads import signed_client


def test_logged_in_rename_rejects_taken_and_preserves_original(market):
    with signed_client(market, "synthetic-alice") as alice, signed_client(market, "synthetic-bob") as bob:
        response = bob.get("/api/username/available", params={"name": "SYNTHETIC-ALICE"})
        assert response.json() == {"available": False, "reason": "taken"}
        assert response.headers["cache-control"] == "private, no-store"
        rejected = bob.post("/api/username", json={"display_name": "SYNTHETIC-ALICE"})
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["error_code"] == "USERNAME_TAKEN"
        assert bob.get("/api/me").json()["user"]["display_name"] == "synthetic-bob"
        for name in ("synthetic-alice", "SYNTHETIC-ALICE"):
            assert alice.get("/api/username/available", params={"name": name}).json()["available"]
            assert alice.post("/api/username", json={"display_name": name}).status_code == 200
        assert alice.post("/api/username", json={"display_name": "  Synthetic   New  "}).json()["display_name"] == "Synthetic New"
        assert bob.post("/api/username", json={"display_name": "synthetic-alice"}).status_code == 200


def test_concurrent_rename_allows_only_one_owner(market):
    with signed_client(market, "synthetic-alice") as alice, signed_client(market, "synthetic-bob") as bob:
        barrier = threading.Barrier(2)
        results = []
        def save(client):
            barrier.wait()
            results.append(client.post("/api/username", json={"display_name": "Synthetic Shared"}).status_code)
        threads = [threading.Thread(target=save, args=(alice,)), threading.Thread(target=save, args=(bob,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert sorted(results) == [200, 409]
