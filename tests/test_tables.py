"""Table-driven tests for the pure parsing/classification/scoring functions.

Run with:  python tests/test_tables.py

Each table is (input, expected) pairs; add a regression case by adding a row.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.cleaning import is_abbreviated, query_variants, strip_duns, strip_legal_suffix
from pipeline.contact_finder import _domain_match, _email_type
from pipeline.ingestion import is_internal_facility
from pipeline.web import should_skip_fetch, url_domain
from pipeline.models import (
    Contact,
    ContactType,
    FullNameType,
    Parsed,
    Resolution,
    ResolutionStatus,
    SourceRow,
    Tier,
)
from pipeline.resolution import name_domain_affinity, parse_city_state
from pipeline.scorer import score_contact

DUNS_CASES = [
    ("DUVAL MOTORS (DUNS N° 6921522)", ("DUVAL MOTORS", "6921522")),
    ("ACME (DUNS Nº 99)", ("ACME", "99")),
    ("ACME (DUNS No 12345)", ("ACME", "12345")),
    ("acme (duns n° 7)", ("acme", "7")),
    ("ACME ( DUNS N° 8 )", ("ACME", "8")),
    ("NO DUNS HERE", ("NO DUNS HERE", None)),
]

SUFFIX_CASES = [
    ("VIRGIN ATLANTIC AIRWAYS LTD", ("VIRGIN ATLANTIC AIRWAYS", "LTD")),
    ("AVIENT COLORANTS USA LLC", ("AVIENT COLORANTS USA", "LLC")),
    ("TAVAERO JET CHARTER CORP.", ("TAVAERO JET CHARTER", "CORP")),
    ("L M SCOFIELD COMPANY", ("L M SCOFIELD", "COMPANY")),
    ("D. F. LOGISTICS , LLC", ("D. F. LOGISTICS", "LLC")),
    ("ACME CO LLC", ("ACME", "CO")),  # stacked suffixes; innermost recorded
    ("ROBERT W CALCOTE MD", ("ROBERT W CALCOTE MD", None)),  # MD kept on purpose
    ("BOUMIL LAW OFFICES", ("BOUMIL LAW OFFICES", None)),
]

CITY_STATE_CASES = [
    ("100 Main St  Norfolk VA 23510 USA", ("Norfolk", "VA")),
    ("Po Box 268  Canton MA 02021 USA", ("Canton", "MA")),
    ("3702 Center St  Deer Park TX 77536 USA", ("Deer Park", "TX")),
    ("695 Park Ave  New York NY 10065 USA", ("New York", "NY")),
    ("Po Box 442  Knoxville TN 37901 USA", ("Knoxville", "TN")),
    # single-spaced C/O forwarding address: state survives, city is noisy
    (
        "Po Box 30382 C/O CT Logistics Team 15 Cleveland OH 44130 USA",
        ("C/O CT Logistics Team 15 Cleveland", "OH"),
    ),
    ("Main St  Springfield IL", (None, None)),  # no zip, no match
    ("no location at all", (None, None)),
]

EMAIL_TYPE_CASES = [
    ("ap", ContactType.ROLE_SPECIFIC),
    ("billing", ContactType.ROLE_SPECIFIC),
    ("accountspayable", ContactType.ROLE_SPECIFIC),
    (("accounts.payable", None), ContactType.ROLE_SPECIFIC),  # punctuated role
    ("remittance", ContactType.ROLE_SPECIFIC),
    ("info", ContactType.GENERIC),
    ("sales", ContactType.GENERIC),
    ("office", ContactType.GENERIC),
    ("jane.doe", ContactType.NAMED_PERSON),
    # NAMED_PERSON needs positive evidence; a bare word could be a person or a
    # functional mailbox (answers@, charter@), so it stays GENERIC unless the
    # invoice contact's name corroborates it.
    ("jsmith", ContactType.GENERIC),
    ("answers", ContactType.GENERIC),
    ("charter", ContactType.GENERIC),
    (("jsmith", "John Smith"), ContactType.NAMED_PERSON),
    (("sjboumil", "James Boumil"), ContactType.NAMED_PERSON),
    (("answers", "James Boumil"), ContactType.GENERIC),
]

DOMAIN_MATCH_CASES = [
    (("acme.com", "acme.com"), "exact"),
    (("mail.acme.com", "acme.com"), "related"),
    (("acme.com", "mail.acme.com"), "related"),
    (("gmail.com", "acme.com"), "freemail"),
    (("other.com", "acme.com"), "mismatch"),
    (("acme.com", None), "unknown"),
]

FACILITY_CASES = [
    (("FEDEX DROP BOX (DUNS N° 119133494)", "Fedex Drop Box"), True),
    (("USPS SCF PITTSBURGH 150", "Usps Scf Pittsburgh 150"), True),
    (("ACME BMEU SERVICES", "Jane Doe"), True),
    (("DUVAL MOTORS", "Joe Rich"), False),
    (("CONCORD HOSPITAL", "Concord Hospital"), False),
]

VARIANT_CASES = [
    (("ACME", "ACME INC"), ["ACME", "ACME INC"]),
    (("ACME", "ACME"), ["ACME"]),
    (
        ("RIVERSIDE INFECTION CONST", "RIVERSIDE INFECTION CONST"),
        ["RIVERSIDE INFECTION CONST", "RIVERSIDE INFECTION CONSTRUCTION"],
    ),
    (("DOBBS RAM", "DOBBS RAM & COMPANY"), ["DOBBS RAM", "DOBBS RAM & COMPANY"]),
]

ABBREVIATED_CASES = [
    ("RIVERSIDE INFECTION CONST", True),
    ("ACME MFG", True),
    ("VIRGIN ATLANTIC AIRWAYS", False),
    ("DATA FINANCIAL", False),
]

URL_DOMAIN_CASES = [
    ("https://www.acme.com/contact", "acme.com"),
    ("https://acme.com:443/billing", "acme.com"),      # port stripped
    ("https://user@acme.com/", "acme.com"),            # userinfo stripped
    ("http://ACME.COM/About", "acme.com"),             # lowercased
    ("https://mail.acme.com", "mail.acme.com"),
]

SKIP_FETCH_CASES = [
    ("https://www.linkedin.com/company/acme", True),
    ("https://x.com/acme", True),
    ("https://xerox.com/contact", False),              # substring bug regression
    ("https://acmefax.com/about", False),
    ("https://acme.com/contact", False),
]

# (clean_name, domain) -> domain plausibly belongs to the company?
AFFINITY_CASES = [
    (("DATA FINANCIAL", "datafinancial.com"), True),
    (("BOUMIL LAW OFFICES", "boumil-law.com"), True),
    (("TAVAERO JET CHARTER", "tavaero.com"), True),
    (("THE SANDING GLOVE", "thesandingglove.com"), True),
    (("COACH REALTORS", "coachrealtors.com"), True),
    (("MINUTE MAN POWERBOSS", "powerboss.com"), True),
    (("CONCORD HOSPITAL", "concordhospital.org"), True),
    (("INTERNATIONAL BUSINESS MACHINES", "ibm.com"), True),   # acronym
    (("Sika Corporation", "usa.sika.com"), True),             # country subdomain
    (("VETERINARY SPECIALTIES", "twcinc.com"), False),        # unrelated
    (("ZUMPANO ENTERPRISES", "giftly.com"), False),
    (("MEDLAB 24", "local.yahoo.com"), False),
]


def _contact(email, dm, typ, role=None, mx=True, src="https://acme.com/contact"):
    return Contact(email, None, role, typ, src, email or "form", mx, dm)


def _scoring_fixtures():
    row = SourceRow(1, {}, "Jane Doe", "1 St  City TX 75001 USA", "ACME LLC", "+1 555")
    parsed = Parsed("ACME", "LLC", None, FullNameType.PERSON, Tier.MEDIUM, ["ACME"])
    resolved = Resolution(ResolutionStatus.RESOLVED, "acme.com", "Acme", 0.85)
    ambiguous = Resolution(ResolutionStatus.AMBIGUOUS, "acme.com", "Acme", 0.30)
    return row, parsed, resolved, ambiguous


# (label, contact, resolution_key, lo, hi) — bands, not brittle exact decimals
SCORING_CASES = [
    ("ap role on-domain", _contact("ap@acme.com", "exact", ContactType.ROLE_SPECIFIC,
                                   role="accounts payable"), "resolved", 0.75, 0.90),
    ("named person match", _contact("jane.doe@acme.com", "exact", ContactType.NAMED_PERSON),
     "resolved", 0.65, 0.80),
    ("generic info@", _contact("info@acme.com", "exact", ContactType.GENERIC),
     "resolved", 0.50, 0.65),
    ("freemail via directory", _contact("acme@gmail.com", "freemail", ContactType.ROLE_SPECIFIC,
                                        src="https://yelp.com/biz/acme"), "resolved", 0.20, 0.45),
    ("ap but ambiguous resolution", _contact("ap@acme.com", "exact", ContactType.ROLE_SPECIFIC,
                                             role="accounts payable"), "ambiguous", 0.05, 0.30),
    ("form only on-domain", _contact(None, "exact", ContactType.FORM_ONLY, mx=False),
     "resolved", 0.30, 0.60),
]


def run_table(name, cases, fn):
    for index, (args, expected) in enumerate(cases):
        got = fn(*args) if isinstance(args, tuple) else fn(args)
        assert got == expected, f"{name}[{index}] {args!r}: expected {expected!r}, got {got!r}"
    print(f"{name}: {len(cases)} cases OK")


def run_scoring_table():
    row, parsed, resolved, ambiguous = _scoring_fixtures()
    resolutions = {"resolved": resolved, "ambiguous": ambiguous}
    for label, contact, key, lo, hi in SCORING_CASES:
        score = score_contact(contact, resolutions[key], parsed, row)
        assert lo <= score <= hi, f"scoring[{label}]: {score} not in [{lo}, {hi}]"
    print(f"scoring bands: {len(SCORING_CASES)} cases OK")


def main() -> None:
    run_table("strip_duns", DUNS_CASES, strip_duns)
    run_table("strip_legal_suffix", SUFFIX_CASES, strip_legal_suffix)
    run_table("parse_city_state", CITY_STATE_CASES, parse_city_state)
    run_table("email_type", EMAIL_TYPE_CASES, _email_type)
    run_table("domain_match", DOMAIN_MATCH_CASES, _domain_match)
    run_table("internal_facility", FACILITY_CASES, is_internal_facility)
    run_table("query_variants", VARIANT_CASES, query_variants)
    run_table("is_abbreviated", ABBREVIATED_CASES, is_abbreviated)
    run_table("url_domain", URL_DOMAIN_CASES, url_domain)
    run_table("should_skip_fetch", SKIP_FETCH_CASES, should_skip_fetch)
    run_table("name_domain_affinity", AFFINITY_CASES, name_domain_affinity)
    run_scoring_table()
    print("\nAll table tests passed.")


if __name__ == "__main__":
    main()
