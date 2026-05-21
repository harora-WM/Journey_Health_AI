#!/usr/bin/env python3
"""
Time Range Resolution Module

Converts natural language time expressions to exact UTC millisecond timestamps.

Hybrid architecture:
  1. Deterministic regex / rule-based parsing  (fast, high confidence)
  2. Claude Sonnet LLM fallback               (handles complex / ambiguous queries)
  3. Hard fallback                             (last 2 hours)

Index granularity rule:
  - duration <= 3 days  →  HOURLY
  - duration >  3 days  →  DAILY
"""

import os
import sys
import re
import json
import logging
import boto3
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Time-of-day periods: (start_hour_inclusive, end_hour_exclusive)
PERIOD_HOURS: dict[str, tuple[int, int]] = {
    "early morning": (4, 6),
    "morning":       (6, 12),
    "noon":          (12, 13),
    "lunch":         (12, 13),
    "afternoon":     (12, 17),
    "evening":       (17, 21),
    "night":         (21, 24),
    "tonight":       (21, 24),
    "midnight":      (0, 1),
}

# Canonical seconds-per-unit for relative window calculations
UNIT_SECONDS: dict[str, int] = {
    "second": 1,     "seconds": 1,     "sec": 1,    "secs": 1,
    "minute": 60,    "minutes": 60,    "min": 60,   "mins": 60,
    "hour":   3600,  "hours":   3600,  "hr":  3600, "hrs":  3600,
    "day":    86400,  "days":    86400,
    "week":   604800, "weeks":   604800,
    "month":  2592000, "months":  2592000,   # ~30 days
    "year":   31536000, "years":  31536000,  # ~365 days
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    """Current time with local timezone."""
    return datetime.now().astimezone()


def _today_start() -> datetime:
    """Today at 00:00:00 local time."""
    return _now().replace(hour=0, minute=0, second=0, microsecond=0)


def _to_ms(dt: datetime) -> int:
    """Convert a datetime to Unix milliseconds (int)."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1000)


def _duration_days(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 86400.0


def _normalize(query: str) -> str:
    """
    Lowercase and normalise synonym phrases so a single set of regex rules
    covers 'past', 'previous', 'prior', 'in the last', etc.
    """
    q = query.lower().strip()
    q = re.sub(r"\bpast\b",              "last", q)
    q = re.sub(r"\bprevious\b",          "last", q)
    q = re.sub(r"\bprev\b",              "last", q)
    q = re.sub(r"\bprior\b",             "last", q)
    q = re.sub(r"\bin\s+the\s+last\b",   "last", q)
    # keep colons (14:30), dots/dashes (date separators / decimals)
    q = re.sub(r"[^\w\s:.\-]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _parse_time_str(raw: str, base_date: datetime) -> Optional[datetime]:
    """
    Parse a short time token ('3pm', '15:00', 'morning') onto *base_date*.
    Returns a tz-aware datetime or None.
    """
    s = raw.strip().lower()

    # Named period → start hour of that period
    for period, (start_h, _) in PERIOD_HOURS.items():
        if s == period:
            return base_date.replace(hour=start_h, minute=0, second=0, microsecond=0)

    # HH:MM [am|pm]
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(am|pm)?", s)
    if m:
        h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == "pm" and h != 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        return base_date.replace(hour=h, minute=mi, second=0, microsecond=0)

    # H am|pm  (e.g. "3pm", "10 am")
    m = re.fullmatch(r"(\d{1,2})\s*(am|pm)", s)
    if m:
        h, ampm = int(m.group(1)), m.group(2)
        if ampm == "pm" and h != 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        return base_date.replace(hour=h, minute=0, second=0, microsecond=0)

    # dateutil last-resort
    try:
        from dateutil import parser as _dp
        dt = _dp.parse(s, default=base_date)
        if dt.hour != base_date.hour or dt.minute != base_date.minute:
            return dt.replace(second=0, microsecond=0, tzinfo=base_date.tzinfo)
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Deterministic parser
# ---------------------------------------------------------------------------

def _parse_deterministic(query: str) -> Optional[tuple[datetime, datetime]]:
    """
    Try every deterministic rule in priority order.
    Returns (start, end) datetimes, or None if nothing matched.
    """
    q     = _normalize(query)
    now   = _now()
    today = _today_start()

    # ── Special named ranges ────────────────────────────────────────────────

    # "right now" / "at the moment" / "currently" → last 1 hour
    if re.search(r"\b(right\s+now|at\s+(the\s+)?moment|currently|at\s+present|as\s+of\s+now)\b", q):
        return (now - timedelta(hours=1), now)

    if re.search(r"\bday before yesterday\b", q):
        return (today - timedelta(days=2),
                today - timedelta(days=1) - timedelta(seconds=1))

    if re.search(r"\blast\s+(working|business)\s+day\b", q):
        d = today - timedelta(days=1)
        while d.weekday() >= 5:          # skip Sat(5), Sun(6)
            d -= timedelta(days=1)
        return (d, d.replace(hour=23, minute=59, second=59))

    if re.search(r"\blast\s+night\b", q):
        yesterday = today - timedelta(days=1)
        return (yesterday.replace(hour=21, minute=0, second=0),
                today.replace(hour=0, minute=0, second=0) - timedelta(seconds=1))

    if re.search(r"\blast\s+weekend\b", q):
        days_back = today.weekday() + 2  # Mon→2, …, Sun→8
        last_sat  = today - timedelta(days=days_back)
        last_sun  = last_sat + timedelta(days=1)
        return (last_sat, last_sun.replace(hour=23, minute=59, second=59))

    if re.search(r"\b(this\s+)?weekend\b", q):
        days_ahead = (5 - today.weekday()) % 7
        sat = today + timedelta(days=days_ahead)
        sun = sat + timedelta(days=1)
        return (sat, sun.replace(hour=23, minute=59, second=59))

    if re.search(r"\b(start|beginning)\s+of\s+(the\s+)?week\b", q):
        monday = today - timedelta(days=today.weekday())
        return (monday, now)

    if re.search(r"\bend\s+of\s+(the\s+)?week\b", q):
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        return (monday, sunday.replace(hour=23, minute=59, second=59))

    # ── Explicit time ranges ─────────────────────────────────────────────────

    # "between TIME and TIME [today|yesterday]"
    m = re.search(r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:\s+(today|yesterday))?\s*$", q)
    if m:
        ref = today - timedelta(days=1) if m.group(3) == "yesterday" else today
        t1  = _parse_time_str(m.group(1).strip(), ref)
        t2  = _parse_time_str(m.group(2).strip(), ref)
        if t1 and t2:
            return (t1, t2)

    # "yesterday TIME to TIME"
    m = re.search(
        r"\byesterday\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+to\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
        q,
    )
    if m:
        ref = today - timedelta(days=1)
        t1  = _parse_time_str(m.group(1).strip(), ref)
        t2  = _parse_time_str(m.group(2).strip(), ref)
        if t1 and t2:
            return (t1, t2)

    # "[from] TIME to TIME [today|yesterday]"
    m = re.search(
        r"\b(?:from\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+to\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
        r"(?:\s+(today|yesterday))?\b",
        q,
    )
    if m:
        ref = today - timedelta(days=1) if m.group(3) == "yesterday" else today
        t1  = _parse_time_str(m.group(1).strip(), ref)
        t2  = _parse_time_str(m.group(2).strip(), ref)
        if t1 and t2:
            return (t1, t2)

    # "from DATESTR to DATESTR" — uses dateutil
    m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+?)(?:\s+\w+)?\s*$", q)
    if m:
        try:
            from dateutil import parser as _dp
            t1 = _dp.parse(m.group(1), default=today).astimezone()
            t2 = _dp.parse(m.group(2), default=today).astimezone()
            return (t1, t2)
        except Exception:
            pass

    # ── Relative time windows ────────────────────────────────────────────────

    # Fractional: "last half hour", "last half day", etc.
    m = re.search(
        r"\blast\s+half\s+(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)\b",
        q,
    )
    if m:
        total_secs = 0.5 * UNIT_SECONDS.get(m.group(1), 3600)
        return (now - timedelta(seconds=total_secs), now)

    # Compound: "last 2 hours 20 minutes", "last 1 day 5 hours", etc.
    m = re.search(
        r"\blast\s+((?:\d+(?:\.\d+)?\s*"
        r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)\s*)+)\b",
        q,
    )
    if m:
        total_secs = 0.0
        for pair in re.finditer(
            r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)",
            m.group(1),
        ):
            total_secs += float(pair.group(1)) * UNIT_SECONDS.get(pair.group(2), 3600)
        if total_secs > 0:
            return (now - timedelta(seconds=total_secs), now)

    # Bare singular: "last hour", "last minute", "last day"
    m = re.search(r"\blast\s+(minute|hour|day)\b", q)
    if m:
        return (now - timedelta(seconds=UNIT_SECONDS[m.group(1)]), now)

    # ── Named calendar periods ───────────────────────────────────────────────

    if re.search(r"\bthis\s+year\b", q):
        return (today.replace(month=1, day=1), now)

    if re.search(r"\blast\s+year\b", q):
        s = today.replace(year=today.year - 1, month=1, day=1)
        e = today.replace(month=1, day=1) - timedelta(seconds=1)
        return (s, e)

    if re.search(r"\bthis\s+month\b", q):
        return (today.replace(day=1), now)

    if re.search(r"\blast\s+month\b", q):
        first_this = today.replace(day=1)
        e = first_this - timedelta(seconds=1)
        s = e.replace(day=1, hour=0, minute=0, second=0)
        return (s, e)

    if re.search(r"\bthis\s+week\b", q):
        monday = today - timedelta(days=today.weekday())
        return (monday, now)

    if re.search(r"\blast\s+week\b", q):
        this_mon = today - timedelta(days=today.weekday())
        last_mon = this_mon - timedelta(weeks=1)
        last_sun = this_mon - timedelta(seconds=1)
        return (last_mon, last_sun)

    # "yesterday PERIOD" — must be checked before bare "yesterday"
    m = re.search(
        r"\byesterday\s+(early\s+morning|morning|afternoon|evening|night|tonight)\b", q
    )
    if m:
        start_h, end_h = PERIOD_HOURS.get(m.group(1), (0, 23))
        yesterday = today - timedelta(days=1)
        s = yesterday.replace(hour=start_h, minute=0, second=0)
        if end_h >= 24:
            e = today.replace(hour=0, minute=0, second=0) - timedelta(seconds=1)
        else:
            e = yesterday.replace(hour=end_h, minute=0, second=0)
        return (s, e)

    if re.search(r"\byesterday\b", q):
        return (today - timedelta(days=1), today - timedelta(seconds=1))

    # "[today|this] PERIOD" or bare period name
    m = re.search(
        r"\b(?:today\s+|this\s+)?(early\s+morning|morning|noon|lunch|afternoon|evening|night|tonight)\b",
        q,
    )
    if m:
        start_h, end_h = PERIOD_HOURS.get(m.group(1), (6, 12))
        s = today.replace(hour=start_h, minute=0, second=0)
        # Period hasn't started yet today — use yesterday's instead
        if s > now:
            yesterday = today - timedelta(days=1)
            s = yesterday.replace(hour=start_h, minute=0, second=0)
            if end_h >= 24:
                e = today.replace(hour=0, minute=0, second=0) - timedelta(seconds=1)
            else:
                e = yesterday.replace(hour=end_h, minute=0, second=0)
        else:
            if end_h >= 24:
                e = (today + timedelta(days=1)).replace(hour=0, minute=0, second=0) - timedelta(seconds=1)
            else:
                e = min(today.replace(hour=end_h, minute=0, second=0), now)
        return (s, e)

    if re.search(r"\btoday\b", q):
        return (today, now)

    # ── Single boundary ──────────────────────────────────────────────────────

    m = re.search(r"\b(?:since|after)\s+(.+?)\s*$", q)
    if m:
        ref_str = m.group(1).strip()
        if ref_str in PERIOD_HOURS:
            start_h, _ = PERIOD_HOURS[ref_str]
            s = today.replace(hour=start_h, minute=0, second=0)
            # Period hasn't happened yet today — use yesterday's
            if s > now:
                s = (today - timedelta(days=1)).replace(hour=start_h, minute=0, second=0)
            return (s, now)
        t = _parse_time_str(ref_str, today)
        if t:
            # Time hasn't happened yet today — use yesterday's
            if t > now:
                t = t - timedelta(days=1)
            return (t, now)
        try:
            from dateutil import parser as _dp
            t = _dp.parse(ref_str, default=today).astimezone()
            if t > now:
                # Month/date without an explicit year was defaulted to the current
                # year but that lands in the future — roll back one year.
                t = t.replace(year=t.year - 1)
            return (t, now)
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# LLM fallback — Claude Sonnet via Anthropic API
# ---------------------------------------------------------------------------

def _parse_with_llm(query: str) -> Optional[tuple[datetime, datetime]]:
    """
    Ask Claude Sonnet (via AWS Bedrock) to extract the time range.
    Uses the same boto3 client pattern as intent_classifier.py.
    Returns None if credentials are missing or the call fails.
    """
    if not config.AWS_ACCESS_KEY_ID:
        logger.debug("AWS credentials not set — skipping LLM fallback")
        return None

    now     = _now()
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    tz_name = now.tzname() or "local"

    system_prompt = (
        "You are a time-range extraction assistant for a service monitoring system.\n"
        "Given a natural language query, extract start_time and end_time.\n\n"
        "Rules:\n"
        "- Return ONLY a valid JSON object.\n"
        "- If the query contains an explicit or implicit time reference (e.g. \"last 2 hours\", \"yesterday\", \"this morning\", \"between 3pm and 5pm\"), return exactly two keys: start_time, end_time.\n"
        "- If the query has NO time reference at all (completely ambiguous about time), return exactly {\"ambiguous\": true}. Do NOT invent a default window.\n"
        "- Timestamps must be ISO 8601 UTC format: YYYY-MM-DDTHH:MM:SS\n"
        "- Period definitions: morning 06–12, afternoon 12–17, evening 17–21, night 21–24.\n"
        "- Output ONLY the JSON — no explanation, no markdown fences."
    )
    user_msg = (
        f"Current time ({tz_name}): {now_str}\n\n"
        f"Query: {query}\n\n"
        "Return the JSON time range:"
    )

    try:
        client = boto3.client(
            service_name="bedrock-runtime",
            region_name=config.AWS_REGION,
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        )
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 128,
            "temperature": 0.0,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_msg}],
        }
        response      = client.invoke_model(
            modelId=config.BEDROCK_MODEL_ID,
            body=json.dumps(request_body),
        )
        raw = json.loads(response["body"].read())["content"][0]["text"].strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            if data.get("ambiguous") is True:
                logger.info("LLM reported query as time-ambiguous; deferring to fallback")
                return None
            if "start_time" in data and "end_time" in data:
                fmt   = "%Y-%m-%dT%H:%M:%S"
                start = datetime.strptime(data["start_time"], fmt).replace(tzinfo=timezone.utc).astimezone()
                end   = datetime.strptime(data["end_time"],   fmt).replace(tzinfo=timezone.utc).astimezone()
                return (start, end)
        logger.warning("LLM returned unexpected format: %s", raw)
    except Exception as exc:
        logger.error("LLM time fallback failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Public interface — used by intent_classifier.py
# ---------------------------------------------------------------------------

class TimestampResolver:
    """
    Resolves a raw user query to UTC millisecond timestamps.

    Pipeline per query:
      1. Deterministic regex / rule-based parsing
      2. Claude Sonnet LLM fallback  (requires AWS_ACCESS_KEY_ID via boto3/Bedrock)
      3. Hard fallback: last 2 hours
    """

    def resolve_time_range(self, query: str) -> Dict[str, Any]:
        """
        Args:
            query: Raw user query, e.g. "show errors in the last 15 minutes"

        Returns:
            {
                'primary_range': {
                    'time_range':    str   (the input query),
                    'start_time':    int   (Unix ms),
                    'end_time':      int   (Unix ms),
                    'duration_days': float,
                },
                'source': str,
            }
        """
        now = _now()

        result = _parse_deterministic(query)
        if result:
            logger.info("Timestamp: deterministic parse succeeded")
            source = "deterministic"
        else:
            logger.info("Timestamp: deterministic parse failed, trying LLM fallback")
            result = _parse_with_llm(query)
            if result:
                source = "llm"
            else:
                source = "fallback"

        if result:
            start, end = result
        else:
            logger.info("Timestamp: LLM fallback unavailable, using default (last 2 hours)")
            start, end = now - timedelta(hours=2), now

        duration_days = _duration_days(start, end)

        return {
            'primary_range': {
                'time_range':    query,
                'start_time':    _to_ms(start),
                'end_time':      _to_ms(end),
                'duration_days': duration_days,
            },
            'source': source,
        }


# ---------------------------------------------------------------------------
# CLI / standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    _test_queries = [
        "show api failures in the last 15 minutes",
        "service performance yesterday evening",
        "show latency between 2pm and 4pm today",
        "how is my service performing today",
        "show errors from yesterday 3pm to 6pm",
        "service performance in the last 30 minutes",
        "since 5pm",
        "last night",
        "day before yesterday",
        "last working day",
        "last weekend",
        "start of the week",
        "how is my service performing",
        "performance since this morning",
        "api errors in the last 10 minutes",
        "show latency for the last hour",
        "service health yesterday",
        "past 24 hours",
        "this week",
        "last month",
        "past 7 days",
        "past 30 days",
    ]

    resolver  = TimestampResolver()
    queries   = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else _test_queries

    print("\n" + "=" * 70)
    print("TIMESTAMP RESOLVER TEST")
    print("=" * 70)

    for q in queries:
        result = resolver.resolve_time_range(q)
        pr = result['primary_range']
        start_dt = datetime.fromtimestamp(pr['start_time'] / 1000, tz=timezone.utc)
        end_dt   = datetime.fromtimestamp(pr['end_time']   / 1000, tz=timezone.utc)
        print(f"\nQuery      : {q}")
        print(f"start_time : {pr['start_time']}  ({start_dt.strftime('%Y-%m-%d %H:%M:%S UTC')})")
        print(f"end_time   : {pr['end_time']}  ({end_dt.strftime('%Y-%m-%d %H:%M:%S UTC')})")
        print(f"duration   : {pr['duration_days']:.3f} days")
        print("-" * 70)
