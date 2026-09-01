import csv
import io
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.db import db_session, init_schema
from app.students import (
    next_student_id,
    student_exists,
    list_students,
    get_student,
    find_by_mobile,
    mobile_in_use,
    normalize_mobile,
    update_student_name,
    update_student_mobile,
    replace_embeddings,
    delete_student,
)
from app.face import decode_image, validate_and_embed, detect_and_embed_all, embedding_to_blob
from app.config import ANGLE_LABELS, ANGLE_INSTRUCTIONS
from app.matching import match_embedding, reload_gallery
from app.attendance import mark_attendance
from app.report import parse_report_params, build_report, render_pdf

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="PSNA IT Department — SIP Attendance")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.on_event("startup")
def _startup():
    init_schema()
    reload_gallery()


@app.get("/")
def root():
    return RedirectResponse(url="/enroll")


@app.get("/enroll")
def enroll_page(request: Request):
    return templates.TemplateResponse(
        "enroll.html",
        {
            "request": request,
            "angle_labels": ANGLE_LABELS,
            "angle_instructions": ANGLE_INSTRUCTIONS,
        },
    )


@app.get("/api/students/lookup")
def lookup_student(mobile: str = ""):
    number = normalize_mobile(mobile)
    if number is None:
        return JSONResponse({"found": False, "reason": "Enter a valid 10-digit mobile number"})

    with db_session() as conn:
        match = find_by_mobile(conn, number)

    if match is None:
        return JSONResponse({"found": False})

    return JSONResponse(
        {
            "found": True,
            "student_id": match["student_id"],
            "name": match["name"],
            "has_photos": match["photo_count"] > 0,
        }
    )


@app.post("/api/enroll/validate-photo")
async def validate_photo(image: UploadFile = File(...)):
    image_bytes = await image.read()
    img = decode_image(image_bytes)
    result = validate_and_embed(img)
    return JSONResponse(result)


class PhotoIn(BaseModel):
    angle_label: str
    embedding: list[float]


class EnrollSubmitIn(BaseModel):
    name: str
    mobile_number: str
    photos: list[PhotoIn]


@app.post("/api/enroll/submit")
def enroll_submit(payload: EnrollSubmitIn):
    """New-registration path: mobile number didn't match anyone in the
    pre-loaded roster, so a brand-new student row is created here (walk-in,
    or not yet in the CSV). student_id is always system-assigned, never
    typed."""
    name = payload.name.strip()
    mobile_number = normalize_mobile(payload.mobile_number)

    if not name:
        return JSONResponse({"ok": False, "reason": "Name is required"}, status_code=400)
    if mobile_number is None:
        return JSONResponse({"ok": False, "reason": "Enter a valid 10-digit mobile number"}, status_code=400)

    if len(payload.photos) < len(ANGLE_LABELS):
        return JSONResponse(
            {"ok": False, "reason": f"Need {len(ANGLE_LABELS)} validated photos, got {len(payload.photos)}"},
            status_code=400,
        )

    with db_session() as conn:
        if mobile_in_use(conn, mobile_number):
            return JSONResponse(
                {"ok": False, "reason": f"{mobile_number} is already enrolled — look them up instead of adding new"},
                status_code=409,
            )

        student_id = next_student_id(conn)
        conn.execute(
            "INSERT INTO students (student_id, name, mobile_number) VALUES (?, ?, ?)",
            (student_id, name, mobile_number),
        )
        for photo in payload.photos:
            conn.execute(
                "INSERT INTO embeddings (student_id, embedding, angle_label) VALUES (?, ?, ?)",
                (student_id, embedding_to_blob(photo.embedding), photo.angle_label),
            )

    reload_gallery()
    return JSONResponse({"ok": True, "student_id": student_id})


@app.get("/students")
def students_page(request: Request):
    with db_session() as conn:
        students = list_students(conn)
    return templates.TemplateResponse("students.html", {"request": request, "students": students})


@app.get("/students/{student_id}/edit")
def edit_student_page(request: Request, student_id: str):
    student_id = student_id.strip().upper()
    with db_session() as conn:
        student = get_student(conn, student_id)
        if student is None:
            raise HTTPException(status_code=404, detail=f"Student {student_id} not found")
    return templates.TemplateResponse(
        "edit_student.html",
        {
            "request": request,
            "student": student,
            "angle_labels": ANGLE_LABELS,
            "angle_instructions": ANGLE_INSTRUCTIONS,
        },
    )


class UpdateStudentIn(BaseModel):
    name: str
    mobile_number: str = ""


@app.post("/api/students/{student_id}/update")
def update_student(student_id: str, payload: UpdateStudentIn):
    student_id = student_id.strip().upper()
    name = payload.name.strip()
    if not name:
        return JSONResponse({"ok": False, "reason": "Name is required"}, status_code=400)

    mobile_number = None
    if payload.mobile_number.strip():
        mobile_number = normalize_mobile(payload.mobile_number)
        if mobile_number is None:
            return JSONResponse({"ok": False, "reason": "Enter a valid 10-digit mobile number"}, status_code=400)

    with db_session() as conn:
        if not student_exists(conn, student_id):
            return JSONResponse({"ok": False, "reason": f"Student {student_id} not found"}, status_code=404)
        existing = find_by_mobile(conn, mobile_number) if mobile_number else None
        if existing and existing["student_id"] != student_id:
            return JSONResponse(
                {"ok": False, "reason": f"{mobile_number} is already used by {existing['student_id']}"},
                status_code=409,
            )
        update_student_name(conn, student_id, name)
        update_student_mobile(conn, student_id, mobile_number)

    return JSONResponse({"ok": True, "student_id": student_id, "name": name, "mobile_number": mobile_number})


@app.post("/api/students/{student_id}/delete")
def delete_student_endpoint(student_id: str):
    student_id = student_id.strip().upper()
    with db_session() as conn:
        if not student_exists(conn, student_id):
            return JSONResponse({"ok": False, "reason": f"Student {student_id} not found"}, status_code=404)
        delete_student(conn, student_id)

    reload_gallery()
    return JSONResponse({"ok": True, "student_id": student_id})


class RecaptureIn(BaseModel):
    photos: list[PhotoIn]


@app.post("/api/students/{student_id}/recapture")
def recapture_student(student_id: str, payload: RecaptureIn):
    student_id = student_id.strip().upper()

    if len(payload.photos) < len(ANGLE_LABELS):
        return JSONResponse(
            {"ok": False, "reason": f"Need {len(ANGLE_LABELS)} validated photos, got {len(payload.photos)}"},
            status_code=400,
        )

    with db_session() as conn:
        if not student_exists(conn, student_id):
            return JSONResponse({"ok": False, "reason": f"Student {student_id} not found"}, status_code=404)
        replace_embeddings(conn, student_id, [p.model_dump() for p in payload.photos])

    reload_gallery()
    return JSONResponse({"ok": True, "student_id": student_id})


@app.get("/scan")
def scan_page(request: Request):
    return templates.TemplateResponse("scan.html", {"request": request})


@app.post("/api/scan")
async def scan(image: UploadFile = File(...)):
    image_bytes = await image.read()
    img = decode_image(image_bytes)
    detections = detect_and_embed_all(img)

    faces = []
    for det in detections:
        match = match_embedding(det["embedding"])
        if match is None:
            faces.append({"bbox": det["bbox"], "status": "unknown"})
            continue

        att = mark_attendance(match["student_id"])
        faces.append(
            {
                "bbox": det["bbox"],
                "status": "match",
                "student_id": match["student_id"],
                "name": match["name"],
                "session": att["session"],
                "already_marked": att["already"],
            }
        )

    return JSONResponse({"faces": faces})


@app.get("/report")
def report_page(request: Request):
    with db_session() as conn:
        date_from, date_to, session_filter, status_filter, q = parse_report_params(request.query_params)
        data = build_report(conn, date_from, date_to, session_filter, status_filter, q)
    return templates.TemplateResponse("report.html", {"request": request, **data})


@app.get("/report/export.csv")
def report_export_csv(request: Request):
    with db_session() as conn:
        date_from, date_to, session_filter, status_filter, q = parse_report_params(request.query_params)
        data = build_report(conn, date_from, date_to, session_filter, status_filter, q)

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["Session", "Total", "Present", "Absent"])
    for s in data["session_summary"]:
        label = s["session"].capitalize()
        if data["date_from"] != data["date_to"]:
            label += f" ({s['date']})"
        writer.writerow([label, s["total"], s["present"], s["absent"]])
    writer.writerow([])

    same_day = data["date_from"] == data["date_to"]
    col_label = (lambda d, s: s.capitalize()) if same_day else (lambda d, s: f"{d} {s.capitalize()}")
    header = ["Student ID", "Name", "Mobile Number"] + [col_label(d, s) for d, s in data["columns"]]
    writer.writerow(header)
    for stu in data["students"]:
        row = [stu["student_id"], stu["name"], stu["mobile_number"] or ""]
        row += ["Present" if stu["cells"][col] else "Absent" for col in data["columns"]]
        writer.writerow(row)

    filename = f"attendance_{data['date_from']}_to_{data['date_to']}_{data['session_filter']}_{data['status_filter']}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/report/export.pdf")
def report_export_pdf(request: Request):
    with db_session() as conn:
        date_from, date_to, session_filter, status_filter, q = parse_report_params(request.query_params)
        data = build_report(conn, date_from, date_to, session_filter, status_filter, q)

    pdf_bytes = render_pdf(data)
    filename = f"attendance_{data['date_from']}_to_{data['date_to']}_{data['session_filter']}_{data['status_filter']}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
