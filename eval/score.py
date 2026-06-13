"""Score a pipeline run against the hand-verified gold set.

    python eval/score.py                     # scores output/enriched_output.jsonl
    python eval/score.py path/to/results.jsonl eval/gold.jsonl

Reports the metrics the brief's rubric actually weights:

  - wrong-contact rate : of emitted emails, the fraction pointing at the wrong
    company (the headline reliability number; should be 0).
  - domain accuracy    : of rows with a known company, fraction resolved to it.
  - coverage           : enriched / (rows that aren't internal facilities).
  - correct abstention : of rows where no email should be findable, the fraction
    where the pipeline correctly did NOT emit one (honest failure is a feature).

Gold rows marked `verified_by: needs_human` (a resolution this session could only
call *plausible*, not *confirmed*) are excluded from the headline numbers and
listed separately, so the score is never inflated by grading against a guess.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_RESULTS = _ROOT / "output" / "enriched_output.jsonl"
_DEFAULT_GOLD = _ROOT / "eval" / "gold.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _domain(record: dict) -> str | None:
    resolution = record.get("resolution") or {}
    return resolution.get("domain")


def _emitted_email(record: dict) -> str | None:
    for contact in record.get("contacts", []):
        if contact.get("email"):
            return contact["email"]
    return None


def _domain_congruent(email: str, expected: str | None) -> bool:
    if not expected:
        return True  # Without a known domain, nothing contradicts the email.
    dom = email.split("@")[-1].lower()
    expected = expected.lower()
    return dom == expected or dom.endswith("." + expected) or expected.endswith("." + dom)


def score(results: list[dict], gold: list[dict]) -> dict:
    by_id = {r["source"]["row_id"]: r for r in results}
    trusted = [g for g in gold if g.get("verified_by") != "needs_human"]
    deferred = [g for g in gold if g.get("verified_by") == "needs_human"]

    dom_total = dom_hit = 0
    emitted = wrong = 0
    abst_total = abst_ok = 0
    cov_total = cov_hit = 0
    wrong_rows: list[str] = []
    missing_rows: list[str] = []

    for g in trusted:
        rid = g["row_id"]
        rec = by_id.get(rid)
        if rec is None:
            missing_rows.append(f"row {rid} ({g['company']})")
            continue
        status = rec.get("status")
        email = _emitted_email(rec)
        expected = g.get("expected_domain")
        findable = g.get("email_findable")

        if g.get("ideal_verdict") != "skipped":
            cov_total += 1
            if status == "enriched":
                cov_hit += 1

        if expected:
            dom_total += 1
            if _domain(rec) == expected:
                dom_hit += 1

        if email:
            emitted += 1
            if findable is False or not _domain_congruent(email, expected):
                wrong += 1
                wrong_rows.append(f"row {rid} ({g['company']}): emitted {email}")

        if findable is False and g.get("ideal_verdict") != "skipped":
            abst_total += 1
            if not email:
                abst_ok += 1

    def pct(num: int, den: int) -> float | None:
        return round(num / den, 3) if den else None

    return {
        "trusted_rows": len(trusted),
        "scored_rows": len(trusted) - len(missing_rows),
        "missing_rows": missing_rows,
        "deferred_rows": [f"row {g['row_id']} ({g['company']})" for g in deferred],
        "wrong_contact_rate": pct(wrong, emitted),
        "wrong_contacts": wrong_rows,
        "emails_emitted": emitted,
        "domain_accuracy": pct(dom_hit, dom_total),
        "domain_hits": f"{dom_hit}/{dom_total}",
        "coverage": pct(cov_hit, cov_total),
        "coverage_hits": f"{cov_hit}/{cov_total}",
        "correct_abstention": pct(abst_ok, abst_total),
        "abstention_hits": f"{abst_ok}/{abst_total}",
    }


def format_report(m: dict) -> str:
    def show(v) -> str:
        return "n/a" if v is None else f"{v:.1%}" if isinstance(v, float) else str(v)

    lines = [
        "Evaluation vs gold set",
        "=" * 52,
        f"  Trusted rows scored      : {m['scored_rows']} of {m['trusted_rows']}",
        f"  Wrong-contact rate       : {show(m['wrong_contact_rate'])}  ({m['emails_emitted']} emails emitted)",
        f"  Domain accuracy          : {show(m['domain_accuracy'])}  ({m['domain_hits']})",
        f"  Coverage (enriched)      : {show(m['coverage'])}  ({m['coverage_hits']})",
        f"  Correct abstention       : {show(m['correct_abstention'])}  ({m['abstention_hits']})",
    ]
    if m["missing_rows"]:
        lines.append(
            f"  WARNING: {len(m['missing_rows'])} gold row(s) absent from results "
            f"(partial run?), excluded from the metrics above: "
            + ", ".join(m["missing_rows"])
        )
    if m["wrong_contacts"]:
        lines.append("  WRONG CONTACTS:")
        lines += [f"    - {w}" for w in m["wrong_contacts"]]
    if m["deferred_rows"]:
        lines.append(f"  Excluded (needs human confirm): {', '.join(m['deferred_rows'])}")
    return "\n".join(lines)


def main() -> int:
    results_path = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_RESULTS
    gold_path = Path(sys.argv[2]) if len(sys.argv) > 2 else _DEFAULT_GOLD
    if not results_path.exists():
        print(f"No results at {results_path}. Run `python run.py` first.", file=sys.stderr)
        return 2
    metrics = score(load_jsonl(results_path), load_jsonl(gold_path))
    print(format_report(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
