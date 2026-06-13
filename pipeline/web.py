"""Cached network primitives: Serper search and page fetch, with a per-row budget."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .cache import Cache
from .config import FETCH_HEADERS, FETCH_TIMEOUT, SERPER_ENDPOINT


def url_domain(url: str) -> str:
    """Registrable host of a URL: lowercased, no userinfo/port, no leading www."""
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")

SKIP_FETCH_DOMAINS = (
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
)

# A directory's page yields its own email, not the debtor's.
DIRECTORY_DOMAINS = (
    "yelp.com", "mapquest.com", "yellowpages.com", "bbb.org", "manta.com",
    "zoominfo.com", "dnb.com", "bizapedia.com", "buzzfile.com", "opencorporates.com",
    "indeed.com", "glassdoor.com", "crunchbase.com", "facebook.com", "linkedin.com",
    "instagram.com", "tripadvisor.com", "angi.com", "thumbtack.com", "houzz.com",
)


def is_directory(domain: str) -> bool:
    d = (domain or "").lower()
    return any(d == x or d.endswith("." + x) for x in DIRECTORY_DOMAINS)


class SearchBudget:
    """Bounds Serper query attempts per row (cached hits count too)."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def spend(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def serper_search(cache: Cache, api_key: str, query: str, num: int = 5) -> dict:
    def producer() -> dict:
        response = httpx.post(
            SERPER_ENDPOINT,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=FETCH_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    return cache.get_or_compute("serper", query, producer)


def should_skip_fetch(url: str) -> bool:
    host = url_domain(url)
    return any(host == d or host.endswith("." + d) for d in SKIP_FETCH_DOMAINS)


_MAX_FETCH_BYTES = 2_000_000
_MAX_REDIRECTS = 5


def is_public_url(url: str) -> bool:
    """Reject anything that could point the fetcher at internal infrastructure.

    Only http(s) to a publicly-routable host is allowed. Guards against SSRF via
    hostile search results or redirects to loopback / link-local (cloud metadata)
    / private ranges.
    """
    import ipaddress
    import socket

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, parsed.port or 0, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # NAT64 is reserved but routable, so it stays allowed.
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            return False
    return True


def fetch_page(cache: Cache, url: str) -> dict:
    cached = cache.get("fetch", url)
    if cached is not None:
        return cached
    try:
        result = _fetch_validated(url)
    except httpx.HTTPError as error:
        # Transient errors are not cached so a rerun retries.
        return {"status": 0, "html": "", "error": str(error)}
    cache.set("fetch", url, result)
    return result


def _fetch_validated(url: str) -> dict:
    from urllib.parse import urljoin

    current = url
    with httpx.Client(
        timeout=FETCH_TIMEOUT,
        follow_redirects=False,
        headers=FETCH_HEADERS,
    ) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            if not is_public_url(current):
                return {"status": 0, "html": "", "error": "blocked non-public URL"}
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return {"status": response.status_code, "html": ""}
                    current = urljoin(current, location)
                    continue
                content_type = response.headers.get("content-type", "text/html").lower()
                if response.status_code != 200 or not (
                    "html" in content_type or "text" in content_type
                ):
                    return {"status": response.status_code, "html": ""}
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= _MAX_FETCH_BYTES:
                        break
                body = b"".join(chunks)[:_MAX_FETCH_BYTES]
                encoding = response.encoding or "utf-8"
                return {"status": 200, "html": body.decode(encoding, errors="replace")}
    return {"status": 0, "html": "", "error": "too many redirects"}
