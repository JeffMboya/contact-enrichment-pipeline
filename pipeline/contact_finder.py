"""Stage 3: find emails literally present in fetched content. Never generated."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .cache import Cache
from .config import HAIKU, Config
from .llm import LLM
from .models import (
    ROLE_ACCOUNTS_PAYABLE,
    Contact,
    ContactType,
    FullNameType,
    Parsed,
    Resolution,
    SourceRow,
)
from .resolution import is_creditor_domain
from .web import (
    SearchBudget,
    fetch_page,
    is_directory,
    serper_search,
    should_skip_fetch,
    url_domain,
)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_ROLE_LOCAL = {
    "ap", "accountspayable", "accounts", "accountsreceivable", "ar",
    "billing", "invoices", "invoice", "remittance", "payments", "payment",
}
_GENERIC_LOCAL = {
    "info", "contact", "hello", "office", "admin", "sales", "support",
    "help", "mail", "enquiries", "inquiries", "general", "team",
}
_FREEMAIL = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com",
}
_JUNK_DOMAINS = {
    "example.com", "sentry.io", "wix.com", "wixpress.com", "squarespace.com",
    "schema.org", "godaddy.com", "fontawesome.com", "sentry-next.wixpress.com",
}
_FETCH_CAP = 8
_FETCH_PATHS = ("/contact", "/contact-us", "/about", "/about-us", "/billing", "")


def _deobfuscate(text: str) -> str:
    text = text.replace("&#64;", "@").replace("&#46;", ".")
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", text, flags=re.IGNORECASE)
    return text


_PERSON_LOCAL_RE = re.compile(r"^[a-z]+[._-][a-z]+$")


def _email_type(local: str, person_name: str | None = None) -> ContactType:
    key = local.lower()
    bare = re.sub(r"[^a-z0-9]", "", key)
    if key in _ROLE_LOCAL or bare in _ROLE_LOCAL:
        return ContactType.ROLE_SPECIFIC
    if key in _GENERIC_LOCAL or bare in _GENERIC_LOCAL:
        return ContactType.GENERIC
    # A bare local part is a mailbox, not a person.
    if _PERSON_LOCAL_RE.match(key):
        return ContactType.NAMED_PERSON
    tokens = [
        t for t in re.sub(r"[^a-z ]", " ", (person_name or "").lower()).split()
        if len(t) >= 3
    ]
    if any(token in bare for token in tokens):
        return ContactType.NAMED_PERSON
    return ContactType.GENERIC


def _domain_match(email_domain: str, resolved: str | None) -> str:
    if not resolved:
        return "unknown"
    if email_domain == resolved:
        return "exact"
    if email_domain in _FREEMAIL:
        return "freemail"
    if email_domain.endswith("." + resolved) or resolved.endswith("." + email_domain):
        return "related"
    return "mismatch"


def extract_emails(html: str) -> list[dict]:
    text = _deobfuscate(BeautifulSoup(html, "lxml").get_text(" "))
    raw = _deobfuscate(html)
    found: dict[str, dict] = {}
    for source in (text, raw):
        for match in _EMAIL_RE.finditer(source):
            email = match.group(0).rstrip(".").lower()
            domain = email.split("@")[-1]
            if domain in _JUNK_DOMAINS or domain.rsplit(".", 1)[-1] in {"png", "jpg", "gif", "svg", "webp"}:
                continue
            if email in found:
                continue
            start, end = max(0, match.start() - 80), match.end() + 80
            snippet = re.sub(r"\s+", " ", source[start:end]).strip()
            if email not in snippet:
                snippet = email
            near_ap = bool(
                re.search(r"accounts?\s+payable|billing|remittance", snippet, re.IGNORECASE)
            )
            found[email] = {"email": email, "snippet": snippet, "near_ap": near_ap}
    return list(found.values())


def _mx_valid(cache: Cache, email_domain: str) -> bool:
    cached = cache.get("mx", email_domain)
    if cached is not None:
        return bool(cached)
    import dns.resolver

    try:
        result = bool(dns.resolver.resolve(email_domain, "MX"))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        result = False  # These DNS errors definitively mean no mail records.
    except Exception:
        # A transient DNS failure must not be cached.
        return False
    cache.set("mx", email_domain, result)
    return result


def _candidate_urls(domain: str) -> list[str]:
    return [f"https://{domain}{path}" for path in _FETCH_PATHS]


def _contact_queries(parsed: Parsed, row: SourceRow, domain: str) -> list[str]:
    queries: list[str] = []
    if parsed.full_name_type == FullNameType.PERSON and row.full_name:
        queries.append(f'"{row.full_name}" "{parsed.clean_name}" email')
    queries.append(f'"{parsed.clean_name}" "accounts payable" email')
    queries.append(f"site:{domain} contact")
    return queries


def _rank_key(contact: Contact) -> tuple:
    type_rank = {
        ContactType.ROLE_SPECIFIC: 0,
        ContactType.NAMED_PERSON: 1,
        ContactType.GENERIC: 2,
        ContactType.FORM_ONLY: 3,
    }[contact.type]
    domain_rank = {"exact": 0, "related": 1, "unknown": 2, "freemail": 3, "mismatch": 4}.get(
        contact.domain_match or "unknown", 2
    )
    return (domain_rank, type_rank, 0 if contact.role else 1)


# The reader annotates extracted emails; it never introduces an address.
_READER_SCHEMA = {
    "type": "object",
    "properties": {
        "annotations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "department": {"type": "string"},
                },
                "required": ["email", "name", "role", "department"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["annotations"],
    "additionalProperties": False,
}

_READER_SYSTEM = (
    "You are given the text of one or more company web pages and the list of email "
    "addresses a regex already extracted from those pages. For each address, read the "
    "page text and report the person it belongs to (name), their role/title, and their "
    "department — but ONLY what the text actually states; use \"\" for anything the page "
    "does not say. Mark the address that reaches accounts payable / billing / a finance "
    "contact by setting its role accordingly. "
    "Hard rules: you may ONLY reference email addresses from the provided list — never "
    "invent, complete, or correct an address. Never infer a person's name that is not "
    "written in the page text. role should say 'accounts payable' or 'billing' only when "
    "the page ties that address to that function."
)

_AP_ROLE_HINTS = ("payable", "billing", "accounts", "finance", "remit", "invoic")

# A title or function is not a usable person name.
_NAME_NONWORDS = {
    "attorney", "dr", "mr", "mrs", "ms", "prof", "professor", "president",
    "owner", "manager", "director", "ceo", "cfo", "coo", "contact", "accounts",
    "payable", "billing", "department", "office", "team", "sales", "support",
    "info", "admin", "the", "esq", "llc", "inc", "company", "co", "group",
}


def _name_grounded(name: str, text: str) -> bool:
    """Accept an attached name only if (a) every significant token appears in the
    fetched text — so the reader can't introduce a person the page never named —
    and (b) it reads like a personal name, not a title/role phrase."""
    haystack = text.lower()
    tokens = [t for t in re.sub(r"[^a-z ]", " ", name.lower()).split() if len(t) >= 2]
    if len(tokens) < 2 or any(t in _NAME_NONWORDS for t in tokens):
        return False
    return all(t in haystack for t in tokens if len(t) >= 3)


def _annotate_contacts(
    contacts: dict[str, Contact], page_text: str, llm: LLM, model: str = HAIKU
) -> None:
    """Attach grounded name/role to regex-extracted email contacts, in place.
    Best-effort: any failure leaves the deterministic contacts untouched."""
    emails = [e for e, c in contacts.items() if c.email]
    if not emails or not page_text.strip():
        return
    user = (
        "Extracted email addresses (the only addresses you may reference):\n"
        + "\n".join(f"- {e}" for e in emails)
        + "\n\nPage text:\n"
        + page_text[:6000]
    )
    try:
        result = llm.json(model, _READER_SYSTEM, user, _READER_SCHEMA)
    except Exception:
        return  # The reader is best-effort and never fails a row.

    for ann in result.get("annotations", []):
        email = (ann.get("email") or "").strip().lower()
        contact = contacts.get(email)
        if contact is None or contact.email is None:
            continue  # Ignore any address the regex did not extract.
        name = (ann.get("name") or "").strip()
        if name and _name_grounded(name, page_text):
            contact.name = name
            if contact.type == ContactType.GENERIC:
                contact.type = ContactType.NAMED_PERSON
        role = (ann.get("role") or "").lower()
        if any(hint in role for hint in _AP_ROLE_HINTS):
            contact.role = ROLE_ACCOUNTS_PAYABLE


def find_contacts(
    resolution: Resolution,
    parsed: Parsed,
    row: SourceRow,
    cache: Cache,
    cfg: Config,
    budget: SearchBudget,
    llm: LLM | None = None,
) -> list[Contact]:
    if not resolution.domain:
        return []

    urls = list(_candidate_urls(resolution.domain))
    for query in _contact_queries(parsed, row, resolution.domain):
        if not budget.spend():
            break
        data = serper_search(cache, cfg.serper_api_key, query)
        for item in data.get("organic", []):
            link = item.get("link", "")
            if link and any(k in link.lower() for k in ("contact", "about", "billing")):
                urls.append(link)

    contacts: dict[str, Contact] = {}
    form_pages: list[str] = []
    email_page_text: list[str] = []
    fetched = 0
    seen_urls: set[str] = set()
    person_name = row.full_name if parsed.full_name_type == FullNameType.PERSON else None
    for url in urls:
        if fetched >= _FETCH_CAP:
            break
        dom = url_domain(url)
        if (
            url in seen_urls or should_skip_fetch(url)
            or is_creditor_domain(dom, cfg) or is_directory(dom)
        ):
            continue
        seen_urls.add(url)
        page = fetch_page(cache, url)
        if page.get("status") != 200 or not page.get("html"):
            continue
        fetched += 1
        page_emails = extract_emails(page["html"])
        if not page_emails and "<form" in page["html"].lower():
            form_pages.append(url)
        if page_emails:
            email_page_text.append(_deobfuscate(BeautifulSoup(page["html"], "lxml").get_text(" ")))
        for item in page_emails:
            email = item["email"]
            if email in contacts:
                continue
            email_domain = email.split("@")[-1]
            if is_creditor_domain(email_domain, cfg):
                continue  # A creditor mailbox is never a debtor contact.
            contact = Contact(
                email=email,
                name=None,
                role=ROLE_ACCOUNTS_PAYABLE if item["near_ap"] else None,
                type=_email_type(email.split("@")[0], person_name),
                source_url=url,
                evidence_snippet=item["snippet"],
                mx_valid=_mx_valid(cache, email_domain),
                domain_match=_domain_match(email_domain, resolution.domain),
            )
            contact.assert_evidence_contains_email()
            contacts[email] = contact

    if not contacts and form_pages:
        url = form_pages[0]  # Candidate order puts the contact page first.
        contacts[url] = Contact(
            email=None,
            name=None,
            role=None,
            type=ContactType.FORM_ONLY,
            source_url=url,
            evidence_snippet="Page offers a contact form; no email published.",
            mx_valid=False,
            domain_match=_domain_match(url_domain(url), resolution.domain),
        )

    if llm is not None:
        # Annotate before ranking because type and role feed it.
        _annotate_contacts(contacts, "\n\n".join(email_page_text), llm)

    ranked = sorted(contacts.values(), key=_rank_key)
    return ranked[:2]
