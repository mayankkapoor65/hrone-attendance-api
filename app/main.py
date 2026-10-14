"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, date as dt_date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent index creation on startup
    # 1. employees indexes
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # 2. attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds."""
    return dt.replace(microsecond=0)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC/aware datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert integer epoch milliseconds to UTC datetime truncated to seconds."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shifts & dates in IST (UTC+05:30).
    For overnight shifts (shift_end <= shift_start): punch-in whose IST time
    is earlier than shift_end belongs to the shift that started the previous day.
    """
    ist_dt = punch_in_dt.astimezone(IST).replace(microsecond=0)
    sh_start_h, sh_start_m = map(int, shift_start.split(":"))
    sh_end_h, sh_end_m = map(int, shift_end.split(":"))

    is_overnight = (sh_end_h, sh_end_m) <= (sh_start_h, sh_start_m)
    if is_overnight:
        punch_time = (ist_dt.hour, ist_dt.minute, ist_dt.second)
        shift_end_time = (sh_end_h, sh_end_m, 0)
        if punch_time < shift_end_time:
            return (ist_dt.date() - timedelta(days=1)).isoformat()
    return ist_dt.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly > 10:00 (600s) after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start.
    """
    ist_dt = punch_in_dt.astimezone(IST).replace(microsecond=0)
    h, m = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(attendance_date)
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, h, m, 0, tzinfo=IST)
    diff_seconds = (ist_dt - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals HALF-UP.
    """
    in_dt = punch_in_dt.replace(microsecond=0)
    out_dt = punch_out_dt.replace(microsecond=0)
    diff_seconds = Decimal(str(int((out_dt - in_dt).total_seconds())))
    hours = diff_seconds / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """
    R3: Whole minutes (floor) between shift_end and punch_out, counted only when >= 30, else 0.
    For overnight shifts, shift_end falls on attendance_date + 1 day.
    """
    ist_out = punch_out_dt.astimezone(IST).replace(microsecond=0)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date)

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight:
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_dt = datetime(
        shift_end_date.year, shift_end_date.month, shift_end_date.day, end_h, end_m, 0, tzinfo=IST
    )
    diff_seconds = (ist_out - shift_end_dt).total_seconds()
    if diff_seconds >= 1800:
        return int(diff_seconds // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: True when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_working_days(joined_on_str: str, year: int, month: int, last_day: int) -> int:
    """
    R7: Mon-Fri only, counted from joined_on for mid-month joiners.
    """
    joined_date = date.fromisoformat(joined_on_str)
    month_start_date = date(year, month, 1)
    month_end_date = date(year, month, last_day)

    if joined_date > month_end_date:
        return 0

    start_date = max(month_start_date, joined_date)
    curr = start_date
    working_days = 0
    while curr <= month_end_date:
        if curr.weekday() < 5:  # Monday is 0, Friday is 4
            working_days += 1
        curr += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Response Serialization
# --------------------------------------------------------------------------- #
def doc_to_employee(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_ms = int(created_at.timestamp() * 1000)
    elif isinstance(created_at, (int, float)):
        created_at_ms = int(created_at)
    else:
        created_at_ms = 0

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def format_change_val(field: str, val: Any) -> Any:
    if val is None:
        return None
    if field in ("punch_in", "punch_out"):
        return dt_to_epoch_ms(val) if isinstance(val, datetime) else val
    return val


def doc_to_attendance(doc: dict) -> dict:
    history = []
    for h in doc.get("history", []):
        serialized_changes = {}
        for f, ch in h.get("changes", {}).items():
            serialized_changes[f] = {
                "from": format_change_val(f, ch.get("from")),
                "to": format_change_val(f, ch.get("to")),
            }
        history.append({
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": serialized_changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_model(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return doc_to_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [doc_to_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(microsecond=0)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return doc_to_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        out_dt = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(microsecond=0)
    else:
        out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in is at or before out_dt
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": out_dt, "$ne": None},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    in_dt = record["punch_in"]
    if in_dt.tzinfo is None:
        in_dt = in_dt.replace(tzinfo=timezone.utc)
    in_dt = in_dt.replace(microsecond=0)

    elapsed_seconds = (out_dt - in_dt).total_seconds()
    if elapsed_seconds <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")
    if elapsed_seconds > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(in_dt, out_dt)
    overtime_mins = compute_overtime(out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)
    late_mins = record.get("late_minutes")
    if late_mins is None:
        late_mins = compute_late_minutes(in_dt, emp["shift_start"], record["date"])

    # Race-condition safe atomic update
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
                "late_minutes": late_mins,
            }
        },
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    updated_doc = db.attendance_logs.find_one({"_id": record["_id"]})
    return doc_to_attendance(updated_doc)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    try:
        dt_date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    old_status = record["status"]
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None:
        if old_punch_in.tzinfo is None:
            old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
        old_punch_in = old_punch_in.replace(microsecond=0)

    old_punch_out = record.get("punch_out")
    if old_punch_out is not None:
        if old_punch_out.tzinfo is None:
            old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)
        old_punch_out = old_punch_out.replace(microsecond=0)

    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    target_status = body.status if body.status is not None else old_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when setting status to ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = old_punch_in

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {calc_date}, but record date is {date}",
            )

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = old_punch_out

        if new_punch_out is not None:
            diff = (new_punch_out - new_punch_in).total_seconds()
            if diff <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if diff > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)

    changes = {}
    if target_status != old_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = record.get("history", [])
    if not current_history:
        history_condition = {
            "$or": [
                {"history": {"$exists": False}},
                {"history": []},
                {"history": {"$size": 0}},
            ]
        }
    else:
        history_condition = {"history": {"$size": len(current_history)}}

    update_filter = {
        "_id": record["_id"],
        "$and": [history_condition],
    }

    update_ops = {
        "$set": {
            "status": target_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {
            "history": history_entry,
        },
    }

    res = db.attendance_logs.update_one(update_filter, update_ops)
    if res.matched_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Conflict: record was modified concurrently by another request",
        )

    updated_doc = db.attendance_logs.find_one({"_id": record["_id"]})
    return doc_to_attendance(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        try:
            dt_date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from format")
    if date_to:
        try:
            dt_date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
    if status_filter and status_filter not in valid_statuses:
        raise HTTPException(status_code=422, detail="Invalid status")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [doc_to_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    working_days = compute_working_days(emp["joined_on"], year, m, last_day)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_stage = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$logs.date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    agg_res = list(db.employees.aggregate(pipeline))
    items = []
    for doc in agg_res:
        headcount = len(doc.get("employees", []))
        if headcount == 0:
            continue
        wh_count = doc.get("work_hours_count", 0)
        if wh_count > 0:
            wh_sum = Decimal(str(doc.get("work_hours_sum", 0.0)))
            avg_wh = wh_sum / Decimal(str(wh_count))
            avg_work_hours = float(avg_wh.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        else:
            avg_work_hours = None

        items.append({
            "department": doc["_id"],
            "headcount": headcount,
            "present_days": float(doc.get("present_days", 0.0)),
            "avg_work_hours": avg_work_hours,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    # Standard competition ranking per openapi.yaml line 315 ($rank)
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$denseRank": {}
                    }
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        from_d = date.fromisoformat(from_date)
        to_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="to date must be >= from date")

    if (to_d - from_d).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    from_dt = datetime(from_d.year, from_d.month, from_d.day, 0, 0, 0, tzinfo=timezone.utc)
    to_plus_one_dt = datetime(to_d.year, to_d.month, to_d.day, 0, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        # Match indexed date range and department employees
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "date": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Union with a synthetic root record at from_dt from employees to ensure densify never receives an empty stream
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date": from_date,
                            "date_dt": from_dt,
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date",
                "date_dt": {"$first": "$date_dt"},
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        # Gap-filling: densify day-by-day across the full range
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_plus_one_dt],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        # Lookup headcount for that day (R9: joined_on <= that day)
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "active_employees",
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$size": "$active_employees"},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": 1,
                "is_working_day": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        # 7-day moving average over preceding non-null attendance_rate rows
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date_dt": 1}},
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for doc in agg_res:
        rate = doc.get("attendance_rate")
        if rate is not None:
            rate = float(Decimal(str(rate)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        ma = doc.get("moving_avg_7d")
        if ma is not None:
            ma = float(Decimal(str(ma)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": float(doc["present_count"]),
            "late_count": int(doc["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": ma,
        })

    return {
        "department": department,
        "items": items,
    }


def find_in_plan(plan_node: dict, target_key: str, target_val: str) -> bool:
    if not isinstance(plan_node, dict):
        return False
    if plan_node.get(target_key) == target_val:
        return True
    for v in plan_node.values():
        if isinstance(v, dict):
            if find_in_plan(v, target_key, target_val):
                return True
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and find_in_plan(item, target_key, target_val):
                    return True
    return False


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
        )
        explain_res = cursor.explain("executionStats")
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {"dateString": "$date"}
                                                    }
                                                },
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        match_stage = {"joined_on": {"$lte": month_end}}
        if department is not None:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp_code"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$department",
                    "employees": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {"dateString": "$logs.date"}
                                                    }
                                                },
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "work_hours_sum": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                0.0,
                            ]
                        }
                    },
                    "work_hours_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$cond": [
                                {"$ne": ["$logs", None]},
                                {"$ifNull": ["$logs.late_minutes", 0]},
                                0,
                            ]
                        }
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$eq": ["$logs.status", "LEAVE"]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$logs", None]},
                                        {"$eq": ["$logs.status", "ON_DUTY"]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]
        if department is not None:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$denseRank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ])

        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422, detail="department, from, and to are required"
            )

        try:
            from_d = date.fromisoformat(from_date)
            to_d = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if to_d < from_d:
            raise HTTPException(status_code=422, detail="to date must be >= from date")

        if (to_d - from_d).days > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        from_dt = datetime(from_d.year, from_d.month, from_d.day, 0, 0, 0, tzinfo=timezone.utc)
        to_plus_one_dt = datetime(to_d.year, to_d.month, to_d.day, 0, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)

        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }
            },
            {"$project": {"date": "$_id", "date_dt": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
            {
                "$unionWith": {
                    "coll": "employees",
                    "pipeline": [
                        {"$match": {"department": department}},
                        {"$limit": 1},
                        {"$project": {"_id": 0, "date": from_date, "date_dt": from_dt, "present_count": {"$literal": 0.0}, "late_count": {"$literal": 0}}},
                    ],
                }
            },
            {"$group": {"_id": "$date", "date_dt": {"$first": "$date_dt"}, "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
            {"$densify": {"field": "date_dt", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_plus_one_dt]}}},
            {"$project": {"_id": 0, "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}}, "date_dt": 1, "present_count": {"$ifNull": ["$present_count", 0.0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"day_str": "$date"},
                    "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day_str"]}]}}}],
                    "as": "active_employees",
                }
            },
            {"$project": {"date": 1, "date_dt": 1, "present_count": 1, "late_count": 1, "headcount": {"$size": "$active_employees"}, "is_working_day": {"$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]}}},
            {"$project": {"date": 1, "date_dt": 1, "present_count": 1, "late_count": 1, "headcount": 1, "is_working_day": 1, "attendance_rate": {"$cond": [{"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
            {"$setWindowFields": {"sortBy": {"date_dt": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
            {"$sort": {"date_dt": 1}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    # Analyze executionStats
    stats = explain_res.get("executionStats", {})
    query_planner = explain_res.get("queryPlanner", {})
    winning_stage = (
        query_planner.get("winningPlan", {}).get("stage")
        or stats.get("executionStages", {}).get("stage")
        or "AGGREGATE"
    )

    has_ixscan = find_in_plan(explain_res, "stage", "IXSCAN")
    has_collscan = find_in_plan(explain_res, "stage", "COLLSCAN")

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_res,
        "winning_stage": winning_stage,
        "has_ixscan": has_ixscan,
        "has_collscan": has_collscan,
        "totalKeysExamined": stats.get("totalKeysExamined", 0),
        "totalDocsExamined": stats.get("totalDocsExamined", 0),
        "nReturned": stats.get("nReturned", 0),
        "executionTimeMillis": stats.get("executionTimeMillis", 0),
    }
