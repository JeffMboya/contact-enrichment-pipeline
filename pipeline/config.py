"""Configuration and constants loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-8"

SERPER_ENDPOINT = "https://google.serper.dev/search"
FETCH_TIMEOUT = 8.0
# Many company sites reject a non-browser User-Agent.
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class Config:
    serper_api_key: str | None
    anthropic_api_key: str | None
    input_file: Path
    output_file: Path
    cache_dir: Path
    max_serper_calls_per_row: int
    creditor_domains: tuple[str, ...] = ()
    # A candidate domain matching a creditor name is excluded.
    creditor_names: tuple[str, ...] = ()
    # When set, the resolution judge uses only this model.
    resolution_model: str | None = None

    def is_creditor(self, domain: str | None) -> bool:
        """The creditor (our client) must never be enriched as the debtor."""
        if not domain:
            return False
        d = domain.lower()
        return any(d == c or d.endswith("." + c) for c in self.creditor_domains)

    def require_live_keys(self) -> None:
        """Raise before any network stage runs if credentials are absent."""
        missing = [
            name
            for name, value in (
                ("SERPER_API_KEY", self.serper_api_key),
                ("ANTHROPIC_API_KEY", self.anthropic_api_key),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                f"Missing required environment variable(s): {', '.join(missing)}.\n"
                "Copy .env.example to .env and fill them in, or run the offline "
                "stages only. See README for details."
            )


def load_config() -> Config:
    cfg = Config(
        serper_api_key=os.getenv("SERPER_API_KEY"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        input_file=Path(os.getenv("INPUT_FILE", "sample_invoices.xlsx")),
        output_file=Path(os.getenv("OUTPUT_FILE", "output/enriched_output.xlsx")),
        cache_dir=Path(os.getenv("CACHE_DIR", "cache")),
        max_serper_calls_per_row=int(os.getenv("MAX_SERPER_CALLS_PER_ROW", "6")),
        creditor_domains=tuple(
            d.strip().lower()
            for d in os.getenv("CREDITOR_DOMAINS", "fedex.com").split(",")
            if d.strip()
        ),
        resolution_model=os.getenv("RESOLUTION_MODEL") or None,
    )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
    return cfg
