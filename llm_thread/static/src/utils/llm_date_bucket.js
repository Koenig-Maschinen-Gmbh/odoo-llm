/** @odoo-module **/

/**
 * P-UX: classify a thread's ``write_date`` into a date bucket
 * ("today" | "yesterday" | "this_week" | "older") for sidebar grouping.
 *
 * Pure function (no OWL, no services) so it can be unit-tested directly in
 * Hoot — mirrors the ``llm_message_classify`` util pattern. The sidebar
 * component imports + delegates to it.
 *
 * Why luxon (not ``new Date(str)``): the server ships datetimes as
 * "YYYY-MM-DD HH:MM:SS" — parsing of the space-separated form is
 * implementation-defined (Safari → ``Invalid Date``), and a rolling-24h
 * diff is NOT a calendar day. ``deserializeDateTime`` parses into the user's
 * timezone and ``startOf("day")`` gives true calendar-day buckets.
 */
import { deserializeDateTime } from "@web/core/l10n/dates";

const { DateTime } = luxon;

export const LLM_DATE_BUCKETS = ["today", "yesterday", "this_week", "older"];

/**
 * @param {string|luxon.DateTime|falsy} writeDate server datetime string or
 *        luxon DateTime (the mail store may hold either).
 * @param {luxon.DateTime} [now] override "now" (for tests); defaults to
 *        ``DateTime.local()``.
 * @returns {"today"|"yesterday"|"this_week"|"older"}
 */
export function llmDateBucket(writeDate, now) {
  if (!writeDate) {
    return "older";
  }
  const dt =
    writeDate instanceof DateTime ? writeDate : deserializeDateTime(writeDate);
  if (!dt || !dt.isValid) {
    return "older";
  }
  const day = dt.startOf("day");
  const today = (now instanceof DateTime ? now : DateTime.local()).startOf(
    "day"
  );
  const diffDays = today.diff(day, "days").days;
  if (diffDays <= 0) {
    return "today";
  }
  if (diffDays === 1) {
    return "yesterday";
  }
  if (diffDays < 7) {
    return "this_week";
  }
  return "older";
}
