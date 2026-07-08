/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import { llmDateBucket } from "../src/utils/llm_date_bucket";

const { DateTime } = luxon;

/**
 * P-UX — unit tests for the date-bucket grouping helper (sidebar "Today" /
 * "Yesterday" / "This Week" / "Older" sections). Pure function (no OWL, no
 * services) so it can be tested directly.
 *
 * Bucketing is calendar-day based in the USER'S local timezone (the server
 * ships UTC datetimes; ``deserializeDateTime`` converts to local). So:
 *  - string inputs use noon-UTC times, which stay in the same calendar day
 *    across any reasonable timezone offset (±a few hours never crosses
 *    midnight when the source is noon).
 *  - the calendar-day-vs-24h boundary is pinned with luxon DateTime inputs
 *    directly (no UTC→local conversion), so it is timezone-independent.
 */
describe("llm_date_bucket", () => {
  // Fixed "now": Wednesday 2026-07-08 12:00 noon LOCAL.
  const NOW = DateTime.local(2026, 7, 8, 12, 0, 0);

  test("today — same calendar day (noon UTC string, TZ-safe)", () => {
    expect(llmDateBucket("2026-07-08 12:00:00", NOW)).toBe("today");
  });

  test("yesterday — previous calendar day (noon UTC string, TZ-safe)", () => {
    expect(llmDateBucket("2026-07-07 12:00:00", NOW)).toBe("yesterday");
  });

  test("this_week — 2-6 days ago", () => {
    expect(llmDateBucket("2026-07-06 12:00:00", NOW)).toBe("this_week");
    expect(llmDateBucket("2026-07-03 12:00:00", NOW)).toBe("this_week");
  });

  test("older — 7+ days ago", () => {
    expect(llmDateBucket("2026-06-30 12:00:00", NOW)).toBe("older");
    expect(llmDateBucket("2025-01-01 12:00:00", NOW)).toBe("older");
  });

  test("falsy / invalid write_date falls back to older", () => {
    expect(llmDateBucket("", NOW)).toBe("older");
    expect(llmDateBucket(null, NOW)).toBe("older");
    expect(llmDateBucket("not a date", NOW)).toBe("older");
  });

  test("accepts a luxon DateTime directly", () => {
    expect(llmDateBucket(NOW, NOW)).toBe("today");
    expect(llmDateBucket(NOW.minus({ days: 1 }), NOW)).toBe("yesterday");
    expect(llmDateBucket(NOW.minus({ days: 30 }), NOW)).toBe("older");
  });

  test("calendar day, NOT a rolling 24h window (timezone-independent)", () => {
    // 23:59 yesterday → 00:01 today is only 2 minutes apart, but a
    // different calendar day → "yesterday". A 24h-window bucket would
    // wrongly call this "today". Luxon inputs bypass UTC→local conversion
    // so the assertion holds in every timezone.
    const justAfterMidnight = DateTime.local(2026, 7, 8, 0, 1, 0);
    const lateYesterday = DateTime.local(2026, 7, 7, 23, 59, 0);
    expect(llmDateBucket(lateYesterday, justAfterMidnight)).toBe("yesterday");
  });

  test("defaults to local now when omitted", () => {
    // A write_date far in the past is "older" regardless of the real now.
    expect(llmDateBucket("2000-01-01 12:00:00")).toBe("older");
    // A write_date 1 second in the future (tz skew) is "today".
    const soon = DateTime.local().plus({ seconds: 5 });
    expect(llmDateBucket(soon)).toBe("today");
  });
});
