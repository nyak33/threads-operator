# Superpower Roadmap — Threads Operator

This document records the planned Superpower sequence for the Threads Operator + Hermes stack.

The implementation order is intentional. Do not start Task #4 or #5 until Tasks #2 and #3 are stable enough to support them.

## Execution Order

| Task | Status | Purpose |
|---|---|---|
| #2 | Current priority | Context-aware engagement and lead/DM handoff |
| #3 | Current priority | Telegram approval to publish-ready queue with smart scheduling |
| #4 | Planned / deferred | Closed-loop content performance intelligence |
| #5 | Planned / deferred | Autonomous content planner with human approval |

## Architecture Rule

Threads Operator remains the deterministic runtime.

- Threads Operator owns API calls, data collection, datastore state transitions, queue safety, account isolation, and publishing gates.
- Hermes owns interpretation, strategy, model-driven analysis, content generation, and planning.
- Generated or planned content must not bypass the approval gate.
- No autonomous LLM-generated content is published merely because Hermes created or selected it.

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

## Dependency Gate

Do not begin implementation of Tasks #4 and #5 merely because this document exists.

Recommended sequence:

```
Task #2 stable
    -> Task #3 stable
        -> Task #4 metrics + learning loop
            -> integrate #4 with #3 scheduling
                -> Task #5 autonomous planner
```

Task #5 depends heavily on the quality of Tasks #3 and #4. Building it earlier would produce an apparently autonomous system whose scheduling and planning are still based on weak assumptions.
