# Superpower Roadmap — Threads Operator

This document records the planned Superpower sequence for the Threads Operator + Hermes stack.

The implementation order is intentional. Complete and stabilize each dependency before moving to the next task.

## Execution Order

| Task | Status | Purpose |
|---|---|---|
| #2 | Current priority | Context-aware engagement and lead/DM handoff |
| #3 | Current priority | Telegram approval to publish-ready queue with smart scheduling |
| #4 | Planned / next | Closed-loop content performance intelligence |
| #5 | Planned / deferred | Autonomous content planner with human approval |
| #6 | Planned / deferred | Account-aware trend discovery and engagement optimization |
| #7 | Planned / deferred | Telegram-first onboarding and true plug-and-play deployment |

## Architecture Rule

Threads Operator remains the deterministic runtime.

- Threads Operator owns API calls, data collection, datastore state transitions, queue safety, account isolation, feedback persistence, and publishing gates.
- Hermes owns interpretation, strategy, model-driven analysis, content generation, planning, and account-aware trend reasoning.
- Telegram is the primary human operator interface for onboarding, approvals, edits, rejects, skips, scheduling, and account settings.
- Generated or planned content must not bypass the approval gate.
- No autonomous LLM-generated content is published merely because Hermes created or selected it.
- Important production behavior must be reproducible from this repository; no required integration may exist only as an untracked/manual edit inside a Hermes installation.

---

## Superpower Task #4 — Closed-Loop Content Intelligence

### Objective

Make the system learn from the performance of its own published Threads posts so future timing and content decisions are based on observed account data rather than static assumptions.

### Core Loop

```
publish
  -> collect performance snapshots
  -> normalize/store metrics
  -> analyse patterns
  -> store reusable learnings
  -> feed learnings into future scheduling/content decisions
```

### Planned Data

For each published post, retain enough structured data to evaluate performance:

- account
- Threads post ID
- publish timestamp
- topic / topic tag
- content pillar
- hook / opening pattern
- format
- post length
- main-post vs reply-chain structure
- scheduled time vs actual publish time
- views
- likes
- replies
- reposts
- quotes where available
- engagement rate / comparable derived metrics
- age of post when snapshot was taken

Suggested observation windows:

- ~1 hour
- ~6 hours
- ~24 hours

Exact windows can be adjusted after reviewing API limits and actual posting volume.

### Intelligence Layer

Hermes should be able to derive account-specific observations such as:

- which posting windows perform best by content pillar;
- which hooks or formats tend to generate replies vs views;
- whether a topic is improving, flat, or showing fatigue;
- whether long or short posts perform better for a given topic;
- whether reply chains improve continuation/engagement;
- whether recent performance is materially different from the historical baseline.

The system should retain evidence behind each learning instead of storing unsupported natural-language conclusions only.

### Task #3 Integration

Task #3 smart scheduling should eventually consume Task #4 outputs.

Example:

```
approved content
  -> identify topic/content pillar
  -> query recent performance history
  -> score candidate posting windows
  -> select recommended slot
  -> enqueue approved post
```

Until enough data exists, Task #3 may use a conservative fallback schedule.

### Guardrails

- Do not optimize using one viral/outlier post alone.
- Separate recent trends from long-term baselines.
- Keep raw metrics available so Hermes conclusions can be audited.
- Never change or republish content automatically based only on performance data.
- Account data and learnings must remain account-scoped.

### Definition of Done

Task #4 is complete when:

1. published posts receive reliable performance snapshots;
2. snapshots are stored in an account-scoped structured form;
3. Hermes can query historical performance by topic/time/format;
4. the system can produce explainable scheduling/content observations;
5. Task #3 can consume those observations for best-time recommendations;
6. fallbacks exist when data is sparse or unavailable;
7. regression tests protect metrics ingestion and account isolation.

---

## Superpower Task #5 — Autonomous Content Planner

### Objective

Use historical performance, current queue state, content pillars, recent posting history, and eligible trend candidates to prepare the next content plan automatically.

This is a planning system, not an unrestricted autonomous publisher.

### Inputs

Hermes may use:

- Task #4 performance intelligence;
- existing approved/draft queue;
- recent published topics;
- account persona;
- content pillars;
- trend candidates;
- campaign/business priorities supplied externally;
- topic fatigue / repetition signals;
- suitable posting windows from Task #3/#4.

### Planned Flow

```
performance intelligence
+ current queue
+ recent posts
+ content pillars
+ trend candidates
        |
        v
Hermes builds proposed plan
        |
        v
Hermes drafts candidate content
        |
        v
dedupe / repetition / safety checks
        |
        v
recommended publishing slots
        |
        v
Telegram approval
        |
        v
approved items enter publish-ready queue
```

### Planner Responsibilities

The planner should:

- avoid repetitive topics and near-duplicate hooks;
- maintain a sensible mix of content pillars;
- account for already-scheduled posts before proposing more;
- use observed high-performing windows where evidence exists;
- preserve the selected account persona;
- explain why each proposed item exists;
- distinguish evergreen content from time-sensitive trend responses;
- allow a user to approve, edit, reject, skip, or reschedule.

Example output:

```
Tomorrow's proposed plan

08:10 — Work / upskill
12:15 — Digital marketing
17:30 — Trend response
20:15 — Fatherhood reflection

Reasoning:
- fatherhood historically stronger at night;
- digital marketing has not been posted recently;
- selected trend candidate is still timely;
- queue has no overlapping topic in those windows.
```

### Human Approval Gate

Required invariant:

**Hermes may autonomously propose and draft. It must not autonomously approve its own newly generated content for publication.**

Publishing continues to require the existing approval contract.

### Definition of Done

Task #5 is complete when:

1. Hermes can generate a proposed daily/next-cycle plan from live account context;
2. the planner checks the existing queue and recent published history;
3. recommendations consume Task #4 learnings where available;
4. proposed posts include explainable timing/topic reasoning;
5. duplicate/topic-fatigue protections are active;
6. Telegram approval/edit/reject/reschedule works end-to-end;
7. only approved content reaches the publish-ready queue;
8. no planner path bypasses account isolation or publishing safety gates.

---

## Superpower Task #6 — Account-Aware Trend Discovery & Engagement Optimization

### Objective

Discover and rank trends based on the selected account's identity, audience, content pillars, persona, goals, historical performance, and operator feedback — with the explicit objective of improving engagement on the account's own Threads content.

The system must not optimize for generic virality alone.

Primary question:

> What conversation is currently relevant enough to this account that publishing a useful account-native angle is likely to improve engagement with the account?

### Account Profile Inputs

Each account should have one structured source of truth containing at least:

- account key / Threads handle;
- primary account goals;
- target audience;
- persona / voice;
- core content pillars;
- adjacent topics;
- topics to avoid;
- language/style preferences;
- CTA preferences;
- exploration ratio;
- timezone;
- approval policy.

Example concept:

```yaml
content_pillars:
  - digital_marketing
  - packaging
  - fatherhood

adjacent_topics:
  - entrepreneurship
  - sales
  - career
  - upskilling
  - AI_productivity
  - SME_business

primary_goal:
  - increase_account_engagement

exploration_ratio: 0.20
```

The schema must be generic and account-scoped. The values above are examples, not hardcoded defaults for all users.

### Candidate Selection Model

Candidate ranking should combine explainable signals such as:

- account relevance;
- engagement opportunity;
- current trend momentum;
- persona fit;
- original-angle potential;
- historical performance of related topics/angles when Task #4 data exists;
- repetition/topic-fatigue penalties;
- recent operator feedback.

A practical deterministic first version is preferred over an opaque ML system.

Trend momentum alone must never be sufficient for selection.

### Core Flow

```
account profile
+ persona
+ Task #4 performance intelligence
+ recent own-account posts
+ current trend discovery
+ operator feedback
        |
        v
account-relevance filter
        |
        v
engagement-opportunity scoring
        |
        v
candidate + proposed account-native angle
        |
        v
Telegram: Approve / Edit / Reject / Skip
        |
        v
Task #3 scheduling
        |
        v
publish
        |
        v
Task #4 performance measurement
        |
        v
improve future candidate ranking
```

### Telegram Feedback Semantics

The operator actions must have distinct meanings:

- **Approve** — positive signal for the topic/angle; continue to scheduling.
- **Edit + Approve** — topic may be useful, but the generated angle/copy needed correction.
- **Skip** — neutral; do not treat the topic as negative.
- **Reject** — negative signal; optionally ask for a structured rejection reason.

Recommended rejection reasons:

- Not relevant to this account
- Weak angle
- Too generic
- Already covered
- Do not want this topic
- Other

Feedback must be persisted account-scoped and auditable. Do not treat LLM inference as explicit user feedback.

### Exploration

Avoid overfitting the account to only previously successful subjects.

Support a configurable exploration share so most candidates stay close to core/adjacent pillars while a minority can explore credible neighboring conversations.

Exploration candidates must still have a defensible connection to the account's persona or audience.

### Skills / Runtime Boundary

Task #6 should add a Hermes skill such as:

`skills/threads-trend-strategist/SKILL.md`

The skill owns model reasoning about:

- account relevance;
- conversation fit;
- possible angle;
- why the candidate may create useful engagement.

Deterministic code owns:

- account-profile retrieval;
- candidate persistence;
- score inputs;
- state transitions;
- feedback persistence;
- dedupe;
- queue linkage;
- account isolation.

### Definition of Done

Task #6 is complete when:

1. trend discovery consumes an account-scoped structured profile;
2. generic viral-but-irrelevant candidates are filtered or strongly down-ranked;
3. each surfaced candidate has explainable relevance and engagement-opportunity evidence;
4. Approve/Edit/Reject/Skip have distinct persisted semantics;
5. rejected candidates can capture structured reasons;
6. Task #4 performance can influence future candidate ranking without one outlier dominating;
7. Task #3 scheduling remains the only publish scheduling path after approval;
8. account isolation and dedupe are regression-tested;
9. the Hermes trend strategy exists as a versioned skill in this repository;
10. candidate quality is optimized toward engagement on the account's own Threads presence, not generic trend participation.

---

## Superpower Task #7 — Telegram-First Onboarding & True Plug-and-Play Deployment

### Objective

Make a fresh Threads Operator deployment usable with minimal manual work:

```
fresh VPS
  -> install Hermes
  -> clone threads-operator
  -> bootstrap
  -> configure required secrets
  -> Telegram onboarding
  -> account profile confirmed
  -> doctor / runtime checks pass
  -> ready to operate
```

Telegram should become the primary onboarding and operator interface after the minimum secret/bootstrap setup.

### Onboarding UX

Do not force a new user through a long technical questionnaire.

Prefer an AI-assisted flow:

1. Ask the user to describe:
   - what the account is about;
   - who they want to reach;
   - what they want the account to achieve;
   - how they normally write.
2. Hermes extracts a proposed structured account profile.
3. Telegram shows a concise profile card.
4. User chooses:
   - Confirm
   - Edit
   - Add detail
5. Ask only for missing or ambiguous information.
6. Persist the confirmed account-scoped profile.
7. Run deterministic readiness checks.

Example confirmation card:

```
Account Profile

Goal:
Increase engagement + authority

Audience:
<derived audience>

Content pillars:
• <pillar 1>
• <pillar 2>
• <pillar 3>

Voice:
<derived voice>

Adjacent topics:
<derived topics>

Trend exploration:
20%

[Looks Good]
[Edit]
```

### Minimum Profile Contract

Onboarding should resolve at least:

- account key;
- Threads handle;
- primary goal(s);
- audience;
- persona/voice;
- content pillars;
- adjacent topics;
- avoid topics;
- language;
- CTA preference;
- trend exploration level;
- posting frequency preference;
- timezone;
- human-approval policy.

Secrets must not be written into public persona/profile files.

### Skill

Add a versioned Hermes skill such as:

`skills/threads-onboarding/SKILL.md`

The skill may interpret natural-language answers and propose account-profile fields.

It must not own credentials, API calls, or irreversible configuration writes.

Deterministic Threads Operator code validates and persists confirmed settings.

### Portability / Hermes Integration

A fresh clone must reproduce every required Threads Operator integration.

Any Hermes gateway/plugin wiring required by Threads Operator must be:

- versioned in this repository, or
- installed from a versioned integration artifact/script, or
- provided through an official supported Hermes plugin mechanism.

Do not depend on manually editing `~/.hermes/hermes-agent/` as an undocumented deployment step.

Target concept:

```
threads-operator/
  integrations/
    hermes/
      ...
  scripts/
    install_hermes_integration.sh
```

The exact mechanism should follow the safest supported Hermes extension path available at implementation time.

### Readiness

The Telegram flow should surface deterministic results from the existing doctor/runtime checks instead of pretending setup succeeded.

Example:

```
Setup status

PASS Threads credentials
PASS Supabase
PASS Telegram
PASS Persona/account profile
PASS Runtime jobs
WARN Browser profile not configured

Threads Operator is ready for API publishing.
Activity browser collection remains disabled until browser login is completed.
```

### Definition of Done

Task #7 is complete when:

1. a fresh VPS can clone and bootstrap the repository without copying hidden production files;
2. required Hermes integration is reproducible from versioned repository assets;
3. secrets remain local and out of Git;
4. Telegram can guide a new account through persona/audience/pillar/goal onboarding;
5. the user confirms the generated profile before it becomes active;
6. Task #6 consumes the same confirmed account profile;
7. onboarding runs deterministic readiness checks and accurately reports PASS/WARN/FAIL;
8. runtime jobs can be installed/converged without hand-editing Hermes internals;
9. the process is documented and tested from a clean environment;
10. a clean install reaches an operational state using repository code + local credentials + explicit user confirmation.

---

## Dependency Gate

Do not implement later tasks merely because this roadmap exists.

Recommended sequence:

```
Task #2 stable
    -> Task #3 stable
        -> Task #4 metrics + learning loop
            -> integrate #4 with #3 scheduling
                -> Task #5 autonomous planner
                    -> Task #6 account-aware trend optimization
                        -> Task #7 Telegram onboarding + plug-and-play deployment
```

Task #5 depends heavily on the quality of Tasks #3 and #4.

Task #6 should consume Task #4 evidence and integrate with Task #3 scheduling rather than creating a separate publishing path.

Task #7 should package the proven workflows from Tasks #2–#6 into a reproducible onboarding and deployment experience instead of hiding unfinished behavior behind a setup wizard.
