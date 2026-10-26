"""Measure extraction accuracy on the golden set (golden/). Calls the real AI, so it costs tokens.

    python -m scripts.evaluate                          # the app's normal model choice
    python -m scripts.evaluate --model gpt-4o --model gpt-4o-mini   # compare models
    python -m scripts.evaluate --only hfcl              # documents whose name contains "hfcl"
    python -m scripts.evaluate --min-accuracy 98        # exit code 1 below 98% (use as a gate)

Writes reports/eval-<time>.md and .json. Exit code 1 if key-field accuracy is below
--min-accuracy, if any invoice would be auto-approved with wrong data, or if a document failed.
"""
import argparse
import json
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.evaluation import KEY_FIELDS, DocResult, compare, predict_decision
from app.logs import setup_logging
from app.pipeline.reading import read_document

GOLDEN = Path("golden")
REPORTS = Path("reports")


def load_golden(only: str | None) -> list[tuple[str, dict, Path]]:
    docs = []
    for exp_file in sorted((GOLDEN / "expected").glob("*.json")):
        g = json.loads(exp_file.read_text(encoding="utf-8"))
        name = exp_file.stem
        if only and only.lower() not in name.lower():
            continue
        if not g.get("verified"):
            print(f"  skipping {name}: not verified yet (check the values, then set \"verified\": true)")
            continue
        doc = GOLDEN / "docs" / g["file"]
        if not doc.exists():
            print(f"  skipping {name}: {doc} not found")
            continue
        docs.append((name, g, doc))
    return docs


@contextmanager
def golden_settings():
    """Same rules for every run, whatever .env says (golden/config.json)."""
    cfg_file = GOLDEN / "config.json"
    cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    saved = {k: getattr(settings, k) for k in cfg}
    for k, v in cfg.items():
        setattr(settings, k, v)
    try:
        yield cfg
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)


def run_one(name: str, golden: dict, doc: Path, model: str | None) -> DocResult:
    r = DocResult(name, decision_expected=golden["expected"].get("decision"))
    t0 = time.monotonic()
    try:
        data = read_document(str(doc), golden["file"], model=model).data   # the worker's reading path
    except Exception as e:  # noqa: BLE001 - one bad document shouldn't stop the run
        r.error = f"{type(e).__name__}: {e}"[:300]
        r.seconds = time.monotonic() - t0
        return r
    r.seconds = time.monotonic() - t0
    r.model = data._model
    r.input_tokens = data._usage.get("input_tokens", 0)
    r.output_tokens = data._usage.get("output_tokens", 0)
    r.fields = compare(golden, data)
    if r.decision_expected:
        r.decision_actual, r.decision_reason = predict_decision(data)
    return r


def summarise(results: list[DocResult]) -> dict:
    per_field_total, per_field_ok = Counter(), Counter()
    for r in results:
        for f in r.fields:
            per_field_total[f.name] += 1
            per_field_ok[f.name] += f.ok
    key_total = sum(per_field_total[k] for k in KEY_FIELDS)
    key_ok = sum(per_field_ok[k] for k in KEY_FIELDS)
    all_total, all_ok = sum(per_field_total.values()), sum(per_field_ok.values())
    decided = [r for r in results if r.decision_expected and r.decision_actual]
    return {
        "documents": len(results),
        "failed_documents": sum(1 for r in results if r.error),
        "key_field_accuracy": round(100 * key_ok / key_total, 1) if key_total else None,
        "all_field_accuracy": round(100 * all_ok / all_total, 1) if all_total else None,
        "documents_fully_correct": sum(1 for r in results if not r.error and not r.wrong),
        "decision_accuracy": round(100 * sum(r.decision_actual == r.decision_expected for r in decided)
                                   / len(decided), 1) if decided else None,
        "wrong_auto_approvals": sum(1 for r in results if r.wrong_auto_approve),
        "per_field": {k: f"{per_field_ok[k]}/{per_field_total[k]}" for k in sorted(per_field_total)},
        "avg_seconds": round(sum(r.seconds for r in results) / len(results), 1) if results else 0,
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
    }


def markdown(model_label: str, results: list[DocResult], s: dict, cfg: dict) -> str:
    out = [f"# Evaluation: {model_label}", "",
           f"{datetime.now():%Y-%m-%d %H:%M} · {s['documents']} documents · rules: {cfg or 'from .env'}", "",
           "| Metric | Value |", "|---|---|",
           f"| Key-field accuracy (target ≥ 98%) | {s['key_field_accuracy']}% |",
           f"| All-field accuracy | {s['all_field_accuracy']}% |",
           f"| Documents fully correct | {s['documents_fully_correct']} / {s['documents']} |",
           f"| Decision accuracy | {s['decision_accuracy']}% |",
           f"| **Wrong auto-approvals (target 0)** | **{s['wrong_auto_approvals']}** |",
           f"| Failed documents | {s['failed_documents']} |",
           f"| Avg seconds per document | {s['avg_seconds']} |",
           f"| Tokens in / out | {s['input_tokens']:,} / {s['output_tokens']:,} |", "",
           "## Per field", "", "| Field | Correct |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in s["per_field"].items()]
    out += ["", "## Per document", "", "| Document | Model | Result | Decision | Problems |", "|---|---|---|---|---|"]
    for r in results:
        if r.error:
            out.append(f"| {r.name} | {r.model} | ERROR | | {r.error} |")
            continue
        problems = "; ".join(f"{f.name}: got {f.actual!r}, expected {f.expected!r}" + (f" ({f.note})" if f.note else "")
                             for f in r.wrong) or "—"
        decision = f"{r.decision_actual}" + ("" if r.decision_actual == r.decision_expected
                                              else f" (expected {r.decision_expected})")
        flag = " ⚠ WRONG AUTO-APPROVE" if r.wrong_auto_approve else ""
        out.append(f"| {r.name} | {r.model} | {len(r.fields) - len(r.wrong)}/{len(r.fields)}{flag} | "
                   f"{decision} | {problems.replace('|', '/')} |")
    return "\n".join(out) + "\n"


def main():
    # cmd.exe's code page can't print ✗ or ₹ when the output is redirected: replace them, don't crash
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", action="append", help="force a model for every document (repeat to compare)")
    p.add_argument("--only", help="only documents whose name contains this text")
    p.add_argument("--min-accuracy", type=float, default=None, help="fail below this key-field accuracy (%%)")
    args = p.parse_args()
    setup_logging()

    docs = load_golden(args.only)
    if not docs:
        sys.exit("No verified golden documents found in golden/expected/")
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    failed_gate = False
    with golden_settings() as cfg:
        for model in args.model or [None]:
            label = model or f"default ({settings.openai_model}, scans {settings.openai_scan_model})"
            print(f"\n=== {label}: {len(docs)} document(s)")
            results = []
            for name, golden, doc in docs:
                r = run_one(name, golden, doc, model)
                status = "ERROR " + r.error if r.error else f"{len(r.fields) - len(r.wrong)}/{len(r.fields)} fields"
                print(f"  {name:32} {status:14} decision {r.decision_actual or '-':13} {r.seconds:5.1f}s"
                      + ("  ⚠ WRONG AUTO-APPROVE" if r.wrong_auto_approve else ""))
                for f in r.wrong:
                    print(f"      ✗ {f.name}: got {f.actual!r}, expected {f.expected!r} {f.note}")
                results.append(r)
            s = summarise(results)
            print(f"  -> key fields {s['key_field_accuracy']}% · all fields {s['all_field_accuracy']}% · "
                  f"decisions {s['decision_accuracy']}% · wrong auto-approvals {s['wrong_auto_approvals']} · "
                  f"tokens {s['input_tokens']:,}/{s['output_tokens']:,}")
            tag = (model or "default").replace("/", "_")
            (REPORTS / f"eval-{stamp}-{tag}.md").write_text(markdown(label, results, s, cfg), encoding="utf-8")
            (REPORTS / f"eval-{stamp}-{tag}.json").write_text(json.dumps(
                {"model": label, "summary": s, "documents": [r.__dict__ | {"fields": [f.__dict__ for f in r.fields]}
                                                              for r in results]}, default=str, indent=1),
                encoding="utf-8")
            print(f"  report: reports/eval-{stamp}-{tag}.md")
            if s["wrong_auto_approvals"] or s["failed_documents"] or (
                    args.min_accuracy is not None and (s["key_field_accuracy"] or 0) < args.min_accuracy):
                failed_gate = True
    sys.exit(1 if failed_gate else 0)


if __name__ == "__main__":
    main()
