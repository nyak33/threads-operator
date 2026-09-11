# Threads Operator — Design Specification

Date: 2026-09-09
Status: Approved architecture, implementation in progress

## 1. Objective

Build a reusable Hermes skill suite that operates a Threads account as a lightweight social-media and sales-development operator.

The system should help with:
- content creation
- engagement discovery and reply drafting
- commercial lead discovery and qualification
- persistent performance tracking and historical growth analysis
- content optimization based on measured results
- top-level orchestration through one operator skill

The repository must remain generic and safe to publish. It must not contain real API tokens, Supabase keys, Threads access tokens, Telegram bot tokens, account IDs, phone numbers, private customer data, or other secrets.

## 2. Success Criteria

The project is successful when a Hermes installation can clone/install this repository, configure environment variables and account-specific profiles, and use the following capabilities independently or through one parent operator:

1. `threads-content`
2. `threads-lead-hunter`
3. `threads-engagement`
4. `threads-insights`
5. `threads-content-optimizer`
6. `threads-operator`

The operator must be able to call the specialist skills without duplicating their logic.

For Insights specifically, success requires preserving raw historical snapshots so that future analysis is not limited to the current lifetime totals returned by Threads.

## 3. Architecture

```text
Threads / configured data sources
            |
            v
     threads-operator
            |
   +--------+---------+----------------+----------------+
   |                  |                |                |
   v                  v                v                v
threads-content  lead-hunter     engagement       insights
   |                  |                |                |
   +------------------+----------------+--------+-------+
                                             |
                                             v
                                  content-optimizer
                                             |
                                             v
                                      next content cycle
```

### Separation of responsibilities

- Hermes performs reasoning, classification, drafting, scoring, analysis, and orchestration.
- Deterministic code performs API reads, snapshot persistence, arithmetic, scheduled execution, posting, and message delivery.
- Supabase/Postgres is the recommended first persistence adapter for historical Insights, but business logic must remain portable and must not embed deployment-specific credentials.
- `social-lead-hunter` remains a separate reusable repository. `threads-operator` may call or consume its output when configured.

## 4. Skill Responsibilities

### 4.1 `threads-content`

Purpose: generate Threads posts that follow a configured account/content profile.

Responsibilities:
- load account/content profile
- load recent-post context when available
- avoid repeating recent hooks, topics, punchlines, CTAs, and structures
- generate main post and optional reply chain
- support campaign-specific rules
- return structured output suitable for queueing or approval

Expected output fields:
- main_post
- replies[]
- topic
- angle
- content_profile
- suggested_posting_window (optional)
- CTA_present
- notes

### 4.2 `threads-lead-hunter`

Purpose: detect commercially relevant Threads posts and qualify them.

Responsibilities:
- accept candidate posts from configured discovery sources
- determine whether the post is a genuine commercial opportunity
- extract likely requirement, location, urgency, quantity/spec clues, and buyer intent
- score lead confidence and commercial fit
- recommend action
- draft a natural public reply when appropriate
- log structured lead data when a datastore tool is available

Default score bands:
- 80–100: high priority
- 60–79: likely reply / review
- 40–59: observe
- below 40: ignore

The scoring model must be configurable rather than hard-coded to one company.

### 4.3 `threads-engagement`

Purpose: grow account visibility and relationships without turning every reply into a sales pitch.

Responsibilities:
- assess whether a post is worth engaging with
- classify possible response style: useful, conversational, humorous, commercial
- draft short, context-aware replies
- avoid spam, repetitive comments, fake expertise, and forced CTAs
- respect configurable daily reply limits and account safety rules

### 4.4 `threads-insights`

Purpose: turn current Threads metrics into persistent historical analytics and useful decisions.

#### Capture responsibilities

- collect account-level Insights from configured Threads data sources
- collect post-level Insights for owned posts
- append immutable snapshots rather than overwriting prior values
- preserve collection timestamp and post age at capture time
- tolerate metrics that are unavailable or null without converting missing values into fabricated zeroes
- warn when collection becomes stale or an integration fails

#### Historical metrics

The system should support calculating, when sufficient snapshots exist:
- follower gain and follower growth rate by hour/day/week/month
- profile-view growth by hour/day/week/month
- post view velocity such as views/minute and views/hour
- like, reply, repost, quote, and share velocity where the source metric exists
- peak velocity and time-to-peak
- velocity decay
- views at comparable post ages such as 15m, 30m, 1h, 3h, 24h, 3d, and 7d
- week-over-week and month-over-month growth
- moving averages and historical baselines
- per-topic, per-angle, per-hook, per-CTA, and per-content-profile performance when content metadata exists

#### Same-age benchmarking

A post should be compared against historical posts at similar ages rather than comparing a new post directly with an old lifetime total.

Example questions the system should eventually answer:
- Is this post above or below the normal 30-minute view benchmark?
- What was its highest views/minute period?
- Is its first-hour velocity accelerating or decaying?
- Which content categories produce reach versus authority or commercial intent?

#### Pattern and event detection

Derived analytics may identify:
- fast-burn posts
- slow-burn posts
- unusual acceleration after a slowdown (a "second-wave" pattern)
- unusually weak or strong velocity versus historical baseline
- stale metric collection
- topic fatigue from recent content history

A second-wave label is an internal description of the observed velocity pattern. It must not be presented as proof that Meta officially initiated a specific distribution event.

#### Data interpretation labels

Reports must distinguish these evidence classes:
- `measured`: directly returned by a configured source
- `calculated`: deterministic arithmetic from measured snapshots
- `inferred`: interpretation of observed patterns
- `attributed`: conversion attribution backed by an explicit tracking mechanism

The system must not silently present an inferred relationship as a measured fact.

#### Attribution limits

Without explicit conversion tracking, the system must not claim:
- an exact number of followers caused by a particular post
- an exact number of profile visits caused by a particular post or reply
- an exact sales conversion caused by a particular post

It may truthfully report temporal correlation, for example: `+8 followers during the three hours following publication`.

#### Sampling strategy

Default post snapshot cadence should be configurable. Recommended defaults:

| Post age | Minimum interval between snapshots |
|---|---:|
| 0–2 hours | 5 minutes |
| 2–6 hours | 15 minutes |
| 6–24 hours | 30 minutes |
| 1–3 days | 60 minutes |
| 3–7 days | 6 hours |
| More than 7 days | 24 hours |

Account-level snapshots should default to every 15 minutes. Deployments may reduce frequency to respect API or infrastructure constraints.

#### Persistence model

Recommended initial Postgres/Supabase entities:
- `threads_account_snapshots`: immutable account metric captures
- `threads_post_snapshots`: immutable per-post metric captures
- `threads_daily_rollups`: derived daily summaries, rebuildable from raw snapshots

Raw snapshots are the source of truth. Derived scores and rollups must be reproducible so formulas can change later without losing history.

### 4.5 `threads-content-optimizer`

Purpose: turn insights into changes for future content.

Responsibilities:
- read recent insights and historical benchmarks
- recommend content-mix changes
- identify patterns worth testing
- preserve strategically important low-reach content categories such as technical authority content when appropriate
- suggest controlled experiments rather than blindly maximizing views
- record recommendations in a machine-readable form when a datastore is available
- avoid optimizing exclusively for reach when engagement or business outcomes matter more

### 4.6 `threads-operator`

Purpose: provide one top-level interface for normal Hermes usage.

Responsibilities:
- inspect configured system state
- route requests to the correct specialist skill
- provide status summaries
- coordinate a full work cycle
- surface failures or items requiring human attention
- avoid duplicating specialist logic

Example intents:
- status
- create content
- hunt leads
- find engagement opportunities
- analyze performance
- optimize next content batch
- run full operator cycle

## 5. Account Profiles

The repo should support multiple configurable profiles without embedding personal/company-specific information in skill logic.

Example profile categories:
- packaging / B2B
- personal / note-to-self
- affiliate
- future custom campaigns

Profiles should be YAML or JSON files copied from safe examples. Real deployment-specific profile values may live outside the public repository or in ignored local config.

For deployments that use WhatsApp CTAs, the initial recommended attribution approach is intentionally simple: direct WhatsApp with a prefilled message such as `Hi, saya datang dari Threads.`. The public repository must use placeholder contact details only.

A branded redirect or Linktree-style tracking hub is not required for v1.

## 6. Data and Integration Boundaries

The skills should work with tool adapters rather than directly coupling business logic to one service.

Potential integrations:
- Threads API or existing Threads executor
- Supabase/Postgres
- Telegram approval flow
- cron/Hermes scheduler
- `social-lead-hunter`
- future n8n workflows

The initial implementation should not require n8n, Postiz, or browser automation.

## 7. Approval and Automation Policy

Default behavior for risky outward actions:
- content generation: safe to automate into a queue
- lead discovery: safe to automate
- lead scoring: safe to automate
- reply drafting: safe to automate
- analytics collection: safe to automate
- posting/replying: should support approval mode by default

The implementation must expose a configuration switch for execution mode, for example:
- `draft_only`
- `approval_required`
- `auto_post`

No skill should silently enable auto-posting merely because credentials exist.

## 8. Repository Structure

Planned structure:

```text
threads-operator/
├── README.md
├── LICENSE
├── .gitignore
├── .env.example
├── config.example.yaml
├── pyproject.toml
├── migrations/
│   └── 001_threads_insights.sql
├── src/
│   └── threads_operator/
│       ├── __init__.py
│       ├── insights.py
│       ├── collector.py
│       ├── cli.py
│       ├── threads_api.py
│       └── supabase_store.py
├── scripts/
│   └── collect_insights.py
├── skills/
│   ├── threads-content/
│   │   └── SKILL.md
│   ├── threads-lead-hunter/
│   │   └── SKILL.md
│   ├── threads-engagement/
│   │   └── SKILL.md
│   ├── threads-insights/
│   │   └── SKILL.md
│   ├── threads-content-optimizer/
│   │   └── SKILL.md
│   └── threads-operator/
│       └── SKILL.md
├── profiles/
├── schemas/
├── tests/
└── docs/
    └── superpowers/
        ├── specs/
        └── plans/
```

Only add implementation files that are actually needed. The historical Insights collector is the first executable subsystem and justifies a small focused Python package; avoid turning the repository into a general framework.

## 9. Security Requirements

Mandatory:
- no real credentials or tokens in repository history
- `.env` ignored
- `.env.example` contains placeholders only
- no hard-coded account IDs, phone numbers, or customer data
- no logging of secrets
- example data must be fictional/generic
- tests should scan common secret patterns in tracked text files
- outbound actions require explicit configured execution mode
- analytics collection must use read-only Threads operations and append-only snapshot persistence where practical

## 10. Error Handling

Skills and collectors should fail safely:
- missing integration -> explain which capability is unavailable and continue with draft/analysis-only behavior when possible
- missing metrics -> preserve null/unknown state; do not fabricate analysis
- unavailable Threads discovery/search -> accept candidates from configured alternative sources
- datastore unavailable -> return structured result without falsely claiming persistence
- partial per-post Insights failure -> continue other eligible posts and report failures
- posting failure -> preserve draft/status and report failure rather than marking success
- collection failure -> do not create a successful run marker for data that was not persisted

## 11. Testing Strategy

Minimum verification before calling implementation complete:
- repository contains no obvious secrets
- example configuration is safe by default
- snapshot interval selection is deterministic at age boundaries
- growth and velocity calculations handle zero and missing values safely
- same-age benchmark design does not compare incompatible ages
- second-wave detection requires a measurable slowdown followed by material acceleration
- collector skips posts that are not yet due for another snapshot
- partial metric failures do not erase successful snapshots
- unsupported individual metrics remain null where possible
- no outward action is enabled by default

## 12. Implementation Order

Historical collection is time-sensitive because old minute-level trajectories cannot be recreated after the fact. Therefore the initial implementation order is:

1. update source-of-truth specification
2. repository foundation and secure configuration examples
3. historical Insights schema and raw snapshot persistence
4. `threads-insights` collector and deterministic analytics primitives
5. `threads-insights` Hermes skill and operating instructions
6. shared schemas/profile format
7. `threads-content`
8. `threads-lead-hunter`
9. `threads-engagement`
10. `threads-content-optimizer`
11. `threads-operator`
12. documentation, installation, and verification/security checks

This supersedes the earlier order that placed lead hunting before Insights. The change is deliberate: every delayed collection interval is historical data that cannot be recovered later.

## 13. Non-Goals for Initial Version

Do not build these unless required later:
- n8n dependency
- custom web dashboard
- full CRM
- browser UI
- complex multi-agent framework
- embedded LLM provider logic
- hard-coded company-specific business data
- automatic unrestricted replying
- exact follower attribution to a post without an explicit attribution mechanism
- exact profile-visit attribution to a post without an explicit attribution mechanism
- branded redirect/link-hub click tracking
- reconstruction of minute-level history that was never captured

## 14. Future Extensions

Possible later additions:
- n8n adapter
- richer approval workflow through Telegram
- campaign experiment tracker
- account-level content memory
- multi-account orchestration
- branded redirect/link-hub click tracking
- stronger conversion/revenue attribution
- dashboard/report generation

These remain optional and must not complicate the first usable version.
