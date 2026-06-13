"""Entry point: run the contact-enrichment pipeline over the invoice rows."""

from __future__ import annotations

import argparse

from pipeline.audit import audit_records
from pipeline.cleaning import clean
from pipeline.config import load_config
from pipeline.contact_finder import find_contacts
from pipeline.ingestion import detect_creditor_names, is_internal_facility, load_rows
from pipeline.llm import LLM
from pipeline.cache import Cache
from pipeline.models import (
    BUDGET_STOP_PREFIX,
    EnrichmentRecord,
    NotFoundReason,
    RowStatus,
    SourceRow,
)
from pipeline.output import write_output
from pipeline.resolution import is_creditor_domain, resolve
from pipeline.scorer import score_contacts
from pipeline.web import SearchBudget


def _select(rows: list[SourceRow], limit: int | None, row_ids: set[int] | None):
    if row_ids:
        rows = [r for r in rows if r.row_id in row_ids]
    if limit is not None:
        rows = rows[:limit]
    return rows


def _process(row: SourceRow, cache: Cache, cfg, llm: LLM) -> EnrichmentRecord:
    record = EnrichmentRecord(source=row)

    if is_internal_facility(row.company_name, row.full_name):
        record.parsed = clean(row)  # Skipped rows need no LLM classification.
        record.status = RowStatus.SKIPPED
        record.not_found_reason = NotFoundReason.INTERNAL_FACILITY
        record.not_found_explanation = (
            "Internal facility record (FedEx/USPS logistics node), not a debtor company."
        )
        return record

    parsed = clean(row, llm)
    record.parsed = parsed
    budget = SearchBudget(cfg.max_serper_calls_per_row)
    resolution = resolve(parsed, row, cache, cfg, llm, budget)
    record.resolution = resolution
    record.queries_used = budget.used

    if not resolution.domain:
        record.status = RowStatus.NOT_FOUND
        # The reason reflects what resolution found, not the query count.
        budget_stopped = any(
            e.startswith(BUDGET_STOP_PREFIX) for e in resolution.evidence
        )
        if resolution.candidates_considered:
            record.not_found_reason = NotFoundReason.AMBIGUOUS_MATCH
            fallback = (
                "Search found possible matches, but none could be confirmed "
                "as this company's own site."
            )
        elif budget_stopped:
            record.not_found_reason = NotFoundReason.BUDGET_EXHAUSTED
            fallback = "Query budget exhausted before a confident match was found."
        else:
            record.not_found_reason = NotFoundReason.NO_WEB_PRESENCE
            fallback = "No web presence found for this company at this location."
        # Surface the judge's most informed verdict to the reviewer.
        detail = next(
            (
                e
                for e in reversed(resolution.evidence)
                if e.strip() and not e.startswith(BUDGET_STOP_PREFIX)
            ),
            None,
        )
        record.not_found_explanation = detail or fallback
        return record

    contacts = find_contacts(resolution, parsed, row, cache, cfg, budget, llm)
    record.queries_used = budget.used
    scored = score_contacts(contacts, resolution, parsed, row)
    record.contacts = scored
    if any(c.email for c in scored):
        record.status = RowStatus.ENRICHED
    elif scored:  # A form is actionable but is not a valid email.
        record.status = RowStatus.PARTIAL
        record.not_found_reason = NotFoundReason.NO_EMAIL_PUBLISHED
        record.not_found_explanation = (
            f"No email published; contact form available at {scored[0].source_url}."
        )
    else:
        record.status = RowStatus.PARTIAL
        record.not_found_reason = NotFoundReason.NO_EMAIL_PUBLISHED
        record.not_found_explanation = (
            f"Company site resolved ({resolution.domain}) but no contact email "
            f"or form was found on its public pages."
        )
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Contact enrichment pipeline")
    parser.add_argument("--limit", type=int, help="process only the first N rows")
    parser.add_argument("--rows", help="comma-separated row ids, e.g. 1,2,9")
    args = parser.parse_args()

    cfg = load_config()
    cfg.require_live_keys()

    rows = load_rows(cfg.input_file)
    cfg.creditor_names = detect_creditor_names(rows)
    if cfg.creditor_names:
        print(
            "Creditor(s) detected from input, excluded from enrichment: "
            + ", ".join(cfg.creditor_names)
        )
    row_ids = {int(x) for x in args.rows.split(",")} if args.rows else None
    selected = _select(rows, args.limit, row_ids)

    cache = Cache(cfg.cache_dir)
    llm = LLM(cache)

    records: list[EnrichmentRecord] = []
    for row in selected:
        try:
            record = _process(row, cache, cfg, llm)
        except Exception as error:  # Crash isolation stops one bad row killing the run.
            record = EnrichmentRecord(source=row, parsed=clean(row))
            record.status = RowStatus.NOT_FOUND
            record.not_found_reason = NotFoundReason.ERROR
            record.not_found_explanation = (
                f"Unexpected error while processing this row "
                f"({type(error).__name__}: {error})."
            )
        records.append(record)
        domain = record.resolution.domain if record.resolution else None
        top = record.contacts[0].confidence if record.contacts else 0.0
        print(
            f"[{record.source.row_id:>2}] {record.status.value:9} "
            f"q={record.queries_used} domain={domain or '-'} "
            f"conf={top:.2f} {record.source.company_name[:40]}"
        )

    write_output(records, cfg.input_file, cfg.output_file)

    counts = {s: 0 for s in ("enriched", "partial", "skipped", "not_found")}
    for record in records:
        counts[record.status.value] += 1
    print(
        f"\nDone: {counts['enriched']} enriched, {counts['partial']} partial, "
        f"{counts['skipped']} skipped, {counts['not_found']} not found. "
        f"Output: {cfg.output_file}"
    )

    # A failed invariant exits non-zero so it cannot ship.
    report = audit_records(records, cfg.cache_dir, lambda d: is_creditor_domain(d, cfg))
    if report.ok:
        print(f"INVARIANTS: PASS ({report.emails_checked} emails checked)")
    else:
        print(f"INVARIANTS: FAIL ({len(report.violations)} violation(s)):")
        for violation in report.violations:
            print(f"  - {violation}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
