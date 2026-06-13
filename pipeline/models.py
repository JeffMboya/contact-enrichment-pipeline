"""Data model shared across all pipeline stages.

One `EnrichmentRecord` flows through the whole pipeline; each stage fills in
its own section and never mutates upstream sections. A row that cannot be
enriched is still a fully-formed record (status NOT_FOUND with a reason), not
a missing entry or an exception — explicit failure is a first-class result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# The finder and scorer must share this exact label.
ROLE_ACCOUNTS_PAYABLE = "accounts payable"

# run.py skips this note when choosing the reviewer line.
BUDGET_STOP_PREFIX = "Stopped:"


class FullNameType(str, Enum):
    """Classification of the invoice's `Full name` field."""

    PERSON = "person"
    ORG_NAME = "org_name"
    DEPT = "dept"
    AMBIGUOUS = "ambiguous"


class Tier(str, Enum):
    """Internal difficulty tag for logging/triage. Carried in results.jsonl but
    never written to the Excel deliverable."""

    MEDIUM = "medium"
    HARD = "hard"


class ContactType(str, Enum):
    NAMED_PERSON = "named_person"
    ROLE_SPECIFIC = "role_specific"
    GENERIC = "generic"
    FORM_ONLY = "form_only"


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class RowStatus(str, Enum):
    ENRICHED = "enriched"
    # Partial means the domain resolved without a usable email.
    PARTIAL = "partial"
    NOT_FOUND = "not_found"
    SKIPPED = "skipped"


class NotFoundReason(str, Enum):
    INTERNAL_FACILITY = "internal_facility"
    AMBIGUOUS_MATCH = "ambiguous_match"
    NO_WEB_PRESENCE = "no_web_presence"
    NO_EMAIL_PUBLISHED = "no_email_published"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"


@dataclass
class SourceRow:
    """One raw input row, with debtor fields pulled out by header name."""

    row_id: int                # This is the 1-based index of the source row.
    raw: dict
    full_name: str
    address: str
    company_name: str
    phone: str


@dataclass
class Parsed:
    """Stage 1 output: cleaned company name and classification."""

    clean_name: str
    legal_suffix: Optional[str]
    duns: Optional[str]
    full_name_type: FullNameType
    tier: Tier
    # These are mechanical variants, never LLM-guessed names.
    query_variants: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    """A domain considered during resolution, kept for the audit trail."""

    domain: str
    rejected_because: Optional[str] = None


@dataclass
class Resolution:
    """Stage 2 output: the debtor's real company and corporate domain."""

    status: ResolutionStatus
    domain: Optional[str]
    legal_name: Optional[str]
    confidence: float          # This 0-to-1 value scales the contact score.
    evidence: list[str] = field(default_factory=list)
    candidates_considered: list[Candidate] = field(default_factory=list)


@dataclass
class Contact:
    """A single discovered contact. `email` is None only for FORM_ONLY."""

    email: Optional[str]
    name: Optional[str]
    role: Optional[str]
    type: ContactType
    source_url: str
    evidence_snippet: str       # This holds the literal text containing the email.
    mx_valid: bool = False
    domain_match: Optional[str] = None
    confidence: float = 0.0

    def assert_evidence_contains_email(self) -> None:
        """Anti-hallucination guard: the snippet must contain the email."""
        if self.email is not None and self.email not in self.evidence_snippet:
            raise ValueError(
                f"evidence snippet does not contain email {self.email!r}; "
                "possible hallucinated/inferred contact"
            )


@dataclass
class EnrichmentRecord:
    """The contract passed between stages. One per source row."""

    source: SourceRow
    parsed: Optional[Parsed] = None
    resolution: Optional[Resolution] = None
    contacts: list[Contact] = field(default_factory=list)
    status: RowStatus = RowStatus.NOT_FOUND
    not_found_reason: Optional[NotFoundReason] = None
    not_found_explanation: Optional[str] = None
    queries_used: int = 0
