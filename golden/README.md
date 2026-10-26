# Golden test set

Real documents with their correct values, checked by a person. `python -m scripts.evaluate` reads every
document here with the AI and scores the result, so changes to the prompt, model or rules are measured
instead of guessed.

    docs/         the documents (PDF/images). Real supplier data: keep local, don't commit.
    expected/     one JSON per document with the correct values ("verified": true when checked)
    config.json   the rules used during evaluation (your GSTINs, auto-approve limit), so results
                  don't change when .env changes

## Adding documents

The target is 50–100, covering every kind you receive: invoices (text PDF, scan, phone photo,
multi-page), credit/debit notes, journal vouchers, foreign-currency bills, and documents that are not
bills (terms, quotations, statements).

Easiest way, from a document already processed and reviewed in the app:

    python -m scripts.golden_add <invoice id>

This copies the file and pre-fills `expected/<name>.json` from the reviewed values with
`"verified": false`. Open the JSON next to the document, check every value, then set `"verified": true`.

Rules for the JSON:
- List only the fields you are sure of; unlisted fields are not scored.
- Amounts: plain numbers (`1065908.16`). Dates: `YYYY-MM-DD`. Missing on the document: `null`.
- `lines`: one object per item; only the keys you list are compared (quantity, unit_price,
  taxable_value, gst_rate, hsn_sac, description).
- `decision`: what the rules should decide offline: `APPROVED`, `NEEDS_REVIEW` or `NOT_INVOICE`
  (journals: `NEEDS_REVIEW`; duplicates can't be judged offline, so leave it out for those).
- `alternatives`: other readings that are also correct, e.g. `{"invoice_number": ["B025260106"]}`
  when a typewriter O and 0 can't be told apart.

## Running

    python -m scripts.evaluate                                     # normal model choice
    python -m scripts.evaluate --model gpt-4o --model gpt-4o-mini  # compare models
    python -m scripts.evaluate --only iti                          # some documents
    python -m scripts.evaluate --min-accuracy 98                   # exit code 1 below 98% (gate)

Reports go to `reports/eval-<time>-<model>.md` (and `.json`). The run fails (exit code 1) if any
invoice would be auto-approved with wrong data, if a document errors, or if key-field accuracy is
below `--min-accuracy`.
