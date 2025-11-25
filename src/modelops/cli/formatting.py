"""Date and time formatting utilities for CLI display.

Handles timezone conversion and human-friendly date formatting.
"""

import time
from datetime import UTC, datetime, timedelta


def format_timestamp(iso_timestamp: str, use_local_tz: bool = True) -> str:
    """Format an ISO timestamp for display.

    Args:
        iso_timestamp: ISO format timestamp string
        use_local_tz: If True, convert to local timezone

    Returns:
        Formatted timestamp string
    """
    dt = datetime.fromisoformat(iso_timestamp)

    # Ensure we have timezone info (assume UTC if not)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    # Convert to local timezone if requested
    if use_local_tz:
        dt = dt.astimezone()

    now = datetime.now(dt.tzinfo)

    # Use relative time for recent events
    delta = now - dt

    # Less than 1 minute
    if delta < timedelta(minutes=1):
        return "just now"

    # Less than 1 hour
    if delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    # Less than 24 hours - show relative hours
    if delta < timedelta(days=1) and dt.date() == now.date():
        hours = int(delta.total_seconds() / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"

    # Less than 7 days
    if delta < timedelta(days=7):
        days = int(delta.total_seconds() / 86400)
        if days == 1:
            return "yesterday " + dt.strftime("%H:%M")
        return f"{days} days ago"

    # Older - show full date
    if dt.year == now.year:
        return dt.strftime("%b %d %H:%M")  # Mar 15 14:30
    else:
        return dt.strftime("%Y-%m-%d %H:%M")  # 2024-03-15 14:30


def format_duration(start_iso: str, end_iso: str | None = None) -> str:
    """Format duration between two timestamps.

    Args:
        start_iso: Start timestamp in ISO format
        end_iso: End timestamp in ISO format (or None for ongoing)

    Returns:
        Human-readable duration string
    """
    start = datetime.fromisoformat(start_iso)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)

    if end_iso:
        end = datetime.fromisoformat(end_iso)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
    else:
        # Use current time if ongoing
        end = datetime.now(UTC)

    duration = end - start

    # Format based on duration length
    total_seconds = int(duration.total_seconds())

    if total_seconds < 60:
        return f"{total_seconds}s"

    minutes = total_seconds // 60
    if minutes < 60:
        seconds = total_seconds % 60
        if seconds > 0:
            return f"{minutes}m {seconds}s"
        return f"{minutes}m"

    hours = minutes // 60
    if hours < 24:
        remaining_minutes = minutes % 60
        if remaining_minutes > 0:
            return f"{hours}h {remaining_minutes}m"
        return f"{hours}h"

    days = hours // 24
    remaining_hours = hours % 24
    if remaining_hours > 0:
        return f"{days}d {remaining_hours}h"
    return f"{days}d"


def get_timezone_info() -> str:
    """Get current timezone information for display.

    Returns:
        Timezone string like "PST" or "UTC-8"
    """
    # Get local timezone offset
    local_tz = datetime.now().astimezone().tzinfo

    # Try to get timezone abbreviation
    tz_name = time.tzname[time.daylight]

    # Get offset
    offset = datetime.now().astimezone().strftime("%z")
    if offset:
        hours = int(offset[:3])
        tz_offset = f"UTC{hours:+d}"
    else:
        tz_offset = "UTC"

    # Return abbreviation if available, otherwise offset
    if tz_name and tz_name not in ["UTC", "GMT"]:
        return tz_name
    return tz_offset
