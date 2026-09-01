import io
from datetime import datetime, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

from app.attendance import IST

VALID_SESSIONS = ("all", "morning", "afternoon")
VALID_STATUSES = ("all", "absent", "present")


def get_default_date_range() -> tuple[str, str]:
    """Report defaults to today only (IST, matching the session-date the
    scanner writes) — not the full attendance history — so opening the page
    doesn't dump every day ever recorded."""
    today = datetime.now(IST).date().isoformat()
    return today, today


def parse_report_params(query_params) -> tuple[str, str, str, str, str]:
    default_from, default_to = get_default_date_range()

    date_from = query_params.get("from") or default_from
    date_to = query_params.get("to") or default_to
    try:
        datetime.strptime(date_from, "%Y-%m-%d")
        datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError:
        date_from, date_to = default_from, default_to

    session_filter = query_params.get("session", "all")
    if session_filter not in VALID_SESSIONS:
        session_filter = "all"

    status_filter = query_params.get("status", "all")
    if status_filter not in VALID_STATUSES:
        status_filter = "all"

    q = (query_params.get("q") or "").strip()

    return date_from, date_to, session_filter, status_filter, q


def build_report(conn, date_from: str, date_to: str, session_filter: str, status_filter: str, q: str) -> dict:
    session_list = ["morning", "afternoon"] if session_filter == "all" else [session_filter]

    d0 = datetime.strptime(date_from, "%Y-%m-%d").date()
    d1 = datetime.strptime(date_to, "%Y-%m-%d").date()
    dates = []
    d = d0
    while d <= d1:
        dates.append(d.isoformat())
        d += timedelta(days=1)

    columns = [(d, s) for d in dates for s in session_list]

    # Only students who've actually been through face capture count toward
    # attendance — someone still sitting in the "pending capture" queue has
    # no way to ever be marked present, so including them would just pad
    # every session out with false absences.
    total_enrolled = conn.execute(
        """
        SELECT COUNT(*) AS c FROM (
            SELECT s.student_id
            FROM students s
            JOIN embeddings e ON e.student_id = s.student_id
            GROUP BY s.student_id
            HAVING COUNT(e.id) >= 5
        )
        """
    ).fetchone()["c"]

    if q:
        like = f"%{q}%"
        rows = conn.execute(
            """
            SELECT s.student_id, s.name, s.mobile_number
            FROM students s
            JOIN embeddings e ON e.student_id = s.student_id
            WHERE s.name LIKE ? OR s.student_id LIKE ? OR s.mobile_number LIKE ?
            GROUP BY s.student_id
            HAVING COUNT(e.id) >= 5
            ORDER BY s.student_id
            """,
            (like, like, like),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.student_id, s.name, s.mobile_number
            FROM students s
            JOIN embeddings e ON e.student_id = s.student_id
            GROUP BY s.student_id
            HAVING COUNT(e.id) >= 5
            ORDER BY s.student_id
            """
        ).fetchall()

    att_rows = conn.execute(
        "SELECT student_id, session_date, session FROM attendance WHERE session_date BETWEEN ? AND ?",
        (date_from, date_to),
    ).fetchall()
    att_set = {(r["student_id"], r["session_date"], r["session"]) for r in att_rows}

    # Headcount for the present/absent summary uses total_enrolled (students
    # who've actually completed face capture), same denominator as the per-row
    # % column above. A student still pending capture has no way to ever be
    # marked present, so counting them toward "Total" would inflate absences
    # with people who were never scannable that session in the first place.
    present_by_col: dict[tuple[str, str], int] = {}
    for r in att_rows:
        key = (r["session_date"], r["session"])
        present_by_col[key] = present_by_col.get(key, 0) + 1

    session_summary = [
        {
            "date": d,
            "session": s,
            "total": total_enrolled,
            "present": present_by_col.get((d, s), 0),
            "absent": max(0, total_enrolled - present_by_col.get((d, s), 0)),
        }
        for d, s in columns
    ]

    total = len(columns)
    students = []
    for r in rows:
        sid, name, mobile_number = r["student_id"], r["name"], r["mobile_number"]
        cells = {}
        present = 0
        for col in columns:
            ok = (sid, col[0], col[1]) in att_set
            cells[col] = ok
            if ok:
                present += 1
        pct = (present / total * 100) if total else 0.0
        students.append(
            {
                "student_id": sid,
                "name": name,
                "mobile_number": mobile_number,
                "cells": cells,
                "present": present,
                "total": total,
                "pct": pct,
            }
        )

    if status_filter == "absent":
        # anyone who missed at least one session in the filtered range
        students = [s for s in students if s["present"] < s["total"]]
    elif status_filter == "present":
        # only students with a clean record across the filtered range
        students = [s for s in students if s["total"] > 0 and s["present"] == s["total"]]

    return {
        "columns": columns,
        "students": students,
        "total_enrolled": total_enrolled,
        "session_summary": session_summary,
        "date_from": date_from,
        "date_to": date_to,
        "session_filter": session_filter,
        "status_filter": status_filter,
        "q": q,
    }


def render_pdf(data: dict) -> bytes:
    buf = io.BytesIO()
    page_size = landscape(A4)
    doc = SimpleDocTemplate(
        buf, pagesize=page_size, leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm, bottomMargin=10 * mm
    )
    styles = getSampleStyleSheet()
    elements = []

    title = f"SIP Attendance Report — {data['date_from']} to {data['date_to']} — {data['session_filter'].capitalize()}"
    if data.get("status_filter", "all") != "all":
        title += f" — {data['status_filter'].capitalize()} only"
    if data["q"]:
        title += f" — search: \"{data['q']}\""
    elements.append(Paragraph(title, styles["Heading3"]))
    elements.append(Paragraph(f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]))
    elements.append(Spacer(1, 10))

    summary_header = ["Session", "Total", "Present", "Absent"]
    summary_data = [summary_header]
    for s in data["session_summary"]:
        label = s["session"].capitalize()
        if data["date_from"] != data["date_to"]:
            label += f" ({s['date']})"
        summary_data.append([label, s["total"], s["present"], s["absent"]])

    summary_table = Table(summary_data, colWidths=[50 * mm, 25 * mm, 25 * mm, 25 * mm])
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171a21")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
            ]
        )
    )
    elements.append(summary_table)
    elements.append(Spacer(1, 12))

    same_day = data["date_from"] == data["date_to"]
    col_label = (lambda d, s: s.capitalize()) if same_day else (lambda d, s: f"{d} {s.capitalize()}")

    header = ["ID", "Name", "Mobile"] + [col_label(d, s) for d, s in data["columns"]]
    table_data = [header]
    for stu in data["students"]:
        row = [stu["student_id"], stu["name"], stu["mobile_number"] or "—"]
        for col in data["columns"]:
            row.append("Present" if stu["cells"][col] else "Absent")
        table_data.append(row)

    avail_width = page_size[0] - 20 * mm
    id_w, name_w, mobile_w = 16 * mm, 28 * mm, 22 * mm
    remaining = max(0, avail_width - id_w - name_w - mobile_w)
    col_w = max(16 * mm, remaining / max(1, len(data["columns"])))
    col_widths = [id_w, name_w, mobile_w] + [col_w] * len(data["columns"])

    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171a21")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("ALIGN", (2, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
            ]
        )
    )
    elements.append(t)
    doc.build(elements)
    return buf.getvalue()
