"""Time and date, read from the system clock.

Exists because "what time is it?" had no deterministic path: with no time
tool and no time in the system prompt, the model either guessed or reached
for web_search, which meant a Gemini attempt, a failure, a DuckDuckGo
fallback, and several seconds — to answer a question the machine already
knew the answer to.

Pure stdlib, no network, no API key, no cost. Sub-millisecond.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

# JARVIS's home timezone — Lee's. Overridable per call.
DEFAULT_TZ = "America/Chicago"

# Cities worth resolving without asking the user for an IANA string. Not
# meant to be exhaustive: an unmatched name falls back to a real IANA
# lookup, and only then to an honest "I don't know that timezone."
_CITY_TZ = {
    "dallas": "America/Chicago", "houston": "America/Chicago",
    "austin": "America/Chicago", "san antonio": "America/Chicago",
    "chicago": "America/Chicago", "new orleans": "America/Chicago",
    "new york": "America/New_York", "nyc": "America/New_York",
    "boston": "America/New_York", "atlanta": "America/New_York",
    "miami": "America/New_York", "washington": "America/New_York",
    "philadelphia": "America/New_York", "detroit": "America/Detroit",
    "denver": "America/Denver", "phoenix": "America/Phoenix",
    "salt lake city": "America/Denver",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "portland": "America/Los_Angeles", "san diego": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles",
    "anchorage": "America/Anchorage", "honolulu": "Pacific/Honolulu",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "mexico city": "America/Mexico_City",
    "london": "Europe/London", "dublin": "Europe/Dublin",
    "paris": "Europe/Paris", "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam", "zurich": "Europe/Zurich",
    "istanbul": "Europe/Istanbul", "moscow": "Europe/Moscow",
    "dubai": "Asia/Dubai", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong", "tokyo": "Asia/Tokyo",
    "seoul": "Asia/Seoul", "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai", "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne", "auckland": "Pacific/Auckland",
    "sao paulo": "America/Sao_Paulo", "buenos aires": "America/Argentina/Buenos_Aires",
    "johannesburg": "Africa/Johannesburg", "lagos": "Africa/Lagos",
    "cairo": "Africa/Cairo", "nairobi": "Africa/Nairobi",
    "utc": "UTC", "gmt": "UTC",
}


def resolve_timezone(name: str) -> str | None:
    """City name or IANA string -> IANA string. None if unrecognizable."""
    raw = (name or "").strip()
    if not raw:
        return DEFAULT_TZ

    key = raw.lower().replace("_", " ")
    if key in _CITY_TZ:
        return _CITY_TZ[key]

    # An exact IANA id, in whatever casing it arrived.
    try:
        zones = available_timezones()
    except Exception:
        zones = set()
    if raw in zones:
        return raw
    for z in zones:
        if z.lower() == raw.lower().replace(" ", "_"):
            return z

    # "Dallas, TX" / "Tokyo Japan" — try the leading token before punctuation.
    head = key.split(",")[0].strip()
    if head in _CITY_TZ:
        return _CITY_TZ[head]
    return None


def current_time(timezone: str = "") -> str:
    """A spoken-ready sentence, never a raw timestamp — this answer goes
    straight into a phone call as often as into a chat window."""
    tz_name = resolve_timezone(timezone)
    if tz_name is None:
        return (
            f"I don't recognize the timezone '{timezone}'. Give me a major city "
            "or an IANA zone like 'America/Chicago'."
        )
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception as e:
        return f"Couldn't read the clock for {tz_name}: {e}"

    # %-I/%-d are POSIX; Render is Linux, but fall back rather than crash.
    try:
        clock_str = now.strftime("%-I:%M %p").lower()
        date_str = now.strftime("%A, %B %-d, %Y")
    except ValueError:
        clock_str = now.strftime("%I:%M %p").lstrip("0").lower()
        date_str = now.strftime("%A, %B %d, %Y").replace(" 0", " ")

    where = "" if not (timezone or "").strip() else f" in {timezone.strip().title()}"
    return f"It's {clock_str}{where} on {date_str} ({now.strftime('%Z')})."


def today() -> str:
    """Date only — for a caller that wants the day without the clock."""
    now = datetime.now(ZoneInfo(DEFAULT_TZ))
    try:
        return now.strftime("%A, %B %-d, %Y")
    except ValueError:
        return now.strftime("%A, %B %d, %Y").replace(" 0", " ")
