# ruff: noqa: F811  (pytest fixtures imported from test_flow / test_gmail)
"""Many users at once: parallel workers, PDF library safety, one sync per inbox, async chat."""
import asyncio
import threading
import time
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import assistant, gmail, worker
from app.api import app
from app.db import engine
from app.documents import PDF_LOCK
from app.models import User
from app.pipeline import einvoice, extract
from tests.conftest import make_extracted
from tests.test_assistant import FakeStreamAgent
from tests.test_flow import client, fake_extract, get, upload   # noqa: F401 (fixtures)
from tests.test_gmail import fake   # noqa: F401 (fixture)

GOLDEN = Path(__file__).resolve().parent.parent / "golden" / "docs"


def test_parallel_workers_process_each_invoice_once(client, users, monkeypatch):
    calls, guard = [], threading.Lock()

    def slow_extract(path, name=None, **kw):
        with guard:
            calls.append(name)
        time.sleep(0.2)
        return make_extracted(invoice_number=f"P-{name}")
    monkeypatch.setattr(worker, "extract_invoice", slow_extract)
    monkeypatch.setattr(worker, "page_count", lambda path: 1)
    ids = [upload(client, users["priya"], f"%PDF {i}".encode(), f"doc{i}.pdf")["invoice_id"] for i in range(12)]

    def drain():
        while (inv_id := worker.claim_next()) is not None:
            worker.process(inv_id)
    threads = [threading.Thread(target=drain) for _ in range(4)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    worker.export()
    assert sorted(calls) == sorted(f"doc{i}.pdf" for i in range(12))            # each exactly once
    assert time.monotonic() - t0 < 12 * 0.2                                   # really in parallel
    assert {get(client, i)["status"] for i in ids} == {"EXPORTED"}


@pytest.mark.skipif(not GOLDEN.exists(), reason="golden documents not present")
def test_pdf_library_is_only_used_under_the_lock(monkeypatch, tmp_path):
    """MuPDF isn't thread-safe: every open must hold PDF_LOCK (parallel workers crashed the server)."""
    real_open, unlocked = pymupdf.open, []

    def checked_open(*a, **kw):
        if not PDF_LOCK._is_owned():
            unlocked.append(a[:1])
        return real_open(*a, **kw)
    monkeypatch.setattr(pymupdf, "open", checked_open)
    pdf = tmp_path / "hfcl.pdf"
    pdf.write_bytes((GOLDEN / "hfcl_invoice.pdf").read_bytes())
    errors = []

    def use_pdfs():
        try:
            extract.prepare_pages(str(pdf), None)
            extract.page_count(str(pdf))
            einvoice._text_layer(str(pdf), None)
        except Exception as e:   # pragma: no cover - reported below
            errors.append(e)
    threads = [threading.Thread(target=use_pdfs) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and unlocked == []


def test_one_sync_per_inbox_at_a_time(users, fake, monkeypatch):
    running, overlaps, guard = [0], [], threading.Lock()

    def slow_sync(session, user, since, max_messages):
        with guard:
            running[0] += 1
            overlaps.append(running[0])
        time.sleep(0.2)
        with guard:
            running[0] -= 1
        return []
    monkeypatch.setattr(gmail, "_sync_user", slow_sync)
    from app.db import SessionLocal

    def sync():
        with SessionLocal() as s:
            gmail.sync_user(s, s.get(User, users["priya"].id))
    threads = [threading.Thread(target=sync) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == [1, 1, 1]


def test_many_chats_at_once_dont_use_up_database_connections(users, monkeypatch):
    """While an answer is being written (seconds, waiting for OpenAI), the request holds no
    database connection; 20 people chatting at once all get their answers saved."""
    held = []

    class Slow(FakeStreamAgent):
        def arun(self, input, stream=False, **kw):
            async def events():
                for ev in self.run(input, stream=True):
                    await asyncio.sleep(0.2)
                    held.append(engine.pool.checkedout())   # connections in use mid-answer
                    yield ev
            return events()
    monkeypatch.setattr(assistant, "_agent", lambda: Slow())
    h = {"X-User-Id": str(users["priya"].id)}
    with TestClient(app) as c:
        one = c.post("/api/v1/assistant/sessions", headers=h).json()["id"]
        assert '"type": "saved"' in c.post(f"/api/v1/assistant/sessions/{one}/stream", headers=h,
                                           json={"content": "hi"}).text
        assert held and max(held) == 0, held

        ids = [c.post("/api/v1/assistant/sessions", headers=h).json()["id"] for _ in range(20)]
        results = []

        def ask(cid):
            r = c.post(f"/api/v1/assistant/sessions/{cid}/stream", headers=h, json={"content": "hi"})
            results.append('"type": "saved"' in r.text)
        threads = [threading.Thread(target=ask, args=(cid,)) for cid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert results == [True] * 20


def test_huge_page_size_is_rendered_small(tmp_path):
    """A 1 KB PDF can declare a page 14,400 x 14,400 points (200 inches): rendering it at 200 dpi
    would need ~8 GB. The render resolution is capped instead."""
    pdf = tmp_path / "huge.pdf"
    doc = pymupdf.open()
    doc.new_page(width=14400, height=14400).insert_text((72, 72), "TAX INVOICE")
    doc.save(pdf)
    doc.close()
    assert pdf.stat().st_size < 5000
    images, text, pages = extract.prepare_pages(str(pdf))
    from PIL import Image as PILImage
    with PILImage.open(images[0].filepath) as im:
        assert im.width * im.height <= 25_000_000
