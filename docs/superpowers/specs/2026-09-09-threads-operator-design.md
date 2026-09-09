# Threads Operator — Design Specification

Date: 2026-09-09
Status: Approved architecture, implementation pending

## 1. Objective

Build a reusable Hermes skill suite that operates a Threads account as a lightweight social-media and sales-development operator.

The system should help with:
- content creation
- engagement discovery and reply drafting
- commercial lead discovery and qualification
- performance analysis
- content optimization based on measured results
- top-level orchestration through one operator skill

The repository must remain generic and safe to publish. It must not contain real API tokens, Supabase keys, Threads access tokens, Telegram bot tokens, account IDs, private customer data, or other secrets.

## 2. Success Criteria

The project is successful when a Hermes installation can clone/install this repository, configure environment variables and account-specific profiles, and use the following capabilities independently or through one parent operator:

1. `threads-content`
2. `threads-lead-hunter`
3. `threads-engagement`
4. `threads-insights`
5. `threads-content-optimizer`
6. `threads-operator`

The operator must be able to call the specialist skills without duplicating their logic.

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

- Hermes performs reasoning, classification, drafting, scoring, and orchestration.
- Existing external APIs/tools perform deterministic actions such as posting, database writes, scheduled execution, or message delivery.
- Supabase or another datastore may be used for persistent state, but the skills must not hard-code a single backend.
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

Purpose: convert account metrics into useful decisions.

Responsibilities:
- consume post/account metrics supplied by configured data sources
- calculate basic performance summaries
- compare topics, formats, post lengths, CTA usage, posting windows, and other available dimensions
- identify strong and weak patterns
- distinguish reach, engagement, authority, and conversion goals where data supports it
- produce daily/weekly summaries

It must not claim causation from correlation when sample sizes are weak.

### 4.5 `threads-content-optimizer`

Purpose: turn insights into changes for future content.

Responsibilities:
- read recent insights
- recommend content-mix changes
- identify patterns worth testing
- preserve strategically important low-reach content categories such as technical authority content when appropriate
- suggest controlled experiments rather than blindly maximizing views
- record recommendations in a machine-readable form when a datastore is available

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

## 6. Data and Integration Boundaries

The skills should work with tool adapters rather than directly coupling business logic to one service.

Potential integrations:
- Threads API or existing Threads executor
- Supabase
- Telegram approval flow
- cron/Hermes scheduler
- `social-lead-hunter`
- future n8n workflows

The initial implementation should not require n8n.

## 7. Approval and Automation Policy

Default behavior for risky outward actions:
- content generation: safe to automate into a queue
- lead discovery: safe to automate
- lead scoring: safe to automate
- reply drafting: safe to automate
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
│   ├── generic.example.yaml
│   ├── packaging.example.yaml
│   ├── personal.example.yaml
│   └── affiliate.example.yaml
├── schemas/
│   ├── content.schema.json
│   ├── lead.schema.json
│   └── insight.schema.json
├── scripts/
│   └── validate_config.py
├── tests/
│   ├── test_profiles.py
│   ├── test_schemas.py
│   └── test_no_secrets.py
└── docs/
    └── superpowers/specs/
```

Only add implementation files that are actually needed. Avoid a framework-heavy package unless executable code becomes necessary.

## 9. Security Requirements

Mandatory:
- no real credentials or tokens in repository history
- `.env` ignored
- `.env.example` contains placeholders only
- no hard-coded account IDs or customer data
- no logging of secrets
- example data must be fictional/generic
- tests should scan common secret patterns in tracked text files
- outbound actions require explicit configured execution mode

## 10. Error Handling

Skills should fail safely:
- missing integration -> explain which capability is unavailable and continue with draft/analysis-only behavior when possible
- missing metrics -> do not fabricate analysis
- unavailable Threads discovery/search -> accept candidates from configured alternative sources
- datastore unavailable -> return structured result without persistence
- posting failure -> preserve draft/status and report failure rather than marking success

## 11. Testing Strategy

Minimum verification before calling implementation complete:
- repository contains no obvious secrets
- example configuration parses successfully
- profile validation passes
- JSON schemas validate representative payloads
- operator routing examples resolve to the intended specialist skill
- content output respects profile constraints in fixture tests where practical
- lead score boundaries are deterministic for fixed fixtures
- no outward action is enabled by default

## 12. Implementation Order

1. repository foundation and secure configuration examples
2. shared schemas/profile format
3. `threads-lead-hunter`
4. `threads-engagement`
5. `threads-insights`
6. `threads-content`
7. `threads-content-optimizer`
8. `threads-operator`
9. documentation and installation instructions
10. verification/security checks

This order prioritizes direct commercial value and establishes data contracts before orchestration.

## 13. Non-Goals for Initial Version

Do not build these unless required later:
- n8n dependency
- custom web dashboard
- full CRM
- browser UI
- complex multi-agent framework
- embedded LLM provider logic
- hard-coded Packtica-specific business data
- automatic unrestricted replying

## 14. Future Extensions

Possible later additions:
- n8n adapter
- richer approval workflow through Telegram
- campaign experiment tracker
- account-level content memory
- multi-account orchestration
- conversion/revenue attribution
- dashboard/report generation

These remain optional and must not complicate the first usable version.
