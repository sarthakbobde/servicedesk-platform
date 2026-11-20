"""D32 latency check against a running server: N concurrent users each make R requests.

    uv run python scripts/load_api.py http://localhost:8765 maya@demo.test <password> [users] [rounds]

Reports p50/p95 for an API read (ticket list), an API action (estimate, alternating values) and a portal page.
"""
import statistics
import sys
import threading
import time

import httpx


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[max(0, int(len(xs) * p) - 1)], 1)


def main():
    base, email, password = sys.argv[1:4]
    users = int(sys.argv[4]) if len(sys.argv) > 4 else 100
    rounds = int(sys.argv[5]) if len(sys.argv) > 5 else 5
    tok = httpx.post(f"{base}/api/v1/auth/token", json={"email": email, "password": password}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    projects = httpx.get(f"{base}/api/v1/projects", headers=h).json()["items"]
    total = httpx.get(f"{base}/api/v1/tickets?view=team&page_size=100", headers=h).json()
    if total["total"] < users:  # spread actions over one ticket per user (a single hot row only measures lock waits)
        team_ticket = total["items"][0]
        for k in range(users - total["total"]):
            httpx.post(f"{base}/api/v1/tickets", headers=h, json={
                "project_id": team_ticket["project"]["id"], "type_id": team_ticket["type"]["id"],
                "title": f"load {k}", "description": "load test"})
    ids = [t["id"] for t in httpx.get(f"{base}/api/v1/tickets?view=team&page_size=100", headers=h).json()["items"]]
    web = httpx.Client(base_url=base)
    web.post("/login", data={"email": email, "password": password})
    cookie = web.cookies.get("tsid")
    reads, acts, pages, errors = [], [], [], []
    barrier = threading.Barrier(users)

    def user(i):
        with httpx.Client(base_url=base, timeout=30) as c:
            barrier.wait()
            for r in range(rounds):
                t = time.perf_counter()
                x = c.get("/api/v1/tickets?view=team", headers=h)
                reads.append((time.perf_counter() - t) * 1000)
                t = time.perf_counter()
                y = c.post(f"/api/v1/tickets/{ids[i % len(ids)]}/estimate", json={"minutes": 10 + (i * rounds + r) % 50}, headers=h)
                acts.append((time.perf_counter() - t) * 1000)
                t = time.perf_counter()
                z = c.get("/tickets/all", cookies={"tsid": cookie})
                pages.append((time.perf_counter() - t) * 1000)
                for resp in (x, z):
                    if resp.status_code != 200:
                        errors.append(resp.status_code)
                if y.status_code not in (200, 422):
                    errors.append(y.status_code)

    ths = [threading.Thread(target=user, args=(i,)) for i in range(users)]
    t0 = time.perf_counter()
    [t.start() for t in ths]
    [t.join() for t in ths]
    print(f"{users} users x {rounds} rounds in {time.perf_counter() - t0:.1f} s, errors={len(errors)}")
    for name, xs in (("API read (ticket list)", reads), ("API action (estimate)", acts), ("portal page", pages)):
        print(f"  {name}: p50 {statistics.median(xs):.0f} ms, p95 {pct(xs, 0.95):.0f} ms")


if __name__ == "__main__":
    main()
