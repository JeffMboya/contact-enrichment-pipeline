# Write-up: Contact Enrichment Pipeline

## Result on the 25 sample rows

The pipeline enriched 8 rows with a valid email, resolved another 8 to the correct
company domain that published no scrapeable email (a "partial"), skipped 2 internal FedEx
and USPS facility records, and returned an explained "not found" for the remaining 7.
Every email it emitted appears verbatim in a page it actually fetched, and no creditor
address reached the output. Scored against a hand-verified gold set with
`python eval/score.py`, the wrong-contact rate is 0 percent, correct abstention is 100
percent (6 of 6), domain accuracy is 86 percent (12 of 14), and coverage is 37 percent. I
optimized for zero wrong contacts over raw coverage, which is what the brief asks for.

## Approach and main design decisions

The pipeline is six small stages, and each row travels through them as a single record
that is either enriched or carries an explicit reason it could not be. It resolves the
company before looking for any contact and only ever searches on the debtor's own name and
address, because the brief's stated trap is enriching the creditor (FedEx), and a loose
search would chase our own client or a same-named company in another state.

The LLM judges from fetched evidence but never invents. It picks one of the search results
it was shown by index, and the domain it returns must equal that result's host before the
pipeline accepts it, because a model asked for a company's domain will readily produce a
plausible but fabricated one, and a substring test would let "me.com" pass for "acme.com".
Email extraction is then pure regex over fetched HTML with no model anywhere in the email
path, which is what makes a hallucinated email structurally impossible, since a model that
never sees a blank to fill cannot pattern-guess an address.

Resolution escalates rather than quitting. When the judge cannot confidently pick a
company it may propose one refined follow-up query grounded in the results, such as a
fuller legal name or an acquirer, and that query is searched before the row is abandoned.
An earlier version gave up after one or two of its six allowed queries and discarded rows
the search had effectively solved, and this escalation is what recovers Avient on its
parent domain, the Redflex acquisition by Verra Mobility, Duval Motor Company, and a sole
practitioner mapped to his group practice. Confidence is a deterministic rubric rather than
a number the model produces, so a reviewer can see exactly why a row scored 0.27 instead of
trusting an unauditable score, and an unenrichable row is a first-class result carrying a
machine-readable reason and the judge's own verdict as a human sentence, because an honest
account of what was found beats both a confident wrong guess and a silent blank. The
shipped run uses Opus as the judge, but the cheaper default model agrees on 14 of the 16
resolved domains, which is the real point: the guardrails and the escalation loop, not any
one model's guess, carry the result.

## Guarding against wrong or hallucinated contacts

The guards close every path by which a contact could be wrong. A fabricated domain is
blocked because the judged domain must match the chosen result's host, an unrelated domain
is rejected because the resolved host must share a name token with the company, and
directory sites such as Yelp are filtered out before the judge sees them. A fabricated
email is impossible because extraction is regex only, a runtime assertion fails if the
evidence snippet does not contain the email, and a post-run audit independently re-checks
that every emitted address appears verbatim in a fetched page and exits non-zero
otherwise. An unreachable address is dropped by a hard check for mail records. The creditor
is kept out of the debtor's results by both a configured domain list and name-affinity to
whatever company appears in the invoice's issuing-company column, so the guard holds for
any client and not only FedEx. Overconfidence is tempered because generic, freemail,
off-domain, and truncated cases all lower the score, and a branch address discounts a match
rather than asserting it. The fetcher refuses any internal or cloud-metadata host on every
redirect hop, and cell values drawn from untrusted pages are neutralized so they cannot
execute as spreadsheet formulas.

## What I would skip, and what I would do with more time

Good reasoning on the hard tail matters more than forcing all 25 rows, so several are
deliberately left unenriched with a specific reason rather than a guess: companies with no
real web presence that surface only directory listings (Dobbs Ram, Zumpano, Medlab), a
truncated name that returns nothing usable (Riverside Infection Const), an institution that
lives on a university host domain (the CUNY Puerto Rican studies center), and an acquired
firm whose successor page could not be verified (L.M. Scofield, now part of Sika). With
more time I would add SMTP and bounce verification beyond the mail-records check, point the
constrained reader (already built and fenced, though this sample's pages named no one to
ground) at richer team and accounts-payable pages where it can attach a named person and
role, and move the sequential fetch and search work onto asynchronous I/O with single-pass
workbook writing for larger inputs. I would also close one residual in the SSRF guard:
it validates a host's resolved IP, but the HTTP client resolves the name again at connect
time, so a DNS-rebinding race could still reach an internal address. Pinning the
connection to the already-validated IP closes it. This matters only if the pipeline runs
as a hosted service rather than a local script, which is why it is noted here rather than
fixed.

## Evaluating quality at scale (thousands of rows)

The gold set, the scoring script, and the always-on invariant audit are already the
small-scale version of this loop. At volume the hand-verified gold set would be replaced by
outcome labels, namely whether a chased email bounced or drew a reply, and the confidence
bands would be calibrated against the real bounce rate in each band. A weekly stratified
human review would sample the high-confidence band most heavily, since a confident wrong
contact is the costly error, and the dashboard would track wrong-contact rate, coverage,
and cost per enriched row, and alert when any of them drifts.
