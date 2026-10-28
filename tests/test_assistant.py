"""AI assistant: its tools (real database, no AI) and the chat endpoint (fake AI)."""
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import assistant, assistant_tools as T
from app.api import app
from app.config import settings
from app.excel import export_pending
from app.models import EmailMessage, Invoice, Status
from tests.conftest import make_extracted, OTHER_GSTIN, SUPPLIER_GSTIN


def add(session, users, n, status, **kw):
    data = make_extracted(**kw.pop("extracted", {}))
    d = data.model_dump()
    inv = Invoice(user_id=users["priya"].id, source=kw.pop("source", "upload"), original_filename=f"inv{n}.pdf",
                  file_path=f"inv{n}.pdf", file_hash=f"{n:064d}", status=status, extracted=d,
                  supplier_name=d["supplier_name"], supplier_gstin=d["supplier_gstin"],
                  invoice_no=d["invoice_number"], invoice_date=d["invoice_date"], grand_total=d["grand_total"], **kw)
    session.add(inv)
    session.commit()
    return inv


@pytest.fixture
def data(session, users):
    """Four documents from two suppliers in three statuses, plus a credit note."""
    a = add(session, users, 1, Status.EXPORTED, approved_by="auto")
    b = add(session, users, 2, Status.NEEDS_REVIEW, status_reason="Amount above auto-approve limit",
            extracted={"invoice_number": "SLS/26-27/119", "invoice_date": "2026-08-10"})
    c = add(session, users, 3, Status.EXPORTED, extracted={
        "supplier_name": "Bangalore Fittings", "supplier_gstin": OTHER_GSTIN, "invoice_number": "BF-9",
        "place_of_supply_state_code": "29", "invoice_date": "2026-08-20"}, source="gmail")
    cn = add(session, users, 4, Status.EXPORTED, extracted={
        "document_type": "credit_note", "invoice_number": "CN-1", "invoice_date": "2026-09-20"})
    return {"a": a, "b": b, "c": c, "credit": cn}


def j(text):
    return json.loads(text)


# ---------- tools

def test_search_invoices_filters(data):
    assert j(T.search_invoices())["matches"] == 4
    r = j(T.search_invoices(status="NEEDS_REVIEW"))
    assert [i["invoice_id"] for i in r["invoices"]] == [data["b"].id]
    assert r["invoices"][0]["file_url"] == f"/api/v1/invoices/{data['b'].id}/file"
    assert j(T.search_invoices(supplier="bangalore"))["matches"] == 1
    assert j(T.search_invoices(text="SLS/26-27/119"))["matches"] == 1
    assert j(T.search_invoices(date_from="2026-09-01"))["matches"] == 2
    assert j(T.search_invoices(document_type="credit_note"))["invoices"][0]["invoice_id"] == data["credit"].id
    assert j(T.search_invoices(status="exported,needs_review", limit=1))["matches"] == 4


def test_get_invoice_has_everything(data, session):
    session.add(EmailMessage(user_id=data["c"].user_id, message_id="m9", sender="v@x.com", subject="Bill",
                             outcome="done", attachments=[]))
    data["c"].source_ref = "m9/inv3.pdf"
    session.commit()
    r = j(T.get_invoice(data["c"].id))
    assert r["extracted"]["invoice_number"] == "BF-9" and r["email"]["subject"] == "Bill"
    assert r["user"] == "Priya" and "history" in r
    assert "error" in j(T.get_invoice(999))


def test_statistics(data):
    by_status = {g["group"]: g for g in j(T.invoice_statistics("status"))["groups"]}
    # 2 exported invoices (+6136 each) and an exported credit note (-6136)
    assert by_status["EXPORTED"]["count"] == 3 and by_status["EXPORTED"]["grand_total"] == 6136
    by_supplier = {g["group"]: g["count"] for g in j(T.invoice_statistics("supplier"))["groups"]}
    assert by_supplier == {"Sri Lakshmi Steels Pvt Ltd": 3, "Bangalore Fittings": 1}
    months = {g["group"] for g in j(T.invoice_statistics("month"))["groups"]}
    assert months == {"2026-09", "2026-08"}
    rates = j(T.invoice_statistics("gst_rate"))["groups"]
    assert rates[0]["group"] == "18%"


def test_read_excel(data, session):
    assert "error" in j(T.read_excel())                    # nothing approved yet
    for inv in (data["a"], data["c"]):
        inv.status = Status.APPROVED
    session.commit()
    export_pending(session)
    r = j(T.read_excel())
    assert r["matching_rows"] == 2 and r["totals"]["Grand Total"] == 12272 and r["download_url"] == "/api/v1/excel?year=2026-27" and r["year"] == "2026-27"
    assert j(T.read_excel(search="bangalore"))["matching_rows"] == 1
    assert j(T.read_excel(sheet="Line Items"))["matching_rows"] == 4
    assert "sheets" in j(T.read_excel(sheet="Nope"))


def test_needs_attention_and_suppliers(data):
    r = j(T.needs_attention())
    assert [i["invoice_id"] for i in r["needs_review"]] == [data["b"].id] and r["failed"] == []
    s = j(T.supplier_summary())["suppliers"]
    sls = next(x for x in s if x["name"] == "Sri Lakshmi Steels Pvt Ltd")
    assert sls["documents"] == 3 and sls["in_review_total"] == 6136 and sls["gstins"] == [SUPPLIER_GSTIN]


def test_export_files_and_download(data, users):
    r = j(T.export_invoices(status="EXPORTED"))
    assert r["rows"] == 3 and r["download_url"].startswith("/api/v1/assistant/files/export-")
    csv_r = j(T.export_invoices(supplier="bangalore", file_format="csv"))
    with TestClient(app) as client:
        x = client.get(r["download_url"])
        assert x.status_code == 200 and x.content[:2] == b"PK"
        c = client.get(csv_r["download_url"])
        assert c.status_code == 200 and "Bangalore Fittings" in c.content.decode("utf-8-sig")
        assert client.get("/api/v1/assistant/files/..%2F..%2F.env").status_code == 404
        assert client.get("/api/v1/assistant/files/secret.xlsx").status_code == 404
    from pathlib import Path
    from app.config import settings
    wb = load_workbook(Path(settings.export_dir) / r["file_name"])
    assert wb.sheetnames == ["Invoices", "Line Items"] and wb["Invoices"].max_row == 4


@pytest.mark.parametrize("payload", ["=1+1", "+1+1", "-1+1", "@SUM(1,1)", " \t\r\n=1+1"])
def test_exports_keep_untrusted_text_and_numeric_amounts(session, users, payload):
    inv = add(session, users, 1, Status.NEEDS_REVIEW, extracted={
        "supplier_name": payload, "invoice_number": payload,
        "grand_total": -118, "round_off": -0.5,
        "lines": [dict(description=payload, hsn_sac=payload, quantity=-1,
                       unit_price=100, taxable_value=-100, gst_rate=18)],
    })
    inv.original_filename = payload
    session.commit()
    expected = "'" + payload

    exported = j(T.export_invoices())
    wb = load_workbook(Path(settings.export_dir) / exported["file_name"], data_only=False)
    try:
        for address in ("D2", "G2", "Q2"):
            assert wb["Invoices"][address].value == expected
            assert wb["Invoices"][address].data_type == "s"
        for address in ("B2", "D2", "E2"):
            assert wb["Line Items"][address].value == expected
            assert wb["Line Items"][address].data_type == "s"
        assert wb["Invoices"]["N2"].value == -118
        assert wb["Invoices"]["N2"].data_type == "n"
        assert wb["Line Items"]["F2"].value == -1
    finally:
        wb.close()

    exported = j(T.export_invoices(file_format="csv"))
    with (Path(settings.export_dir) / exported["file_name"]).open(encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    for field in ("Supplier", "Invoice No", "File"):
        assert row[field] == expected
    assert float(row["Grand Total"]) == -118
    assert float(row["Round Off"]) == -0.5
    session.refresh(inv)
    assert inv.supplier_name == payload  # escaping is only for the output


def test_exports_remove_illegal_control_characters(session, users):
    add(session, users, 1, Status.NEEDS_REVIEW, extracted={"supplier_name": "Good\x01Supplier"})
    for fmt in ("xlsx", "csv"):
        exported = j(T.export_invoices(file_format=fmt))
        path = Path(settings.export_dir) / exported["file_name"]
        if fmt == "csv":
            with path.open(encoding="utf-8-sig", newline="") as fh:
                assert next(csv.DictReader(fh))["Supplier"] == "GoodSupplier"
        else:
            wb = load_workbook(path)
            try:
                assert wb["Invoices"]["D2"].value == "GoodSupplier"
            finally:
                wb.close()


def test_propose_action_changes_nothing(data, session):
    r = j(T.propose_action(data["b"].id, "approve", "All checks passed; only over the limit"))
    assert r["proposal"]["action"] == "approve"
    session.refresh(data["b"])
    assert data["b"].status == Status.NEEDS_REVIEW            # nothing happened
    bad = j(T.propose_action(data["a"].id, "approve", "x"))
    assert "isn't possible" in bad["error"] and bad["possible_actions"] == []
    assert "Unknown action" in j(T.propose_action(data["b"].id, "delete", "x"))["error"]


# ---------- chat endpoint (fake AI)

class AsyncRun:
    """Agno's arun: awaitable result, or an async stream of events with stream=True."""
    def arun(self, input, stream=False, **kw):
        if stream:
            async def events():
                for ev in self.run(input, stream=True, **kw):
                    yield ev
            return events()

        async def once():
            return self.run(input)
        return once()


class FakeAgent(AsyncRun):
    def __init__(self, content, tools=(), status="completed"):
        self.content, self.tools, self.status, self.seen = content, list(tools), status, None

    def run(self, input):
        self.seen = input
        return SimpleNamespace(content=self.content, tools=self.tools, status=self.status)


def tool_run(name, args, result):
    return SimpleNamespace(tool_name=name, tool_args=args, result=result)


def test_chat_returns_reply_files_and_actions(data, users, monkeypatch):
    export = T.export_invoices(status="EXPORTED")
    proposal = T.propose_action(data["b"].id, "approve", "checked")
    fake = FakeAgent("Here you go: [Download](/api/v1/x)", tools=[
        tool_run("export_invoices", {"status": "EXPORTED"}, export),
        tool_run("propose_action", {"invoice_id": data["b"].id}, proposal),
        tool_run("search_invoices", {}, T.search_invoices())])
    monkeypatch.setattr(assistant, "_agent", lambda: fake)
    with TestClient(app) as client:
        r = client.post("/api/v1/assistant/chat", headers={"X-User-Id": str(users["priya"].id)}, json={
            "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                         {"role": "user", "content": "export approved invoices"}]})
    body = r.json()
    assert r.status_code == 200 and body["reply"].startswith("Here you go")
    assert [t["tool"] for t in body["tools_used"]] == ["export_invoices", "propose_action", "search_invoices"]
    assert body["files"][0]["url"] == j(export)["download_url"] and body["files"][0]["rows"] == 3
    assert body["actions"][0]["invoice_id"] == data["b"].id
    assert [m.role for m in fake.seen] == ["user", "assistant", "user"]


def test_chat_errors(users, monkeypatch):
    h = {"X-User-Id": str(users["priya"].id)}
    with TestClient(app) as client:
        monkeypatch.setattr(assistant, "_agent", lambda: FakeAgent("Connection error.", status="RunStatus.error"))
        assert client.post("/api/v1/assistant/chat", headers=h,
                           json={"messages": [{"role": "user", "content": "x"}]}).status_code == 503
        assert client.post("/api/v1/assistant/chat", headers=h,
                           json={"messages": [{"role": "assistant", "content": "x"}]}).status_code == 422
        assert client.post("/api/v1/assistant/chat", json={"messages": [{"role": "user", "content": "x"}]}).status_code == 422
        assert client.get("/api/v1/assistant/suggestions").json()[0] == "What needs my attention today?"


def test_statistics_leave_out_copies_and_separate_currencies(data, session, users):
    add(session, users, 7, Status.DUPLICATE, extracted={"document_type": "journal_entry", "invoice_number": "X"})
    add(session, users, 8, Status.NEEDS_REVIEW, extracted={"currency": "USD", "invoice_number": "US-1"})
    r = j(T.invoice_statistics("supplier"))
    groups = {g["group"]: g for g in r["groups"]}
    assert r["left_out_duplicates_rejected_etc"] == 1
    assert groups["Sri Lakshmi Steels Pvt Ltd"]["count"] == 3          # the journal copy isn't counted
    assert "Sri Lakshmi Steels Pvt Ltd (USD)" in groups                  # dollars kept apart from rupees
    assert j(T.invoice_statistics("status", status="DUPLICATE"))["documents"] == 1   # asked for explicitly


def test_excel_dates_are_plain_dates(data, session):
    data["a"].status = Status.APPROVED
    session.commit()
    export_pending(session)
    assert j(T.read_excel())["rows"][0]["Invoice Date"] == "2026-09-15"


# ---------- saved conversations (fake AI)

class FakeStreamAgent(AsyncRun):
    """Streams a tool call, then the answer in two pieces (or an error event)."""
    def __init__(self, pieces=("Hello ", "there"), tool=None, error=None):
        self.pieces, self.tool, self.error, self.seen = pieces, tool, error, None

    def run(self, input, stream=False, stream_events=False):
        self.seen = input
        if self.tool:
            yield SimpleNamespace(event="ToolCallStarted", tool=self.tool)
            yield SimpleNamespace(event="ToolCallCompleted", tool=self.tool)
        if self.error:
            yield SimpleNamespace(event="RunError", content=self.error)
            return
        for p in self.pieces:
            yield SimpleNamespace(event="RunContent", content=p)


def sse_events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_chat_sessions_saved_and_private(users, monkeypatch):
    priya, other = {"X-User-Id": str(users["priya"].id)}, {"X-User-Id": str(users["ravi"].id)}
    monkeypatch.setattr(assistant, "_agent", lambda: FakeAgent("Two invoices."))
    with TestClient(app) as client:
        chat = client.post("/api/v1/assistant/sessions", headers=priya).json()
        assert chat["title"] == "New chat" and chat["messages"] == 0
        r = client.post(f"/api/v1/assistant/sessions/{chat['id']}/messages", headers=priya,
                        json={"content": "How many invoices need review?"})
        assert r.status_code == 200 and r.json()["content"] == "Two invoices."
        full = client.get(f"/api/v1/assistant/sessions/{chat['id']}", headers=priya).json()
        assert [m["role"] for m in full["messages"]] == ["user", "assistant"]
        assert full["title"] == "How many invoices need review?"            # named after the first question
        # another user can't see, rename or delete it
        assert client.get("/api/v1/assistant/sessions", headers=other).json() == []
        assert client.get(f"/api/v1/assistant/sessions/{chat['id']}", headers=other).status_code == 404
        assert client.delete(f"/api/v1/assistant/sessions/{chat['id']}", headers=other).status_code == 404
        assert client.patch(f"/api/v1/assistant/sessions/{chat['id']}", headers=priya,
                            json={"title": "Review count"}).json()["title"] == "Review count"
        listed = client.get("/api/v1/assistant/sessions", headers=priya).json()
        assert [(c["title"], c["messages"]) for c in listed] == [("Review count", 2)]
        assert client.delete(f"/api/v1/assistant/sessions/{chat['id']}", headers=priya).status_code == 204
        assert client.get("/api/v1/assistant/sessions", headers=priya).json() == []


def test_chat_history_is_sent_to_the_model(users, monkeypatch):
    h = {"X-User-Id": str(users["priya"].id)}
    fake = FakeAgent("ok")
    monkeypatch.setattr(assistant, "_agent", lambda: fake)
    with TestClient(app) as client:
        cid = client.post("/api/v1/assistant/sessions", headers=h).json()["id"]
        client.post(f"/api/v1/assistant/sessions/{cid}/messages", headers=h, json={"content": "first"})
        monkeypatch.setattr(assistant, "_agent", lambda: FakeAgent("Connection error.", status="RunStatus.error"))
        assert client.post(f"/api/v1/assistant/sessions/{cid}/messages", headers=h,
                           json={"content": "second"}).status_code == 503
        monkeypatch.setattr(assistant, "_agent", lambda: fake)
        client.post(f"/api/v1/assistant/sessions/{cid}/messages", headers=h, json={"content": "third"})
        roles = [m["role"] for m in client.get(f"/api/v1/assistant/sessions/{cid}", headers=h).json()["messages"]]
    assert roles == ["user", "assistant", "user", "error", "user", "assistant"]
    # the saved error isn't sent to the model; the earlier turns are
    assert [(m.role, m.content) for m in fake.seen] == [("user", "first"), ("assistant", "ok"),
                                                        ("user", "second"), ("user", "third")]


def test_streamed_answer_is_saved(data, users, monkeypatch):
    h = {"X-User-Id": str(users["priya"].id)}
    proposal = T.propose_action(data["b"].id, "approve", "checked")
    tool = tool_run("propose_action", {"invoice_id": data["b"].id}, proposal)
    monkeypatch.setattr(assistant, "_agent", lambda: FakeStreamAgent(tool=tool))
    with TestClient(app) as client:
        cid = client.post("/api/v1/assistant/sessions", headers=h).json()["id"]
        r = client.post(f"/api/v1/assistant/sessions/{cid}/stream", headers=h, json={"content": "anything to approve?"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        evs = sse_events(r.text)
        assert [e["type"] for e in evs] == ["tool", "delta", "delta", "saved"]
        saved = evs[-1]["message"]
        assert saved["content"] == "Hello there" and saved["actions"][0]["invoice_id"] == data["b"].id
        assert evs[-1]["title"] == "anything to approve?"
        # mark the suggested action as done; it stays done when the chat is reopened
        url = f"/api/v1/assistant/messages/{saved['id']}/actions/0/done"
        assert client.post(url, headers=h, json={"label": "Approve done"}).status_code == 200
        assert client.post(url, headers={"X-User-Id": str(users["ravi"].id)},
                           json={"label": "x"}).status_code == 404
        assert client.post(f"/api/v1/assistant/messages/{saved['id']}/actions/5/done", headers=h,
                           json={"label": "x"}).status_code == 404
        msgs = client.get(f"/api/v1/assistant/sessions/{cid}", headers=h).json()["messages"]
        assert msgs[-1]["actions"][0]["done"] == "Approve done"

        monkeypatch.setattr(assistant, "_agent", lambda: FakeStreamAgent(error="Connection error."))
        evs = sse_events(client.post(f"/api/v1/assistant/sessions/{cid}/stream", headers=h,
                                     json={"content": "again"}).text)
        assert evs[-1]["type"] == "error" and "unavailable" in evs[-1]["message"]
        roles = [m["role"] for m in client.get(f"/api/v1/assistant/sessions/{cid}", headers=h).json()["messages"]]
        assert roles == ["user", "assistant", "user", "error"]
