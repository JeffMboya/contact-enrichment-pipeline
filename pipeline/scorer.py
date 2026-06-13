"""Stage 4: deterministic confidence score. Hard MX gate, then signals.

Score structure is fixed; the weights are tunable. Every contact starts from a
base for being found verbatim on a fetched page, accrues signal deltas, is
clamped, then multiplied by the resolution confidence so a shaky company match
can never yield a confident contact.
"""

from __future__ import annotations

from .cleaning import is_abbreviated
from .models import (
    ROLE_ACCOUNTS_PAYABLE,
    Contact,
    ContactType,
    FullNameType,
    Parsed,
    Resolution,
    ResolutionStatus,
    SourceRow,
)
from .web import url_domain

_BASE = 0.40
_DOMAIN_DELTA = {"exact": 0.30, "related": 0.15, "unknown": 0.0, "freemail": -0.15, "mismatch": -0.10}
_TYPE_DELTA = {
    ContactType.ROLE_SPECIFIC: 0.10,
    ContactType.NAMED_PERSON: 0.0,
    ContactType.GENERIC: -0.10,
    ContactType.FORM_ONLY: -0.10,
}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _person_name_match(contact: Contact, parsed: Parsed, row: SourceRow) -> bool:
    if parsed.full_name_type != FullNameType.PERSON or not row.full_name:
        return False
    local = contact.email.split("@")[0].lower() if contact.email else ""
    tokens = [t for t in row.full_name.lower().replace(".", " ").split() if len(t) >= 3]
    return any(token in local for token in tokens)


def score_contact(
    contact: Contact, resolution: Resolution, parsed: Parsed, row: SourceRow
) -> float:
    score = _BASE
    score += _DOMAIN_DELTA.get(contact.domain_match or "unknown", 0.0)
    score += _TYPE_DELTA[contact.type]
    if contact.role == ROLE_ACCOUNTS_PAYABLE:
        score += 0.10
    if _person_name_match(contact, parsed, row):
        score += 0.10
    if contact.mx_valid:
        score += 0.05
    source_domain = url_domain(contact.source_url)
    if resolution.domain and source_domain != resolution.domain:
        score -= 0.05  # Penalize an off-domain or third-party source.
    if resolution.status == ResolutionStatus.AMBIGUOUS:
        score -= 0.10
    if is_abbreviated(parsed.clean_name):  # A truncated company name is a weaker match.
        score -= 0.10
    score = _clamp(score) * resolution.confidence
    return round(_clamp(score), 2)


def score_contacts(
    contacts: list[Contact], resolution: Resolution, parsed: Parsed, row: SourceRow
) -> list[Contact]:
    survivors: list[Contact] = []
    for contact in contacts:
        if contact.email is not None and not contact.mx_valid:
            continue  # A domain without MX records cannot receive mail.
        contact.confidence = score_contact(contact, resolution, parsed, row)
        survivors.append(contact)
    survivors.sort(key=lambda c: c.confidence, reverse=True)
    return survivors
