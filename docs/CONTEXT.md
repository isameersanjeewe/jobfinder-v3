# Domain context for development

How the job finder decides what is a lead, and why. Everything user-specific (titles, locations, visa needs,
company tiers, industry priorities, job boards) lives in the user's profile: `backend/rules.json`
(gitignored; copy `backend/rules.example.json` to start). Personal notes belong in `docs/*.local.md` (gitignored).

## Two tracks
**Full-time (FT, default)**
- Titles: the profile's `titles`. Secondary titles (program/project) score lower than primary ones.
- Geography (hard filter): the profile's `geo_pass_terms`, or fully remote US when `remote_ok`. A country-only
  location ("United States") counts as remote/unspecified; "City, ST, United States" for an out-of-area city fails.
- Geo ranking (sort only): `geo_rank` buckets, most preferred first.
- Recency: 24h for daily runs, 7 days (168h) for weekly runs. **Undated = fail.** A canonical posted date is required.
- Visa gate (only when `visa.enabled`): exclude postings that say no sponsorship / citizenship / clearance.
  Company must be a **Strong or Moderate** sponsor. Unverified -> research, else exclude. Risk/Excluded -> out.

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
- Manual tiers in the profile win over automated research. Research results persist per user in `rules.local.json`.
- Company stage: prefer Series B+ or large enterprises with immigration counsel; very small companies = Risk by default.

## Scoring model (implemented in `fit_service.score_lead`, weights in profile `scoring`)
| Factor | Weight | Scoring |
|---|---|---|
| H1B signal | 35 | Strong 35, Moderate 22, Unverified 10, Risk 3, explicit no-sponsorship 0. Full points if `visa.enabled` is false |
| Title match | 25 | Primary 25, secondary 15, stretch 5 |
| Geography | 15 | In the profile's area or remote US 15, other 0 |
| Industry priority | 15 | Profile's `industry_priority` tiers (15 ... 5); no match = `opportunistic_points` |
| Contact found | 10 | Hiring-influence contact 10, recruiter only 6, none 0 |

Hot Lead = score >= `hot_threshold` (65). Never pad results with weak matches.
Override: a JD asking for GenAI/agentic AI product work AND regulated/compliance experience gets full industry points.
`fit` (resume keyword coverage) is a separate column; it does not drive ranking.

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
