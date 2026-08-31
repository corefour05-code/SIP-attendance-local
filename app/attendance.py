import sqlite3
from datetime import datetime, date, time as dtime, timezone, timedelta

from app.db import db_session

# Fixed offset, not a system/zoneinfo lookup — if this ever runs on a host
# (e.g. a cloud container) whose clock is UTC, the session cutoff must still
# follow India time, not the server's clock. India has no DST, so a fixed
# +5:30 offset is exact.
IST = timezone(timedelta(hours=5, minutes=30))
NOON = dtime(12, 0)


def current_session(now: datetime | None = None) -> tuple[date, str]:
    now = now.astimezone(IST) if now else datetime.now(IST)
    session = "morning" if now.time() < NOON else "afternoon"
    return now.date(), session


def mark_attendance(student_id: str, now: datetime | None = None) -> dict:
    session_date, session = current_session(now)
    with db_session() as conn:
        try:
            conn.execute(
                "INSERT INTO attendance (student_id, session_date, session) VALUES (?, ?, ?)",
                (student_id, session_date.isoformat(), session),
            )
            already = False
        except sqlite3.IntegrityError:
            already = True

    return {
        "already": already,
        "session": session,
        "session_date": session_date.isoformat(),
    }
