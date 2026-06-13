"""Stage 1: clean the company name and classify the contact field."""

from __future__ import annotations

import re

from .config import HAIKU
from .llm import LLM
from .models import FullNameType, Parsed, SourceRow, Tier

_DUNS_RE = re.compile(r"\(\s*DUNS\s*N[°ºo]?\s*(\d+)\s*\)", re.IGNORECASE)

_LEGAL_SUFFIXES = {
    "LLC", "LTD", "INC", "CORP", "CO", "COMPANY", "LLP",
    "CORPORATION", "INCORPORATED", "LIMITED",
}

_ABBREVIATIONS = {
    "CONST": "CONSTRUCTION",
    "CONSTR": "CONSTRUCTION",
    "MFG": "MANUFACTURING",
    "MGMT": "MANAGEMENT",
    "SVC": "SERVICE",
    "SVCS": "SERVICES",
    "CTR": "CENTER",
    "ASSOC": "ASSOCIATES",
    "INTL": "INTERNATIONAL",
    "TECH": "TECHNOLOGY",
    "SYS": "SYSTEMS",
}

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["person", "org_name", "dept", "ambiguous"]}
    },
    "required": ["label"],
    "additionalProperties": False,
}

_CLASSIFY_SYSTEM = (
    "You classify the contact-name field from an invoice row. "
    "Reply with one label: 'person' for an individual's name, 'org_name' when it "
    "repeats the company or a facility, 'dept' for a function like 'Invoicing' or "
    "'Accounts Payable', and 'ambiguous' when unclear."
)


def strip_duns(company_name: str) -> tuple[str, str | None]:
    match = _DUNS_RE.search(company_name)
    duns = match.group(1) if match else None
    return _DUNS_RE.sub("", company_name).strip(), duns


def strip_legal_suffix(name: str) -> tuple[str, str | None]:
    tokens = name.split()
    suffix = None
    while tokens:
        bare = tokens[-1].strip(".,").upper()
        if bare in _LEGAL_SUFFIXES:
            suffix = tokens[-1].strip(".,")
            tokens.pop()
        else:
            break
    clean = " ".join(tokens).strip(" ,.&-")
    return clean, suffix


def query_variants(clean_name: str, name_without_duns: str) -> list[str]:
    variants = [clean_name]
    if name_without_duns and name_without_duns != clean_name:
        variants.append(name_without_duns)
    expanded = " ".join(
        _ABBREVIATIONS.get(token.strip(".,").upper(), token)
        for token in clean_name.split()
    )
    if expanded != clean_name:
        variants.append(expanded)
    seen: set[str] = set()
    return [v for v in variants if v and not (v in seen or seen.add(v))]


def is_abbreviated(clean_name: str) -> bool:
    return any(
        token.strip(".,").upper() in _ABBREVIATIONS for token in clean_name.split()
    )


def _tier(clean_name: str) -> Tier:
    if len(clean_name.split()) <= 2 or is_abbreviated(clean_name):
        return Tier.HARD
    return Tier.MEDIUM


def classify_full_name(value: str, llm: LLM | None) -> FullNameType:
    if llm is None or not value:
        return FullNameType.AMBIGUOUS
    result = llm.json(HAIKU, _CLASSIFY_SYSTEM, f"String: {value}", _CLASSIFY_SCHEMA)
    return FullNameType(result["label"])


def clean(row: SourceRow, llm: LLM | None = None) -> Parsed:
    name_without_duns, duns = strip_duns(row.company_name)
    clean_name, legal_suffix = strip_legal_suffix(name_without_duns)
    return Parsed(
        clean_name=clean_name,
        legal_suffix=legal_suffix,
        duns=duns,
        full_name_type=classify_full_name(row.full_name, llm),
        tier=_tier(clean_name),
        query_variants=query_variants(clean_name, name_without_duns),
    )
