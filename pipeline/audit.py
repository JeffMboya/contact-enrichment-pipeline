"""Post-run invariant audit: the anti-hallucination guarantee, self-checked.

The pipeline promises three things about every email it outputs. This module
re-checks them against the evidence on disk after the run, independently of the
stages that produced them, so a regression becomes a hard failure instead of a
silent wrong contact:

  1. The email appears verbatim in a page the pipeline actually fetched
     (cache/fetch), after the same de-obfuscation the extractor applies.
  2. Its domain is not the creditor's.
  3. It passed the MX gate (a domain that can receive mail).

run.py runs this at the end and exits non-zero on any violation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .contact_finder import _deobfuscate
from .models import EnrichmentRecord


@dataclass
class AuditReport:
    emails_checked: int = 0
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _fetch_corpus(cache_dir: Path) -> str:
    """Every fetched page, concatenated and de-obfuscated once. Emails are
    checked against this so an obfuscated source (`a [at] b [dot] com`) still
    matches the address the extractor recovered."""
    parts: list[str] = []
    fetch_dir = cache_dir / "fetch"
    if fetch_dir.is_dir():
        for path in fetch_dir.glob("*.json"):
            try:
                parts.append(path.read_text(errors="ignore"))
            except OSError:
                continue
    return _deobfuscate("\n".join(parts)).lower()


def audit_records(
    records: Iterable[EnrichmentRecord],
    cache_dir: Path | str,
    is_creditor: Callable[[str], bool],
) -> AuditReport:
    report = AuditReport()
    corpus = _fetch_corpus(Path(cache_dir))
    for record in records:
        for contact in record.contacts:
            email = contact.email
            if not email:
                continue
            report.emails_checked += 1
            domain = email.split("@")[-1].lower()
            if email.lower() not in corpus:
                report.violations.append(
                    f"row {record.source.row_id}: {email} is not present verbatim in "
                    f"any fetched page (possible hallucinated/inferred contact)"
                )
            if is_creditor(domain):
                report.violations.append(
                    f"row {record.source.row_id}: {email} is on the creditor domain "
                    f"{domain} (the debtor must never resolve to our client)"
                )
            if not contact.mx_valid:
                report.violations.append(
                    f"row {record.source.row_id}: {email} reached the output without a "
                    f"valid MX record (the scoring gate should have dropped it)"
                )
    return report
