"""Several users using the system at the same time, with test invoices whose outcome is known.

    python -m scripts.simulate_users                          # 3 users x one of each scenario, against :8000
    python -m scripts.simulate_users --users 5 --repeat 2
    python -m scripts.simulate_users --api http://localhost:8000 --only clean_intra,over_limit

Needs the API running (uvicorn app.api:app) with its worker, and calls the REAL AI: every document
costs one or two OpenAI calls. Run it on a test database if you don't want test users and invoices in
your real one (set DATABASE_URL for the API, e.g. the ap_test database).

What it does:
  1. checks the system status (stops if the OpenAI account can't be used)
  2. creates (or reuses) users "Test user 1..N" (test-user-N@example.com)
  3. every user uploads their own set of invoices AT THE SAME TIME (scripts/make_test_invoices.py)
  4. user 1 and user 2 also upload the very same file: the second must get the first's invoice back
  5. waits until the worker has read everything, then compares each document's status with the
     expected one, per scenario and per user, and saves the details to reports/sim-<time>.json

Exit code 1 if any document ends with an unexpected status.
"""
import argparse
import asyncio
import json
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import httpx

from scripts.make_test_invoices import SCENARIOS, make_set, render

PENDING = {"RECEIVED", "PROCESSING", "APPROVED"}   # APPROVED = waiting to be written to Excel


async def ensure_user(client: httpx.AsyncClient, n: int) -> dict:
    email = f"test-user-{n}@example.com"
    users = (await client.get("/api/v1/users")).json()
    for u in users:
        if u["email"].lower() == email:
            return u
    r = await client.post("/api/v1/users", json={"name": f"Test user {n}", "email": email})
    r.raise_for_status()
    return r.json()


async def upload(client: httpx.AsyncClient, user: dict, name: str, data: bytes) -> dict:
    t0 = time.monotonic()
    r = await client.post("/api/v1/invoices/upload", headers={"X-User-Id": str(user["id"])},
                          files={"file": (name, data, "application/pdf")})
    took = time.monotonic() - t0
    if r.status_code != 201:
        return {"error": f"{r.status_code} {r.text[:200]}", "seconds": took}
    return {**r.json(), "seconds": took}


async def run_user(client, user, docs, results):
    """One user's uploads, a few at a time like a person dropping files in."""
    async def one(inv):
        res = await upload(client, user, inv["file"], inv["pdf"])
        results.append({"user": user["name"], "user_id": user["id"], "scenario": inv["scenario"],
                        "expected": inv["expected"], "file": inv["file"], "number": inv["number"], **res})
    await asyncio.gather(*(one(inv) for inv in docs))


async def wait_done(client, ids: set[int], user_ids: list[int], timeout: float) -> dict[int, dict]:
    t0, last = time.monotonic(), None
    while True:
        rows = []
        for uid in user_ids:   # per user, so other invoices in the database can't push ours out
            rows += (await client.get("/api/v1/invoices", params={"limit": 500, "user_id": uid})).json()
        mine = {r["invoice_id"]: r for r in rows if r["invoice_id"] in ids}
        pending = [i for i, r in mine.items() if r["status"] in PENDING]
        if not pending and len(mine) == len(ids):
            return mine
        if time.monotonic() - t0 > timeout:
            print(f"\nTimed out with {len(pending)} document(s) still pending")
            return mine
        if pending != last:
            print(f"  waiting for the worker: {len(pending)} of {len(ids)} still being read...", flush=True)
            last = pending
        await asyncio.sleep(3)


async def main_async(args) -> int:
    rnd = random.Random(args.seed)
    scenarios = [s.strip() for s in args.only.split(",") if s.strip()] or list(SCENARIOS)
    async with httpx.AsyncClient(base_url=args.api.rstrip("/"), timeout=120) as client:
        try:
            status = (await client.get("/api/v1/system/status")).json()
        except httpx.HTTPError as e:
            print(f"Can't reach the API at {args.api}: {e}")
            return 2
        if not status["ai"]["ok"] and status["ai"].get("kind") == "account":
            print(f"Stopping: {status['ai']['problem']}. Fix the OpenAI account first.")
            return 2
        if not status.get("workers_here"):
            print("Note: this API runs no worker (RUN_WORKER=false); documents are read only if a worker "
                  "process runs elsewhere.")

        users = [await ensure_user(client, n) for n in range(1, args.users + 1)]
        plan = {}
        for u in users:
            tag = f"U{u['id']}-{rnd.randint(100, 999)}-"   # numbers unique per user and run
            docs = make_set(rnd, scenarios * args.repeat, tag)
            for inv in docs:
                inv["pdf"] = render(inv)
            plan[u["id"]] = docs
        total = sum(len(d) for d in plan.values())
        print(f"{len(users)} user(s) uploading {total} document(s) at the same time "
              f"(~{total}-{2 * total} AI calls)...")
        results: list[dict] = []
        t0 = time.monotonic()
        await asyncio.gather(*(run_user(client, u, plan[u["id"]], results) for u in users))
        # the same file uploaded by two people: one record, the second gets it back
        shared = None
        if len(users) >= 2 and plan[users[0]["id"]]:
            first_doc = plan[users[0]["id"]][0]
            shared = await upload(client, users[1], "shared-" + first_doc["file"], first_doc["pdf"])
        print(f"Uploads done in {time.monotonic() - t0:.1f}s; "
              f"slowest upload {max((r.get('seconds', 0) for r in results), default=0):.2f}s")

        ids = {r["invoice_id"] for r in results if r.get("invoice_id")}
        final = await wait_done(client, ids, [u["id"] for u in users], args.timeout)
        print(f"All read in {time.monotonic() - t0:.0f}s\n")

    # ---------- compare
    by_scenario: dict[str, Counter] = defaultdict(Counter)
    mismatches = []
    pairs: dict[tuple, list] = defaultdict(list)   # an invoice and its 'duplicate': read at the same moment,
    dup_numbers = {(u["id"], d["number"]) for u in users for d in plan[u["id"]] if d["scenario"] == "duplicate"}
    for r in results:
        got = final.get(r.get("invoice_id"), {})
        r["actual"], r["reason"] = got.get("status", "ERROR" if r.get("error") else "MISSING"), got.get("status_reason")
        if (r["user_id"], r["number"]) in dup_numbers:
            pairs[(r["user_id"], r["number"])].append(r)   # either copy may be the original: judged as a pair
            continue
        r["ok"] = r["actual"] == r["expected"]
        by_scenario[r["scenario"]]["ok" if r["ok"] else "wrong"] += 1
        if not r["ok"]:
            mismatches.append(r)
    for pair in pairs.values():
        ok = sorted(r["actual"] for r in pair) == ["DUPLICATE", "EXPORTED"]   # exactly one is the copy
        for r in pair:
            r["ok"] = ok
            by_scenario[r["scenario"]]["ok" if ok else "wrong"] += 1
            if not ok:
                mismatches.append(r)

    print(f"{'scenario':14} {'expected':14} right / wrong")
    for sc in scenarios:
        c = by_scenario.get(sc, Counter())
        print(f"{sc:14} {SCENARIOS[sc]:14} {c['ok']:5} / {c['wrong']}")
    if shared is not None:
        first_id = next((r["invoice_id"] for r in results if r["user_id"] == users[0]["id"]
                         and r["file"] == plan[users[0]["id"]][0]["file"]), None)
        same = shared.get("duplicate_file") and shared.get("invoice_id") == first_id
        print(f"\nSame file from two users -> one record: {'yes' if same else 'NO'} "
              f"(#{shared.get('invoice_id')}, duplicate_file={shared.get('duplicate_file')})")
        if not same:
            mismatches.append({"scenario": "shared_file", "detail": shared})
    if mismatches:
        print(f"\n{len(mismatches)} unexpected result(s):")
        for m in mismatches:
            print(f"  {m.get('user', '')} {m.get('scenario')} #{m.get('invoice_id')}: expected {m.get('expected')}, "
                  f"got {m.get('actual')} - {(m.get('reason') or m.get('error') or '')[:160]}")
    Path("reports").mkdir(exist_ok=True)
    out = Path(f"reports/sim-{datetime.now():%Y%m%d-%H%M%S}.json")
    out.write_text(json.dumps({"users": [u["name"] for u in users], "results": results,
                               "shared_file": shared}, indent=2, default=str), encoding="utf-8")
    print(f"\nDetails: {out}")
    return 1 if mismatches else 0


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--users", type=int, default=3)
    p.add_argument("--repeat", type=int, default=1, help="how many of each scenario per user")
    p.add_argument("--only", default="", help="comma-separated scenarios (default: all)")
    p.add_argument("--timeout", type=float, default=900, help="seconds to wait for the worker")
    p.add_argument("--seed", type=int, default=None)
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()
