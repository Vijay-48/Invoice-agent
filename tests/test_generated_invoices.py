# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""The test-invoice generator against the real rules, with a perfect stand-in for the AI.

scripts/make_test_invoices.py promises an outcome per scenario. Here every generated PDF is "read"
exactly (from the generator's own data, found through the PDF's text) and goes through the real
pipeline: upload -> queue -> worker -> checks -> decision -> Excel. If a scenario's promise and the
rules ever disagree, this fails, before anyone spends OpenAI credits on a simulation.
Second part: three users at once, four workers in parallel.
"""
import random
import threading
from datetime import datetime

import pymupdf

from app import worker
from app.models import Status
from app.schemas import ExtractedInvoice
from scripts.make_test_invoices import SCENARIOS, make_set, render
from tests.conftest import not_an_invoice
from tests.test_flow import client, fake_extract, get, h, upload   # noqa: F401 (fixtures)

KIND = {"TAX INVOICE": "tax_invoice", "CREDIT NOTE": "credit_note"}


def perfect_reader(registry: dict):
    """extract_invoice stand-in: the generator's data for the document, as a flawless reading."""
    def read(path, name=None, **kw):
        with pymupdf.open(path) as doc:
            text = doc[0].get_text()
        inv = next((v for k, v in registry.items() if k in text), None)
        if inv is None or inv["scenario"] == "not_invoice":
            return not_an_invoice()
        return ExtractedInvoice(
            document_type=KIND[inv["kind"]], supplier_name=inv["supplier"], supplier_gstin=inv["supplier_gstin"],
            buyer_gstin=inv["buyer_gstin"], invoice_number=inv["number"],
            invoice_date=datetime.strptime(inv["date"], "%d/%m/%Y").date().isoformat(),
            voucher_number=None, voucher_date=None, invoice_kind="regular",
            reference_invoice_number=inv.get("reference"), currency="INR",
            place_of_supply_state_code=inv["buyer_gstin"][:2], po_number=None, irn=None,
            lines=[{"description": line["description"], "hsn_sac": line["hsn"], "quantity": line["qty"],
                    "unit_price": line["price"], "taxable_value": line["taxable"], "gst_rate": line["rate"]}
                   for line in inv["lines"]],
            taxable_total=inv["taxable"], cgst=inv["cgst"], sgst=inv["sgst"], igst=inv["igst"],
            round_off=inv["round_off"], grand_total=inv["total"], amount_in_words=inv["words"],
            confidence=0.97, low_confidence_fields=[])
    return read


def _register(docs: list[dict], registry: dict) -> None:
    for inv in docs:
        inv["pdf"] = render(inv)
        key = inv["number"] if inv["scenario"] != "not_invoice" else "TERMS AND CONDITIONS"
        if inv["scenario"] != "duplicate":   # the duplicate reads like its original
            registry.setdefault(key, inv)


def _drain(workers: int = 1):
    def loop():
        while (inv_id := worker.claim_next()) is not None:
            worker.process(inv_id)
    threads = [threading.Thread(target=loop) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    worker.export()


def test_every_scenario_gets_its_promised_outcome(client, users, monkeypatch):
    registry = {}
    docs = make_set(random.Random(1), list(SCENARIOS), "T1-")
    _register(docs, registry)
    monkeypatch.setattr(worker, "extract_invoice", perfect_reader(registry))
    monkeypatch.setattr(worker, "page_count", lambda path: 1)
    ids = {}
    for inv in docs:        # one at a time, in order: the clean_intra before its duplicate
        ids[inv["file"]] = upload(client, users["priya"], inv["pdf"], inv["file"])["invoice_id"]
        _drain()
    for inv in docs:
        d = get(client, ids[inv["file"]])
        assert d["status"] == inv["expected"], (inv["scenario"], d["status"], d["status_reason"])


def test_three_users_at_once_with_parallel_workers(client, users, monkeypatch):
    """Like scripts/simulate_users.py, without OpenAI: 3 users upload every scenario at the same time,
    4 workers read in parallel. Every document gets its outcome; each invoice and its re-sent copy end
    as exactly one EXPORTED + one DUPLICATE; the same file from two users is one record."""
    registry, plans, results = {}, {}, []
    people = [users["priya"], users["ravi"]]
    from app.db import SessionLocal
    from app.models import User
    with SessionLocal() as s:
        third = User(name="Asha", email="asha@example.com")
        s.add(third)
        s.commit()
        people.append(third)
    rnd = random.Random(2)
    for u in people:
        plans[u.id] = make_set(rnd, list(SCENARIOS), f"U{u.id}-")
        _register(plans[u.id], registry)
    monkeypatch.setattr(worker, "extract_invoice", perfect_reader(registry))
    monkeypatch.setattr(worker, "page_count", lambda path: 1)
    lock = threading.Lock()

    def user_uploads(u):
        for inv in plans[u.id]:
            r = client.post("/api/v1/invoices/upload", headers=h(u),
                            files={"file": (inv["file"], inv["pdf"], "application/pdf")})
            with lock:
                results.append((u.id, inv, r.status_code, r.json()))
    threads = [threading.Thread(target=user_uploads, args=(u,)) for u in people]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(code == 201 for _, _, code, _ in results)
    # the same file from another user: the existing record comes back
    first = plans[people[0].id][0]
    shared = client.post("/api/v1/invoices/upload", headers=h(people[1]),
                         files={"file": ("shared.pdf", first["pdf"], "application/pdf")}).json()
    first_id = next(body["invoice_id"] for uid, inv, _, body in results if inv is first)
    assert shared["duplicate_file"] is True and shared["invoice_id"] == first_id

    _drain(workers=4)
    pairs = {}
    for uid, inv, _, body in results:
        status = get(client, body["invoice_id"])["status"]
        if inv["scenario"] in ("clean_intra", "duplicate") and any(
                d["scenario"] == "duplicate" and d["number"] == inv["number"] for d in plans[uid]):
            pairs.setdefault((uid, inv["number"]), []).append(status)
            continue
        assert status == inv["expected"], (uid, inv["scenario"], status)
    assert pairs and all(sorted(v) == [Status.DUPLICATE, Status.EXPORTED] for v in pairs.values()), pairs
