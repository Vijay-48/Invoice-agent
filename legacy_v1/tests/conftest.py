"""Test setup: a real Postgres database (TEST_DATABASE_URL), no OpenAI or Gmail calls.

    createdb ap_test
    TEST_DATABASE_URL=postgresql+psycopg://ap:ap@localhost:5432/ap_test pytest
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="ap-tests-")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://ap:ap@localhost:5432/ap_test")
os.environ["STORAGE_DIR"] = os.path.join(_tmp, "storage")
os.environ["LEDGER_DIR"] = os.path.join(_tmp, "ledgers")
os.environ["COMPANY_GSTIN"] = "36AABCT1234F1ZR"
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Supplier, PurchaseOrder, POLine
from app.schemas import ExtractedInvoice

SUPPLIER_GSTIN = "36AAACS1234A1Z3"     # Telangana, same state as the company
OTHER_GSTIN = "29AAACS1234A1ZY"        # Karnataka


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean():
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    for d in (settings.storage_dir, settings.ledger_dir):
        shutil.rmtree(d, ignore_errors=True)
    yield


@pytest.fixture
def session():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def master(session):
    """The same master data as scripts/seed.py, plus a second supplier."""
    sup = Supplier(name="Sri Lakshmi Steels Pvt Ltd", gstin=SUPPLIER_GSTIN)
    other = Supplier(name="Bangalore Fittings", gstin=OTHER_GSTIN)
    session.add_all([sup, other])
    session.flush()
    po = PurchaseOrder(po_number="PO-2026-0042", supplier_id=sup.id, lines=[
        POLine(description="MS Pipe 25mm", hsn_sac="7306", qty_ordered=100,
               qty_received=100, unit_price=450, gst_rate=18),
        POLine(description="GI Elbow 25mm", hsn_sac="7307", qty_ordered=200,
               qty_received=150, unit_price=35, gst_rate=18),
    ])
    session.add(po)
    session.commit()
    return {"supplier": sup, "other": other, "po": po}


def make_extracted(**overrides) -> ExtractedInvoice:
    """A clean intra-state invoice for PO-2026-0042: 10 pipes + 20 elbows, 18% GST."""
    lines = overrides.pop("lines", [
        dict(description="MS Pipe 25mm", hsn_sac="7306", quantity=10, unit_price=450,
             taxable_value=4500, gst_rate=18),
        dict(description="GI Elbow 25mm", hsn_sac="7307", quantity=20, unit_price=35,
             taxable_value=700, gst_rate=18),
    ])
    taxable = sum(l["taxable_value"] for l in lines)
    tax = round(sum(l["taxable_value"] * l["gst_rate"] / 100 for l in lines), 2)
    base = dict(
        is_invoice=True, supplier_name="Sri Lakshmi Steels Pvt Ltd", supplier_gstin=SUPPLIER_GSTIN,
        buyer_gstin="36AABCT1234F1ZR", invoice_number="SLS/26-27/118", invoice_date="2026-09-15",
        place_of_supply_state_code="36", po_number="PO-2026-0042", irn=None, lines=lines,
        taxable_total=taxable, cgst=tax / 2, sgst=tax / 2, igst=0, round_off=0,
        grand_total=taxable + tax, confidence=0.95, low_confidence_fields=[],
    )
    base.update(overrides)
    return ExtractedInvoice(**base)


def ledger_path(*parts) -> Path:
    return Path(settings.ledger_dir, *parts)
