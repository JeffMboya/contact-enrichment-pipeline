"""Stage 2: resolve the debtor's real company and corporate domain.

Resolution is an escalation loop, not a single shot: each round spends one
Serper query, accumulates results, and asks an LLM judge to pick the company's
own site. A judge that cannot pick may instead propose ONE refined follow-up
query grounded in what the results revealed (a fuller legal name, an acquirer,
a current brand) — that query goes to the front of the queue and the loop
continues until the judge accepts, the queries run out, or the row budget is
exhausted. The judge can never output a domain that is not in a result URL it
was shown; a follow-up query only triggers another search, never an output.
"""

from __future__ import annotations

import re
from collections import deque

from .cache import Cache
from .config import HAIKU, SONNET, Config
from .llm import LLM
from .models import (
    BUDGET_STOP_PREFIX,
    Candidate,
    Parsed,
    Resolution,
    ResolutionStatus,
    SourceRow,
    Tier,
)
from .web import SearchBudget, is_directory, serper_search, url_domain

_PO_BOX_RE = re.compile(r"(?i)\bp\.?\s*o\.?\s*box\s+\d+\b")
_CITY_STATE_RE = re.compile(r"^(.*?)\s+([A-Z]{2})\s+\d{5}")

_MAX_JUDGED = 8           # At most this many results go to the judge.
_MAX_FOLLOWUP_LEN = 100   # A longer follow-up query is rejected as rambling.
# A branch address may match a same-named company elsewhere.
_LOCATION_MISMATCH_FACTOR = 0.85

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "result_index": {"type": "integer"},
        "domain": {"type": "string"},
        "reasoning": {"type": "string"},
        "confidence": {"type": "number"},
        "location_match": {
            "type": "string",
            "enum": ["exact", "same_region", "different_location", "unknown"],
        },
        "current_name": {"type": "string"},
        "followup_query": {"type": "string"},
    },
    "required": [
        "result_index", "domain", "reasoning", "confidence",
        "location_match", "current_name", "followup_query",
    ],
    "additionalProperties": False,
}

_JUDGE_SYSTEM = (
    "You resolve which company a debtor invoice row belongs to. You are given the "
    "debtor's messy company name, its address and phone, and web search results. "
    "Choose the single result that is the company's OWN official website. Treat the "
    "address as a disambiguation signal, not a hard requirement: invoice addresses "
    "are often a plant, branch office, PO box or registered agent, so when the name "
    "is distinctive and the results consistently identify one company, choose its "
    "official site even if no result shows that exact address. When the name is "
    "generic (many unrelated companies could share it), require location agreement. "
    "Prefer the company's own site over directories, registries and news articles; "
    "those may still corroborate a choice. "
    "result_index is the 1-based index of the chosen result, or -1 if none. domain "
    "is the bare hostname of the chosen result's URL (e.g. 'acme.com'), or '' if "
    "none. Never output a domain that is not in one of the result URLs. confidence "
    "is 0..1. location_match says how the chosen result relates to the given "
    "address: exact, same_region, different_location, or unknown. current_name is "
    "the company's current official name copied verbatim from the result text when "
    "it differs from the debtor name (acquisition, rebrand, fuller legal name), "
    "else ''. followup_query: only when you choose NO result but the results reveal "
    "a better search string (a fuller legal name, the current owner or brand, plus "
    "a location), give ONE refined web query, else ''."
)


def parse_city_state(address: str) -> tuple[str | None, str | None]:
    cleaned = _PO_BOX_RE.sub(" ", address).strip()
    chunks = re.split(r"\s{2,}", cleaned)
    for chunk in reversed(chunks):
        match = _CITY_STATE_RE.search(chunk.strip())
        if match:
            return match.group(1).strip(" ,"), match.group(2)
    match = re.search(r"\b([A-Z]{2})\s+\d{5}", cleaned)
    return (None, match.group(1)) if match else (None, None)


def build_queries(parsed: Parsed, city: str | None, state: str | None) -> list[str]:
    names = parsed.query_variants or [parsed.clean_name]
    loc = " ".join(part for part in (city, state) if part)
    queries: list[str] = []
    for name in names:
        if loc:
            queries.append(f'"{name}" {loc}')
        if state:
            queries.append(f'"{name}" {state} official website')
        queries.append(f'"{name}" contact')
    seen: set[str] = set()
    return [q for q in queries if not (q in seen or seen.add(q))]


_SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
    "additionalProperties": False,
}

_SUGGEST_SYSTEM = (
    "You are given a messy, often abbreviated or truncated company name from an "
    "invoice and its address. Propose up to 3 web SEARCH QUERIES likely to surface "
    "the company's own official website. Expand plausible abbreviations (CONST could "
    "be 'construction' or 'control'; MFG 'manufacturing'), try a likely fuller name, "
    "and include the city or state. These strings are used only as search inputs and "
    "are never shown to anyone or used as an answer, so favour recall. Do not assert "
    "facts you don't know — offer plausible search phrasings, not a definitive name."
)


def llm_query_suggestions(parsed: Parsed, row: SourceRow, llm: LLM, model: str = HAIKU) -> list[str]:
    """LLM-proposed alternative SEARCH strings for a hard/cryptic name. Safe by
    construction: the output of resolution is always a domain taken from a real
    search result, so a suggested query can only widen what Serper returns — it can
    never itself become the answer. Returns [] on any malformed/failed response."""
    user = f"Company name: {parsed.clean_name}\nAddress: {row.address}\nPhone: {row.phone}"
    try:
        result = llm.json(model, _SUGGEST_SYSTEM, user, _SUGGEST_SCHEMA)
    except Exception:
        return []
    out: list[str] = []
    for q in result.get("queries", []):
        q = (q or "").strip()
        if q and len(q) <= _MAX_FOLLOWUP_LEN and q not in out:
            out.append(q)
    return out[:3]


_STOP_TOKENS = {
    "the", "of", "and", "a", "an", "for", "llc", "inc", "corp", "co",
    "company", "ltd", "llp", "group",
}


def name_domain_affinity(clean_name: str, domain: str) -> bool:
    """True if the resolved domain plausibly belongs to the company.

    A correct match almost always shares a name token with its domain (DATA
    FINANCIAL -> datafinancial.com, Sika Corporation -> usa.sika.com) or
    matches its acronym (IBM -> ibm.com). A domain that shares nothing
    (VETERINARY SPECIALTIES -> twcinc.com) is the judge picking an unrelated
    site; reject it rather than emit a wrong contact.
    """
    labels = [
        re.sub(r"[^a-z0-9]", "", label.lower())
        for label in (domain or "").split(".")[:-1]  # This keeps every label except the TLD.
    ]
    labels = [label for label in labels if label]
    if not labels:
        return False
    tokens = [re.sub(r"[^a-z0-9]", "", t.lower()) for t in clean_name.split()]
    tokens = [t for t in tokens if t]
    sig = [t for t in tokens if len(t) >= 3 and t not in _STOP_TOKENS] or tokens
    joined = "".join(tokens)
    acronym = "".join(t[0] for t in sig)
    for label in labels:
        if any(t in label for t in sig if len(t) >= 4):
            return True
        if len(label) >= 4 and label in joined:
            return True
        if len(acronym) >= 2 and acronym == label:
            return True
    return False


def is_creditor_domain(domain: str, cfg: Config) -> bool:
    """True if a candidate domain belongs to the creditor (our client) and must
    never be enriched as the debtor. Two layers: the configured domain list
    (exact, e.g. fedex.com) and name-affinity to a creditor name detected from
    the invoice column — so the guard generalizes past the env default to any
    client whose own domain happens to surface in a debtor search."""
    if cfg.is_creditor(domain):
        return True
    return any(name_domain_affinity(name, domain) for name in cfg.creditor_names)


def _judge(
    llm: LLM, model: str, parsed: Parsed, row: SourceRow, results: list[dict]
) -> dict:
    lines = [
        f"[{i + 1}] Title: {r['title']} | URL: {r['url']} | Snippet: {r['snippet']}"
        for i, r in enumerate(results)
    ]
    user = (
        f"Debtor company: {parsed.clean_name}\n"
        f"Address: {row.address}\n"
        f"Phone: {row.phone}\n\n"
        "Search results:\n" + "\n".join(lines)
    )
    return llm.json(model, _JUDGE_SYSTEM, user, _JUDGE_SCHEMA)


def _name_in_results(name: str, results: list[dict]) -> bool:
    """Grounding check: the judge's current_name must appear verbatim in the
    search-result text it was shown, so an affinity pass can never ride on a
    name the model produced from its own knowledge."""
    haystack = " ".join(f"{r['title']} {r['snippet']}" for r in results).lower()
    return bool(name) and name.lower() in haystack


def _accept(
    verdict: dict, top: list[dict], parsed: Parsed
) -> tuple[Resolution | None, str]:
    """Validate one judge verdict. Returns (resolution, "") on acceptance or
    (None, rejection_reason) — the reason feeds the evidence trail."""
    index = verdict.get("result_index", -1)
    domain = (verdict.get("domain") or "").strip().lower()
    confidence = float(verdict.get("confidence", 0.0))
    reasoning = verdict.get("reasoning", "")

    chosen = top[index - 1] if 1 <= index <= len(top) else None
    if chosen is None:
        return None, reasoning or "Judge matched none of the search results."
    # An exact host match blocks substring near-misses.
    if domain.removeprefix("www.") != url_domain(chosen["url"]):
        return None, (
            f"Judge proposed domain {domain!r}, which is not the host of the "
            f"result it selected; rejected as unverifiable."
        )

    chosen_domain = url_domain(chosen["url"])
    current_name = (verdict.get("current_name") or "").strip()
    grounded_current = current_name if _name_in_results(current_name, top) else ""
    if not (
        name_domain_affinity(parsed.clean_name, chosen_domain)
        or (grounded_current and name_domain_affinity(grounded_current, chosen_domain))
    ):
        return None, (
            f"Resolved domain {chosen_domain} is unrelated to the company "
            f"name; likely a directory or a wrong match."
        )

    evidence = [reasoning, f"Matched {chosen['url']}"]
    if grounded_current:
        evidence.append(
            f"Company appears in sources under its current name {grounded_current!r}."
        )
    if verdict.get("location_match") == "different_location":
        confidence *= _LOCATION_MISMATCH_FACTOR
        evidence.append(
            "Invoice address looks like a branch/facility of this company, not "
            "the location the chosen site shows; confidence discounted."
        )
    others = [
        Candidate(url_domain(r["url"]), "not selected")
        for i, r in enumerate(top)
        if i != index - 1
    ]
    return (
        Resolution(
            status=ResolutionStatus.RESOLVED,
            domain=chosen_domain,
            legal_name=grounded_current or parsed.clean_name,
            confidence=round(confidence, 2),
            evidence=evidence,
            candidates_considered=others,
        ),
        "",
    )


def _norm_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.lower()).strip(' "')


def resolve(
    parsed: Parsed,
    row: SourceRow,
    cache: Cache,
    cfg: Config,
    llm: LLM,
    budget: SearchBudget,
) -> Resolution:
    city, state = parse_city_state(row.address)
    stock = build_queries(parsed, city, state)
    ordered = list(stock)
    if parsed.tier == Tier.HARD:
        # Smart queries follow the first stock query to save budget.
        suggestions = llm_query_suggestions(parsed, row, llm)
        if suggestions:
            ordered = stock[:1] + suggestions + stock[1:]
    tried: set[str] = set()
    ordered = [q for q in ordered if not (_norm_query(q) in tried or tried.add(_norm_query(q)))]
    pending = deque(ordered)
    results: list[dict] = []
    seen: set[str] = set()
    rejections: list[str] = []
    last_confidence = 0.0
    judged = False

    while pending:
        if not budget.spend():
            rejections.append(
                f"{BUDGET_STOP_PREFIX} per-row query budget exhausted with "
                f"{len(pending)} quer{'y' if len(pending) == 1 else 'ies'} untried."
            )
            break
        query = pending.popleft()
        data = serper_search(cache, cfg.serper_api_key, query)
        new = 0
        for item in data.get("organic", []):
            url = item.get("link")
            dom = url_domain(url) if url else ""
            if url and url not in seen and not is_creditor_domain(dom, cfg) and not is_directory(dom):
                seen.add(url)
                new += 1
                results.append(
                    {
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", ""),
                        "url": url,
                    }
                )
        if not results or (judged and new == 0):
            continue  # Skip judging when this query added no new results.

        top = results[:_MAX_JUDGED]
        if cfg.resolution_model:
            # An explicit override skips the cheap-first model ladder.
            verdict = _judge(llm, cfg.resolution_model, parsed, row, top)
        else:
            verdict = _judge(llm, HAIKU, parsed, row, top)
            if verdict.get("confidence", 0.0) < 0.4:
                verdict = _judge(llm, SONNET, parsed, row, top)
        judged = True
        last_confidence = float(verdict.get("confidence", 0.0))

        resolution, rejection = _accept(verdict, top, parsed)
        if resolution is not None:
            return resolution
        rejections.append(rejection)

        followup = (verdict.get("followup_query") or "").strip()
        if (
            followup
            and len(followup) <= _MAX_FOLLOWUP_LEN
            and _norm_query(followup) not in tried
        ):
            tried.add(_norm_query(followup))
            pending.appendleft(followup)  # The judge's follow-up outranks the stock queries.

    if not results:
        return Resolution(
            status=ResolutionStatus.UNRESOLVED,
            domain=None,
            legal_name=None,
            confidence=0.0,
            evidence=rejections or ["No web search results for any query variant."],
        )
    return Resolution(
        status=ResolutionStatus.UNRESOLVED if last_confidence < 0.2 else ResolutionStatus.AMBIGUOUS,
        domain=None,
        legal_name=None,
        confidence=min(last_confidence, 0.3),
        evidence=rejections or ["No result matched the debtor company."],
        candidates_considered=[
            Candidate(url_domain(r["url"]), "not selected") for r in results[:_MAX_JUDGED]
        ],
    )
