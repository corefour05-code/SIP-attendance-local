import io
from datetime import date, datetime, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

VALID_SESSIONS = ("all", "morning", "afternoon")
VALID_STATUSES = ("all", "absent", "present")


def get_default_date_range(conn) -> tuple[str, str]:
    row = conn.execute(
        "SELECT MIN(session_date) AS mn, MAX(session_date) AS mx FROM attendance"
    ).fetchone()
    if row["mn"] and row["mx"]:
        return row["mn"], row["mx"]
    today = date.today().isoformat()
    return today, today


def parse_report_params(query_params, conn) -> tuple[str, str, str, str, str]:
    default_from, default_to = get_default_date_range(conn)

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
    elements.append(Spacer(1, 8))

    header = ["ID", "Name", "Mobile"] + [f"{d[5:]} {s[:1].upper()}" for d, s in data["columns"]] + ["%"]
    table_data = [header]
    for stu in data["students"]:
        row = [stu["student_id"], stu["name"], stu["mobile_number"] or "—"]
        for col in data["columns"]:
            row.append("P" if stu["cells"][col] else "A")
        row.append(f"{stu['pct']:.0f}")
        table_data.append(row)

    avail_width = page_size[0] - 20 * mm
    id_w, name_w, mobile_w, pct_w = 16 * mm, 28 * mm, 22 * mm, 12 * mm
    remaining = max(0, avail_width - id_w - name_w - mobile_w - pct_w)
    col_w = max(6 * mm, remaining / max(1, len(data["columns"])))
    col_widths = [id_w, name_w, mobile_w] + [col_w] * len(data["columns"]) + [pct_w]

    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171a21")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 6),
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
