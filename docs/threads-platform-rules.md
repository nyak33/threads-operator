# Threads Platform & Content Rules

**Last verified:** 2026-09-24  
**Re-verification cadence:** at least every 30 days, and immediately when Meta changes Threads publishing behavior or releases a relevant posting feature.

This file is the human-readable source of truth for content-generation rules used by Threads Operator. Numeric constraints enforced by code live in `src/threads_operator/content_rules.py`.

## Active publishing constraints

Threads Operator currently publishes through the standard Threads `TEXT` API path.

- **Standard root post:** maximum **500 characters**.
- **Each reply in a chain:** maximum **500 characters** independently.
- **Long-text attachments:** Threads supports a separate text-attachment feature of up to **10,000 characters**, but Threads Operator does **not** currently create text attachments. Do not treat 10,000 as the normal post/reply limit.
- **Topic:** use one meaningful content-specific `topic_tag` on the root post when a real topic can be determined.
- The operator validates the whole root + reply chain before publishing so an oversized later reply cannot leave a partially published thread.

The code uses Python Unicode character counting (`len(text)`) as a conservative preflight guard. Meta remains the final authority on accepted payloads.

## Thread-chain writing rule

When content includes replies, do not write the root post as if it were the whole article.

1. The root must contain the strongest hook, tension, pain point, question, or incomplete insight.
2. It should give enough context to be useful, but preserve a genuine reason to open the replies.
3. Reply 1 should advance the idea immediately; do not waste it repeating the root.
4. Each later reply should add a new example, explanation, proof point, contrast, or practical takeaway.
5. The final reply may close the loop and use the appropriate CTA.
6. Keep every root/reply independently readable and within the active character limit.
7. Do not use misleading clickbait, fake suspense, or hide the only useful information purely to force engagement.

For market-testing content, prefer curiosity and a concrete pain point over revealing the complete product mechanism or brand in the root when the brief intentionally calls for a tease.

## Generation / queue rule

Before `enqueue-draft`:

- Count every root and reply separately.
- Keep normal posts/replies at or below the active standard-post limit.
- If a chain is requested, optimize the root for the open-loop/hook and distribute the payoff across replies.
- Assign a meaningful topic instead of a generic placeholder when possible.
- If the draft depends on a newly released Threads feature, verify that Threads Operator actually supports that API capability before using it.

Threads Operator also validates these hard limits at queue ingress, immediately before a queued thread starts publishing, and again at the Threads API boundary.

## Staying current

Do **not** change limits based on memory, screenshots, creator posts, or a UI feature alone.

When this file is more than 30 days past `Last verified`, or when Meta announces a relevant change:

1. Verify the current behavior against official Meta sources.
2. Distinguish **standard post text** from separate features such as text attachments, polls, ghost posts, or media-specific capabilities.
3. Update this document, `content_rules.py`, affected generation guidance, and regression tests in the **same change set**.
4. Only relax a hard publishing guard after the corresponding API path is implemented and tested in this repo.

### Official sources currently used

- Meta Newsroom — Threads launch / standard post length:  
  https://about.fb.com/news/2023/07/introducing-threads-new-app-text-sharing/
- Meta Newsroom — long text attachments up to 10,000 characters:  
  https://about.fb.com/news/2025/09/attach-text-threads-posts-share-longer-perspectives/
- Meta's official Threads API collection on Postman — current publishing fields/capabilities (`text`, `reply_to_id`, `topic_tag`, `text_attachment`, etc.):  
  https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api

If sources disagree, use the official API behavior that Threads Operator can actually execute, and keep unsupported/UI-only capabilities documented separately.
