# Domain context for development

How the job finder decides what is a lead, and why. Everything user-specific (titles, locations, visa needs,
company tiers, industry priorities, job boards) lives in the user's profile: `backend/rules.json`
(gitignored; copy `backend/rules.example.json` to start). Personal notes belong in `docs/*.local.md` (gitignored).

Any user can also override the main settings per run from the UI's Search filters panel (titles, locations, remote,
visa need: none / new H-1B sponsorship / H-1B transfer, sponsors-only, industries, companies to skip). See CLAUDE.md.

## Two tracks
**Full-time (FT, default)**
- Titles: the profile's `titles`. Secondary titles (program/project) score lower than primary ones.
- Geography (hard filter): the profile's `geo_pass_terms`, or fully remote US when `remote_ok`. A country-only
  location ("United States") counts as remote/unspecified; "City, ST, United States" for an out-of-area city fails.
- Geo ranking (sort only): `geo_rank` buckets, most preferred first.
- Recency: 24h for daily runs, 7 days (168h) for weekly runs. **Undated = fail.** A canonical posted date is required.
- Visa gate (only when `visa.enabled`): exclude postings that say no sponsorship / citizenship / clearance / US person.
  Company must be a **Strong or Moderate** sponsor. Unverified -> research, else exclude. Risk/Excluded -> out.
- `visa.mode`: `new_sponsorship` (needs a new petition) or `h1b_transfer` (already holds an H-1B). Postings that refuse
  NEW sponsorship but accept change-of-employer (transfer) petitions are classified `Transfer` and kept only in `h1b_transfer` mode.
- The whole JD is scanned: disclaimers usually sit at the end (in a live run, 144 of 147 were past the first 800 characters).
- Citizenship words in inclusive lists ("US citizens, green card and H1B holders welcome") don't block when sponsorship is
  also stated; "no clearance required" doesn't block. A skill mention like "FedRAMP experience" is not a citizenship requirement.

**Contract / C2C track**
- Same titles plus `fallback_titles`. 7-day recency.
- Visa: exclude only `contract_track.exclude_patterns` (USC/GC-only, no H1B, W-2 only, clearance).
- Staffing vendors are the target here. Vendor quality bar: verifiable site plus scale, track record or a named client.
- Good source: Dice (`filters.employmentType=THIRD_PARTY|CONTRACTS`, `filters.postedDate=SEVEN`, one keyword per query).

## Sponsorship (H1B) methodology
- Public sources: myvisajobs.com, h1bgrader.com, h1bdata.info (used by the agent), ellis.com, careery.pro.
- Company-wide Strong/Moderate filing volume is enough for **direct employers**. Staffing/consulting firms need
  role-specific confirmation on the posting; otherwise they fail FT (`staffing_c2c_only`) and belong on the C2C track.
- An explicit no-sponsorship or citizenship line on a posting always excludes, regardless of company tier.
- Thin volume (~2-6 LCAs/yr) excludes even large companies. An exact title-level LCA match is the strongest signal.
- Manual tiers in the profile win over automated research. Research results persist in `rules.local.json`'s
  `company_signals` map (re-checked after 90 days; Unverified after 7) and, when the private data repo is configured
  (see CLAUDE.md "Multi-user"), durably write through to and read back from `data/_company_signals.json` there --
  one shared, growing database of everything the app has ever learned about a company, read *before* re-researching.
  h1bdata.info must be queried per year: an "all years" query returns no table for large employers, which used to
  look like zero filings.
- Thresholds on recent yearly volume: >= 100 Strong; >= 20 (or >= 10 with a same-title filing) Moderate; 1-9 Risk; 0 Unverified.
- Company stage: prefer Series B+ or large enterprises with immigration counsel; very small companies = Risk by default.
- **Auto-reject flag**, separate from the LCA tier above: a company's own postings explicitly saying no
  sponsorship accumulate (deduped by URL, across runs) in that same per-company record. Once a company crosses
  `AUTO_REJECT_THRESHOLD` (default 2), `company_auto_reject` excludes its future postings even when a given
  posting's own text says nothing about sponsorship, and even if its LCA tier is Strong -- these are deliberately
  independent signals (a company can file plenty of LCAs company-wide while still refusing sponsorship for this
  specific role family). Only takes effect starting the run *after* the threshold is crossed, never retroactively
  within the run that crossed it.

## Scoring model (implemented in `fit_service.score_lead`, weights in profile `scoring`)
| Factor | Weight | Scoring |
|---|---|---|
| H1B signal | 35 | Strong 35, Moderate 22, Unverified 10, Risk 3, explicit no-sponsorship 0. Full points if `visa.enabled` is false |
| Title match | 25 | Primary 25, secondary 15, stretch 5 |
| Geography | 15 | In the profile's area or remote US 15, other 0 |
| Industry priority | 15 | Profile's `industry_priority` tiers (15 ... 5); no match = `opportunistic_points` |
| Contact found | 10 | Hiring-influence contact 10, recruiter only 6, none 0 |
| Feedback bias | +/-10 | Accumulated thumbs up/down (`/feedback`) by company and by title pattern (seniority-stripped), +3/-3 per net company signal, +2/-2 per net title-pattern signal, clamped |

Hot Lead = score >= `hot_threshold` (65). Total score is clamped to 0-100 after the feedback bias. Never pad
results with weak matches. Override: a JD asking for GenAI/agentic AI product work AND regulated/compliance
experience gets full industry points. `fit` (resume keyword coverage, or an LLM JD-vs-resume gap read on the
top-scoring passing leads when a BYO LLM key is configured) is a separate column; it does not drive ranking.

Each lead also carries a LinkedIn People-tab contact-search deep link (not a verified contact -- doesn't affect
`contact_points`) and the pre-written sponsorship-question answer for the user's `visa.mode`. "Draft outreach"
in the UI generates (never sends) a short note via the same BYO LLM key, or a template with no network call.

## Tracker rules
- The user's Google Sheet is canonical. **Append, never recreate**. Dedupe by job link.
- Appends map to the tab's existing header names (e.g. "Role", "Job Link"); unknown columns stay blank.
- Only Hot Leads go to the Sheet by default; sub-threshold and excluded jobs stay in the run log.
- Feedback loop: change scoring weights only after a pattern across >= 3 outcomes; explicit user preferences apply immediately.

## Sources learned so far
- LinkedIn: highest yield, no API; the JobSpy guest endpoint rate-limits (~10 pages/IP, HTTP 429). Logged-in scraping risks account bans.
- Indeed: most reliable JobSpy source. Glassdoor OK. Google/ZipRecruiter often empty or blocked upstream.
- JobSpy sessions have no read timeout; every call is wrapped in `JOBSPY_TIMEOUT`.
- Employer ATS boards (Greenhouse/Lever/Ashby): freshest data, canonical dates.
- Workday: public `POST /wday/cxs/<tenant>/<site>/jobs` (20/page, relative "Posted N Days Ago" only).
  The detail call gives startDate, endDate (application deadline), all locations and the description.
  Rate limit is per IP across tenants, so boards run one at a time with backoff.
- Aggregator dates can be in the future (unusable); some postings show a fresh date with an expired deadline.
- Paid sources (avoided): JSearch/Adzuna free tiers are tiny; commercial scrapers are paid.

## Guardrails
- Pre-approved: searching, scoring, writing/sorting the user's Sheet, updating docs.
- Never: submit applications or send messages for the user. Drafts only.
