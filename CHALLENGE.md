# Take-home: Contact Enrichment Pipeline

**Time budget:** ~4–6 hours. We don't want you to build a production system — we want
to see how you reason about a messy, real-world data problem and how you make an
LLM + external APIs cooperate reliably.

---

## The problem

We collect overdue invoices on behalf of our clients. Each client hands us a
spreadsheet of debtors that looks like `sample_invoices.xlsx` (attached). The rows are
messy exports from accounting systems: company names carry registration codes, the
"Full name" is sometimes a department or the company itself, and **the email column is
almost always empty**.

To chase a payment we need a **reachable, correct contact** — ideally the person in the
debtor company who handles accounts payable, otherwise a usable corporate contact.

Your task: **build a pipeline that takes these raw rows and, for each one, finds the
best contact you can — at minimum a valid corporate email — and outputs the enriched
result with enough metadata for a human reviewer to trust (or reject) each row.**

## Input data

`sample_invoices.xlsx` — 25 real (anonymized) rows. Relevant columns:

| Column | Meaning | Notes |
|---|---|---|
| `Full name` | the debtor contact on the invoice | may be a person, a department ("Invoicing"), or the company name itself |
| `Address` | the **debtor's** address | useful for disambiguating which "Acme" you found |
| `Company name` | the debtor company | often has `(DUNS N° …)` or a legal form (`LLC`, `INC`) appended |
| `Email` | contact email | **empty in this dataset — this is what you're finding** |
| `Phone number` | a phone on file | usually the debtor's switchboard / AP desk |
| `Company issuing the invoice` / `Company address` | **our client (FedEx here)** | ignore for enrichment — this is *not* the debtor |
| `Invoice number`, `Issue date`, amounts | invoice details | context only |

> Note: `Company issuing the invoice` is the creditor, not the target. A common mistake
> is enriching the wrong company.

## What we'd like to see

This is **open-ended** — design the pipeline yourself. We're not looking for a specific
architecture. A strong submission typically:

1. **Resolves the company** behind a messy name (e.g. `LAKE CABLE LLC (DUNS N° 927410308)`
   → the real company and ideally its corporate domain). Think about how you'd verify
   you found the *right* company and not a same-named one elsewhere.
2. **Finds a contact** — at least a valid email. Web search, the company website, and
   public sources are all fair game. Be explicit about how you avoid inventing /
   hallucinating emails.
3. **Validates the result** so a reviewer can trust it — e.g. a confidence signal, the
   evidence/source for each contact, and a clear "couldn't enrich this one" state rather
   than a confident wrong answer.
4. **Outputs** the enriched data in a form a non-engineer could review. **We want a
   spreadsheet that looks like the input**, but for each original row, insert the
   contact(s) you found on the row(s) directly **below** it. Make these enriched rows
   visually distinct — **fill them with a different background color** so a reviewer can
   tell at a glance what was added vs. what came from the source. **Each found contact
   carries its own confidence score** (e.g. a 0–1 column) so rows can be ranked / filtered.

Handle the long tail gracefully: some of these companies are tiny or have no web
presence. A pipeline that says "no contact found, here's why" for those is **better**
than one that guesses.

## Tooling

Use whatever you like. Some suggestions to keep it free/cheap:

- **Web search / Google: [Serper](https://serper.dev)** — has a free tier (a few
  thousand queries) and a simple JSON API. We recommend it; it's enough for this task.
- **LLM:** any provider. If you want to mirror our stack, Anthropic's Claude works well,
  but OpenAI / a local model / etc. are all fine.
- Scraping a company's `/contact` or `/about` page is a legitimate source.
- Paid contact-data APIs (RocketReach, Hunter, etc.) are **not required** — don't spend
  money. If your design *would* use one, just describe where it'd slot in.

Don't hardcode answers for these 25 rows — the pipeline should generalize.

## Deliverables

1. **Code** — runnable, with a short `README` saying how to run it and what env vars /
   keys it needs.
2. **Output** — an enriched spreadsheet for the 25 sample rows, in the format above:
   same columns as the input, with each found contact added as a **color-highlighted row
   directly beneath its source row** and a **per-contact confidence score**. (If you
   can't produce styled cells, a clearly-labelled CSV/JSONL fallback is acceptable, but
   the colored-rows spreadsheet is what we're asking for.)
3. **A short write-up (≈1 page)** covering:
   - your approach and the main design decisions,
   - how you guard against wrong/hallucinated contacts,
   - what you'd do with more time, and how you'd evaluate quality at scale (1000s of rows).

## How we evaluate

| Area | What we look for |
|---|---|
| **Correctness** | Are the contacts plausibly right? Did you target the debtor, not the creditor? |
| **Judgment** | Sensible handling of ambiguity, the long tail, and "don't know" cases |
| **Reliability** | No hallucinated emails; failures are explicit and explained |
| **Code quality** | Clear, runnable, reasonably structured — not necessarily production-grade |
| **Communication** | The write-up shows you understand the trade-offs you made |

We care more about **good reasoning on a hard subset** than about enriching all 25 rows.
Tell us what you'd skip and why.

Have fun — and reach out if anything is ambiguous.
