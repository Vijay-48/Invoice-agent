"""Test setup: a real Postgres database, no OpenAI or Composio calls.

Uses TEST_DATABASE_URL (default: the ap_test database made by scripts/setup_db.py).
The database is emptied for every test, so never point it at your real data.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="ap-tests-")
# Environment variables win over .env, so real keys and settings are never used here
os.environ.update({
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://ap:ap@localhost:5432/ap_test"),
    "STORAGE_DIR": os.path.join(_tmp, "storage"),
    "EXCEL_PATH": os.path.join(_tmp, "output", "invoices.xlsx"),
    "EXPORT_DIR": os.path.join(_tmp, "output", "exports"),
    "COMPANY_GSTIN": "36AABCT1234F1ZR",
    "AUTO_APPROVE_LIMIT": "50000",
    "MIN_CONFIDENCE": "0.85",
    "OPENAI_API_KEY": "sk-test",
    "COMPOSIO_API_KEY": "ak_test",
    "GMAIL_QUERY": "has:attachment",
    "GMAIL_SUBJECT_KEYWORDS": "",
    # Tests drive the worker by hand; test_background.py turns these on
    "RUN_WORKER": "false",
    "RUN_GMAIL_LISTENER": "false",
    "RUN_EXCEL_EXPORT": "false",
    # Fake AI readings are queued one per document: no automatic second reading (test_reading.py turns it on)
    "AI_SECOND_PASS": "false",
})

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import User
from app.schemas import ExtractedInvoice

SUPPLIER_GSTIN = "36AAACS1234A1Z3"     # Telangana, same state as the company
OTHER_GSTIN = "29AAACS1234A1ZY"        # Karnataka


@pytest.fixture(scope="session", autouse=True)
def _schema():
    # Start from an empty schema, including tables left over from older versions
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean():
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    shutil.rmtree(settings.storage_dir, ignore_errors=True)
    shutil.rmtree(Path(settings.excel_path).parent, ignore_errors=True)
    yield


@pytest.fixture
def session():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def users(session):
    """Two users; Priya has Gmail connected."""
    priya = User(name="Priya", email="priya@example.com", gmail_account_id="ca_priya")
    ravi = User(name="Ravi", email="ravi@example.com")
    session.add_all([priya, ravi])
    session.commit()
    return {"priya": priya, "ravi": ravi}


def make_extracted(**overrides) -> ExtractedInvoice:
    """A clean intra-state invoice: 10 pipes + 20 elbows at 18% GST = 6,136.00."""
    lines = overrides.pop("lines", [
        dict(description="MS Pipe 25mm", hsn_sac="7306", quantity=10, unit_price=450,
             taxable_value=4500, gst_rate=18),
        dict(description="GI Elbow 25mm", hsn_sac="7307", quantity=20, unit_price=35,
             taxable_value=700, gst_rate=18),
    ])
    taxable = sum(l["taxable_value"] for l in lines)
    tax = round(sum(l["taxable_value"] * l["gst_rate"] / 100 for l in lines), 2)
    base = dict(
        document_type="tax_invoice", supplier_name="Sri Lakshmi Steels Pvt Ltd",
        supplier_gstin=SUPPLIER_GSTIN, buyer_gstin="36AABCT1234F1ZR", invoice_number="SLS/26-27/118",
        invoice_date="2026-09-15", voucher_number=None, voucher_date=None, invoice_kind="regular",
        reference_invoice_number=None, currency="INR", place_of_supply_state_code="36", po_number="PO-2026-0042", irn=None,
        lines=lines, taxable_total=taxable, cgst=tax / 2, sgst=tax / 2, igst=0, round_off=0,
        grand_total=taxable + tax, amount_in_words=None, confidence=0.95, low_confidence_fields=[],
    )
    base.update(overrides)
    return ExtractedInvoice(**base)


def not_an_invoice() -> ExtractedInvoice:
    return ExtractedInvoice(
        document_type="other", supplier_name="", supplier_gstin=None, buyer_gstin=None,
        invoice_number="", invoice_date="", voucher_number=None, voucher_date=None, invoice_kind="regular",
        reference_invoice_number=None, currency="INR", place_of_supply_state_code=None, po_number=None, irn=None,
        lines=[], taxable_total=0, cgst=0, sgst=0, igst=0, round_off=0, grand_total=0, amount_in_words=None,
        confidence=0.9, low_confidence_fields=[])
