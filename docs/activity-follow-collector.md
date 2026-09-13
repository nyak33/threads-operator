# Threads Activity Follow Collector

## Objective

Add an **optional, read-only follower-attribution collector** that reads the authenticated Threads Web **Activity → Follows** view and records how many follows Threads attributes to each source post.

This supplements the official Threads Insights API. It must never replace or block the existing Insights collector.

## Verified constraints

The following was verified against the live Threads account/browser session on 2026-09-13:

- Official Threads post insights expose views, likes, replies, reposts, quotes, shares and clicks, but no per-post follow metric.
- Account insights expose aggregate follower information, not source-post attribution.
- Threads Web `/activity/follows` visibly exposes `Followed from your post` rows.
- Each follow-attribution row provides a notification ID, follow/notification timestamp, source-post text/snippet, visible username and grouped count such as `+20 others`.
- The rendered row does **not** expose the source post ID, shortcode, permalink or source-post publication timestamp.
- A CDP network capture of a clean `/activity/follows` load confirmed the server-prefetched `BarcelonaActivityFeedStoryListContainerQuery` payload also omits any source-post identifier. `extra.media_dict` exists in the schema but was `null` for all inspected follow notifications.
- Therefore exact source-post identity cannot be read directly from Activity. Matching must use the Activity snippet plus the account's own known post history.

## Required architecture

Keep this module isolated from the existing official API collector.

```text
Official Threads Insights Collector     Activity Follow Collector
              |                                  |
              |                                  v
              |                           raw follow events
              |                                  |
              +----------------------+-----------+
                                     v
                              analytics layer
```

If the Activity collector fails, posting, official Insights collection and other operator functions must continue normally.

## Matching strategy

For every new `Followed from your post` Activity event:

1. Preserve the raw source snippet exactly as Threads returned it.
2. Create a normalized comparison form for matching only.
3. Search the account's own post history for text/snippet matches.
4. Reject any candidate published after the notification time.
5. If one unique candidate remains, match it.
6. If multiple repeated-text candidates remain, choose the nearest prior candidate only as an inference, never as an exact fact.
7. Preserve the match method and confidence.
8. If ambiguity remains material, leave the post unresolved rather than silently assigning it.

Recommended confidence classes:

- `high`: unique normalized-text match to one eligible own post.
- `medium`: repeated text; nearest prior post selected by notification-time upper bound.
- `low`: multiple plausible candidates remain.
- `unknown`: no reliable match.

Activity notification time is an **upper bound only**. It is not the source post publication time and does not prove that the newest repeated post caused the follow.

## Raw event fields

Store enough raw information to improve matching later without having to re-scrape old Activity rows.

Recommended fields:

- `account_key`
- `notification_id`
- `notification_datetime`
- `notification_type`
- `visible_username` (optional for analytics; may be omitted from long-term storage)
- `source_snippet_raw`
- `source_snippet_normalized`
- `context_raw`
- `grouped_others_count`
- `follow_count`
- `matched_post_id`
- `matched_permalink`
- `match_method`
- `match_confidence`
- `collected_at`

For a grouped row such as `username and 20 others`, `follow_count = 21`.

## Deduplication

Primary deduplication key:

- Threads Activity `notification_id` / `tuuid`

Fallback fingerprint if Meta changes the internal notification ID representation:

- hash of `notification_datetime + source_snippet_raw + visible_username + grouped_others_count`

Never double-count the same Activity event across scans.

## Browser/session requirements

Use the deployment's existing authenticated persistent Chromium profile. Credentials, cookies and tokens must never be stored in this repository.

The collector must:

- read only `/activity/follows`
- never open follower profiles as part of normal operation
- never like, reply, follow, unfollow, publish or message
- never bypass CAPTCHA, 2FA, login challenges, rate limits or other Meta controls
- stop and alert on authentication/security challenges
- use conservative scan frequency
- avoid aggressive refresh, scrolling or retries

Recommended initial schedule: **2 scans per day**. Increase only after stability is proven and only if the additional freshness has clear value.

## Failure isolation

Provide a deployment-level kill switch, for example:

```text
ACTIVITY_FOLLOW_COLLECTOR_ENABLED=false
```

A failure in browser parsing or Activity access must not fail the official Insights collector.

## Analytics output

Expose Activity attribution separately from official metrics. Suggested derived field:

- `follows_from_post_activity`

Every downstream report must preserve attribution confidence. Do not present a heuristic repeated-text match as an exact fact.

Example:

```text
Post: Kadang jadi ayah ni kelakar...
Views: 10,739
Reposts: 132
Activity-attributed follows: 21
Attribution confidence: high
```

## Implementation goal for Hermes

Paste the following into Hermes when ready to implement:

```text
/goal Implement an OPTIONAL Threads Activity Follow Collector in the existing threads-operator deployment.

OBJECTIVE

Add a read-only collector that uses the already-authenticated persistent Chromium profile to read Threads Web Activity → Follows and store per-post follow-attribution events for analytics.

This collector supplements the existing official Threads Insights collector. It must never replace, block or modify the behavior of the official API collector.

SOURCE OF TRUTH

Before coding, read this repository's:
- README.md
- docs/activity-follow-collector.md
- existing Insights collector implementation
- existing migrations/tests/config conventions

Treat docs/activity-follow-collector.md as the specification for this feature.

KNOWN VERIFIED FACTS

1. Official Threads API does not expose per-post follow attribution.
2. Threads Web /activity/follows shows `Followed from your post` rows.
3. Activity gives notification ID, notification time, source-post snippet and grouped follow count.
4. Activity does NOT expose source post ID, permalink, shortcode or source-post timestamp.
5. A real CDP network capture of BarcelonaActivityFeedStoryListContainerQuery confirmed no source-post identifier exists in the naturally loaded payload.
6. Therefore matching must use source snippet + our own post history + notification-time upper bound.
7. Repeated identical text can remain ambiguous and must carry confidence rather than being presented as exact.

IMPLEMENTATION REQUIREMENTS

A. Inspect first
- Find the current repository/deployment layout.
- Identify the existing official Insights collector, database helpers, config loader, tests and scheduler conventions.
- Reuse existing patterns instead of creating a parallel framework.
- Report the implementation plan before editing if the required changes differ materially from the repository spec.

B. Activity browser reader
- Use the existing authenticated persistent Threads browser profile on the deployment.
- Open only https://www.threads.com/activity/follows
- Read `Followed from your post` rows.
- Do not open follower profiles.
- Do not perform any account actions.
- Parse at minimum:
  - notification_id / tuuid
  - notification_datetime
  - source_snippet_raw
  - visible_username when available
  - grouped `+N others` count
  - computed follow_count
  - context text if available
- Filter non-attribution rows such as `Requested`.

C. Deduplication
- Primary key: notification_id / tuuid.
- Add a deterministic fallback fingerprint from notification datetime + snippet + visible username + grouped count.
- Re-running the collector must not increase counts for already-seen events.

D. Source-post matching
- Match only against this account's own known post history.
- Normalize text conservatively for comparison while preserving raw text.
- Candidate post must have published_at <= notification_datetime.
- Unique eligible text match → confidence=high.
- Repeated text where nearest prior candidate is selected → confidence=medium unless stronger evidence exists.
- Multiple plausible candidates → confidence=low or unresolved.
- No reliable candidate → confidence=unknown/unresolved.
- Never fabricate a post ID.
- Never label a heuristic match as exact.

E. Storage
- Add the smallest migration/table required for append-only Activity attribution events.
- Keep raw events separate from rebuildable analytics/rollups.
- Store enough raw fields to re-run matching later without re-scraping Activity.
- Do not store browser credentials, cookies or session tokens.
- Avoid permanently storing follower usernames if not required for deduplication/analytics; if retained, document why.

Recommended logical fields:
account_key, notification_id, notification_datetime, notification_type,
visible_username(optional), source_snippet_raw, source_snippet_normalized,
context_raw, grouped_others_count, follow_count,
matched_post_id, matched_permalink, match_method, match_confidence,
fallback_fingerprint, collected_at.

F. Configuration and safety
- Add a kill switch such as ACTIVITY_FOLLOW_COLLECTOR_ENABLED.
- Default must be safe/off unless the existing deployment convention clearly requires otherwise.
- Never log credentials/cookies/tokens.
- Stop rather than bypass CAPTCHA, 2FA, suspicious-login challenges, rate limits or other Meta controls.
- Use conservative retries.
- Browser collector failure must not fail the official Insights collector.

G. Scheduling
- Do not create an aggressive schedule.
- Initial target: 2 scans/day.
- If this repository does not own deployment cron configuration, document the recommended schedule rather than silently creating it.

H. Analytics integration
- Add a deterministic way to aggregate follow_count by matched post.
- Expose the metric separately as activity-based attribution, e.g. follows_from_post_activity.
- Preserve match_confidence in reports/queries.
- Do not overwrite official raw post snapshots.

I. Tests
Add tests for at minimum:
1. one unique normal-post snippet → high-confidence match
2. grouped notification `+20 others` → follow_count=21
3. duplicate notification → no double count
4. `Requested` row → ignored
5. repeated Hadith text → not falsely promoted to exact/high confidence
6. candidate published after notification → rejected
7. no candidate → unresolved/unknown
8. Activity/browser failure → official Insights path remains unaffected
9. fallback fingerprint deterministic
10. credentials/session data never written to repo/log fixtures

J. Verification
Before claiming completion:
- run the full relevant test suite
- run lint/type checks if this repo has them
- perform a read-only dry run against the existing authenticated browser session
- prove no account actions occurred
- prove repeated execution is idempotent
- prove existing Insights collector still passes its tests
- inspect git diff for secrets/session artifacts

DO NOT publish, like, reply, follow, unfollow or message during verification.

REPORT FORMAT

DONE
- files changed
- migration added
- collector/parser added
- matching logic added
- tests added

VERIFICATION
- test commands + results
- read-only browser dry-run result
- idempotency result
- existing Insights regression result
- secrets/git diff check

CURRENT DATA RESULT
- Activity rows discovered
- new rows stored
- duplicate rows ignored
- matched high / medium / low / unknown counts
- total attributed follows by confidence

RISKS / LIMITATIONS
- any Meta UI/internal-prop dependency
- unresolved/repeated-text cases
- session/login requirements

NEXT
- recommended scheduler command/config, but do not enable a new cron unless explicitly approved

STOP CONDITIONS
- If login/2FA/CAPTCHA/security challenge appears: STOP and report.
- If implementing this requires changing the official Insights collector's core behavior: STOP and explain first.
- If exact attribution cannot be proven for a repeated-text event: preserve uncertainty; do not guess.

Implement, verify, and report. Do not create or enable a production cron until I approve it.
```
