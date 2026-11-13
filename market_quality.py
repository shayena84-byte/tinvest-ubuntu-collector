"""Shared, offline market identity and freshness checks. No network or writes."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def utc(value):
    if not isinstance(value, str):
        raise ValueError("timestamp_missing")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp_without_timezone")
    return stamp.astimezone(timezone.utc)


def age_seconds(value, now):
    return (now.astimezone(timezone.utc) - utc(value)).total_seconds()


def freshness(collected, book_time, now, max_snapshot_age=600, max_book_age=600):
    """Recalculate BOTH ages at the time of use, never reuse a stored age."""
    try:
        snapshot_age = age_seconds(collected, now)
        book_age = age_seconds(book_time, now)
    except (ValueError, TypeError, OverflowError):
        return False, "timestamp_unverified"
    if snapshot_age < 0 or book_age < 0:
        return False, "timestamp_in_future"
    if snapshot_age > max_snapshot_age:
        return False, "old_snapshot"
    if book_age > max_book_age:
        return False, "old_orderbook"
    return True, "ok"


def book_check(row, now, max_age=600, max_spread_pct=0.8):
    okay, reason = freshness(row.get("collected_utc"), row.get("book_time_utc"),
                             now, max_age, max_age)
    if not okay:
        return False, reason
    if row.get("quality_status") != "valid":
        return False, row.get("quality_reason") or "invalid_book"
    try:
        bid, ask = Decimal(str(row["best_bid"])), Decimal(str(row["best_ask"]))
        if not bid.is_finite() or not ask.is_finite() or not 0 < bid < ask:
            return False, "crossed_or_invalid_book"
        spread = (ask - bid) / ((ask + bid) / 2) * 100
        if spread > Decimal(str(max_spread_pct)):
            return False, "spread_too_wide"
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False, "invalid_book"
    return True, "ok"


def resolve_uid(db, symbol, board):
    """Resolve exactly one board + UID/ISIN/ticker. Ambiguity is an error."""
    columns = {r[1] for r in db.execute("PRAGMA table_info(instruments)")}
    predicates, values = [], [board]
    for name in ("uid", "isin", "ticker"):
        if name in columns:
            predicates.append(name + "=?")
            values.append(symbol)
    rows = db.execute("SELECT DISTINCT uid FROM instruments WHERE board=? AND (" +
                      " OR ".join(predicates) + ")", values).fetchall()
    if len(rows) > 1:
        raise ValueError("ambiguous_instrument")
    return rows[0][0] if rows else None
