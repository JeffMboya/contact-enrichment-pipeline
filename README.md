# Contact Enrichment Pipeline

Takes a spreadsheet of messy invoice debtors (`sample_invoices.xlsx`) and, for each
row, finds the best reachable contact at the **debtor** company — ideally an accounts
payable / billing email — with a confidence score and the evidence behind it. The
original challenge brief is in [`CHALLENGE.md`](CHALLENGE.md); the design write-up is in
[`WRITEUP.md`](WRITEUP.md).

The guiding principle is **no hallucinated emails**: an address only reaches the output
if it was found verbatim on a page the pipeline actually fetched. A row we cannot
enrich gets an explicit, explained "not found" state rather than a confident guess.

## Quickstart

Requires Python 3.10+ (uses `X | None` type syntax).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then fill in the two keys (see below)

python run.py               # all 25 rows (default Haiku→Sonnet judge)
python run.py --rows 1,9,10 # just these rows (cheap, cached)
python run.py --limit 5     # first 5 rows
```

The committed `output/enriched_output.xlsx` was generated with the Opus judge. To
reproduce it exactly (otherwise the default ladder differs by a row or two on the hard
tail — see the write-up's "On model choice"):

```bash
RESOLUTION_MODEL=claude-opus-4-8 python run.py
```

Output is written to `output/enriched_output.xlsx` (and `output/enriched_output.jsonl`).
Each run ends with an invariant self-audit (`INVARIANTS: PASS (N emails checked)`); it
exits non-zero if any emitted email is not present verbatim in a fetched page, sits on the
creditor domain, or lacks an MX record.

## Evaluation

Score a run against the hand-verified gold set (`eval/gold.jsonl`):

```bash
python eval/score.py              # scores output/enriched_output.jsonl
```

Reports wrong-contact rate (the headline reliability number), domain accuracy, coverage,
and correct-abstention rate. Rows the gold set can only call *plausible* are tagged
`needs_human` and excluded from the headline, so the score is never graded against a guess.

## Tests

All suites run without keys (the network and LLM boundaries are stubbed or avoided):

```bash
python tests/test_offline.py      # ingestion, cleaning, extraction, scoring, output
python tests/test_tables.py       # table-driven cases for the pure parsers and scorer
python tests/test_guards.py       # SSRF, MX caching, anti-hallucination, creditor autodetect
python tests/test_stubbed_e2e.py  # full resolve→find→score→output with canned I/O
python tests/test_eval.py         # scoring math + the invariant audit's failure modes
```

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SERPER_API_KEY` | yes | Web search ([serper.dev](https://serper.dev), free tier) |
| `ANTHROPIC_API_KEY` | yes | Company resolution + contact-field classification |
| `INPUT_FILE` | no | Defaults to `sample_invoices.xlsx` |
| `OUTPUT_FILE` | no | Defaults to `output/enriched_output.xlsx` |
| `CACHE_DIR` | no | Defaults to `cache/` |
| `MAX_SERPER_CALLS_PER_ROW` | no | Per-row search budget, defaults to `6` |
| `CREDITOR_DOMAINS` | no | The invoicing client's domain(s), defaults to `fedex.com` — never enriched as the debtor |
| `RESOLUTION_MODEL` | no | Override the resolution judge with one stronger model (e.g. `claude-opus-4-8`). Unset = cheap Haiku-then-Sonnet ladder. The `Full name` classifier always stays on Haiku. |

If the keys are missing, `run.py` exits with a clear message. Every Serper and HTTP
call is cached under `cache/`, so re-running is free and deterministic.

## How it works

Six stages, each operating on one `EnrichmentRecord` per row (design detail in
[`WRITEUP.md`](WRITEUP.md)):

1. **Ingestion** — read the workbook by header; flag internal facility rows (FedEx/USPS) as skips.
2. **Cleaning** — strip `(DUNS …)` and legal suffixes; classify the `Full name` field; build search-query variants.
3. **Resolution** — an address-anchored, escalating Serper query ladder; an LLM judge picks the company from real results (never invents a domain) and may propose one grounded follow-up query.
4. **Contact finding** — fetch `/contact`, `/about`, etc.; extract emails by regex from fetched HTML only; a constrained LLM reader annotates name/role but never selects an address. A web-form-only page becomes a `form_only` contact.
5. **Scoring** — a deterministic rubric with a hard MX gate, scaled by resolution confidence.
6. **Output** — the styled worklist spreadsheet (`Next action` column + `Summary` sheet) and a `results.jsonl` sidecar, then the invariant self-audit.

## Output format

Same columns as the input, with six appended: `Status`, `Contact type`, `Confidence`,
`Source URL`, `Evidence`, `Next action`. A second `Summary` sheet gives the one-glance
tally (reachable by email / web-form / manual review / skipped). For each source row, the
contact(s) found are inserted on the row(s) directly **below** it:

- **Found contacts** are filled light yellow; the confidence cell is green (≥0.70),
  amber (0.40–0.69), or red (<0.40).
- **Not-found / skipped** rows are filled light gray with the reason in `Evidence`.

This lets a non-engineer sort or filter by confidence and see at a glance what was
added versus what came from the source.
