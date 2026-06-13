"""Tests for the evaluation harness and the post-run invariant audit.

Run with:  python tests/test_eval.py

No network, no keys. Verifies the scorer's math against a hand-built results +
gold pair, and that the audit catches the failure modes it exists to catch.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
import score as scorer  # noqa: E402

from pipeline.audit import audit_records  # noqa: E402
from pipeline.models import (  # noqa: E402
    Contact,
    ContactType,
    EnrichmentRecord,
    RowStatus,
    SourceRow,
)


def _result(row_id, status, domain=None, email=None):
    res = {"domain": domain} if domain else None
    contacts = [{"email": email}] if email else []
    return {"source": {"row_id": row_id}, "status": status, "resolution": res, "contacts": contacts}


def test_score_math():
    gold = [
        # correct enriched, domain + email match
        {"row_id": 1, "company": "A", "expected_domain": "a.com", "email_findable": True,
         "ideal_verdict": "enriched", "verified_by": "human_session"},
        # correct abstention: no email should be findable, pipeline emitted none
        {"row_id": 2, "company": "B", "expected_domain": None, "email_findable": False,
         "ideal_verdict": "not_found", "verified_by": "human_session"},
        # Gold says not findable but the pipeline emitted one.
        {"row_id": 3, "company": "C", "expected_domain": "c.com", "email_findable": False,
         "ideal_verdict": "not_found", "verified_by": "human_session"},
        # excluded from headline (needs_human)
        {"row_id": 4, "company": "D", "expected_domain": "d.com", "email_findable": True,
         "ideal_verdict": "enriched", "verified_by": "needs_human"},
        # A skipped facility is not counted in coverage.
        {"row_id": 5, "company": "E", "expected_domain": None, "email_findable": False,
         "ideal_verdict": "skipped", "verified_by": "obvious"},
    ]
    results = [
        _result(1, "enriched", "a.com", "ap@a.com"),
        _result(2, "not_found"),
        _result(3, "enriched", "c.com", "info@c.com"),   # should be flagged wrong
        _result(4, "enriched", "d.com", "x@d.com"),
        _result(5, "skipped"),
    ]
    m = scorer.score(results, gold)

    assert m["trusted_rows"] == 4, m["trusted_rows"]               # excludes needs_human
    assert m["emails_emitted"] == 2, m["emails_emitted"]           # rows 1 and 3 (trusted)
    assert m["wrong_contact_rate"] == 0.5, m["wrong_contact_rate"]  # row 3 of 2 emitted
    assert any("row 3" in w for w in m["wrong_contacts"]), m["wrong_contacts"]
    assert m["domain_hits"] == "2/2", m["domain_hits"]             # rows 1,3 resolved correctly
    # Coverage counts enriched rows regardless of contact correctness.
    assert m["coverage_hits"] == "2/3", m["coverage_hits"]
    assert m["abstention_hits"] == "1/2", m["abstention_hits"]
    assert "row 4 (D)" in m["deferred_rows"][0], m["deferred_rows"]
    assert m["missing_rows"] == [] and m["scored_rows"] == 4, (m["scored_rows"], m["missing_rows"])

    # A gold row with no result is reported.
    partial = scorer.score(results[:1], gold)
    assert partial["scored_rows"] == 1 and len(partial["missing_rows"]) == 3, partial["missing_rows"]
    print("score math: OK")


def _contact(email, mx=True, src="https://acme.com/contact"):
    return Contact(email, None, None, ContactType.GENERIC, src, email or "form", mx, "exact")


def test_audit_invariants():
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        fetch = cache / "fetch"
        fetch.mkdir()
        # A cached page that contains one of the emails verbatim.
        (fetch / "page.json").write_text(
            '{"status": 200, "html": "<p>reach us at ap@acme.com</p>"}', encoding="utf-8"
        )
        row = SourceRow(1, {}, "x", "addr", "ACME", "+1 555")

        good = EnrichmentRecord(source=row, contacts=[_contact("ap@acme.com")], status=RowStatus.ENRICHED)
        report = audit_records([good], cache, lambda d: False)
        assert report.ok and report.emails_checked == 1, report.violations

        # Hallucinated email: not present in any fetched page.
        ghost = EnrichmentRecord(source=row, contacts=[_contact("ghost@acme.com")], status=RowStatus.ENRICHED)
        report = audit_records([ghost], cache, lambda d: False)
        assert not report.ok and any("verbatim" in v for v in report.violations), report.violations

        # Creditor mailbox leaked.
        cred = EnrichmentRecord(source=row, contacts=[_contact("ap@acme.com")], status=RowStatus.ENRICHED)
        report = audit_records([cred], cache, lambda d: d == "acme.com")
        assert not report.ok and any("creditor" in v for v in report.violations), report.violations

        # The audit catches an email that skipped the MX gate.
        nomx = EnrichmentRecord(source=row, contacts=[_contact("ap@acme.com", mx=False)], status=RowStatus.ENRICHED)
        report = audit_records([nomx], cache, lambda d: False)
        assert not report.ok and any("MX" in v for v in report.violations), report.violations
    print("audit invariants: OK")


def main() -> None:
    test_score_math()
    test_audit_invariants()
    print("\nAll eval tests passed.")


if __name__ == "__main__":
    main()
