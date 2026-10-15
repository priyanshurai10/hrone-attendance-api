import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
import json

# Load environment variables (.env file won't override actual system env vars)
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# IST Timezone (UTC + 05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Timestamp bounds
MIN_TIMESTAMP_MS = 100_000_000_000
MAX_TIMESTAMP_MS = 4_102_444_800_000

# Regular expressions
EMP_CODE_REGEX = re.compile(r"^EMP\d{4,6}$")
TIME_HH_MM_REGEX = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DATE_YYYY_MM_REGEX = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
DATE_YYYY_MM_DD_REGEX = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")

# Database client and database handle
client: Optional[MongoClient] = None
db: Any = None

def get_db():
    global client, db
    if db is None:
        client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
        db = client[MONGO_DB]
    return db

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        database = get_db()
        # 1. Unique index on employees.emp_code
        database.employees.create_index([("emp_code", ASCENDING)], unique=True)
        # 2. Compound index on department and joined_on for department lookups
        database.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        # 3. Unique compound index on attendance_logs (emp_code, date)
        database.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        # 4. Compound index on attendance_logs (date DESC, emp_code ASC) for listing queries
        database.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    except Exception:
        # If MongoDB is unavailable at startup, do not crash; healthcheck will report 503
        pass
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="1.0.0", lifespan=lifespan)

def round_half_up(val: Optional[Union[float, int, Decimal]], places: int = 2) -> Optional[float]:
    """Rounds a numeric value using exact half-up rounding."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -places
    return float(d.quantize(target, rounding=ROUND_HALF_UP))

def validate_timestamp_ms(v: Any, allow_none: bool = False) -> Optional[int]:
    """Validates integer epoch milliseconds and truncates to whole seconds."""
    if v is None:
        if allow_none:
            return None
        raise ValueError("Timestamp cannot be null")
    if isinstance(v, bool):
        raise ValueError("Timestamp cannot be a boolean")
    if not isinstance(v, int):
        raise ValueError("Timestamp must be an integer")
    if v < MIN_TIMESTAMP_MS or v > MAX_TIMESTAMP_MS:
        raise ValueError(f"Timestamp outside allowed range ({MIN_TIMESTAMP_MS} to {MAX_TIMESTAMP_MS})")
    # Truncate to whole seconds
    return (v // 1000) * 1000

def ms_to_utc_datetime(ms: int) -> datetime:
    """Converts epoch ms to timezone-aware UTC datetime."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)

def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts BSON/Python datetime to epoch ms truncated to whole seconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (int(dt.timestamp()) // 1) * 1000

def get_ist_datetime(dt_utc: datetime) -> datetime:
    """Converts UTC datetime to IST datetime."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(IST)

def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Computes the attendance date string (YYYY-MM-DD) in IST.
    For overnight shifts (shift_end <= shift_start), punch-in whose IST time
    is strictly earlier than shift_end belongs to previous day's shift.
    """
    dt_ist = get_ist_datetime(punch_in_utc)
    punch_time_str = dt_ist.strftime("%H:%M")
    is_overnight = shift_end <= shift_start
    if is_overnight and punch_time_str < shift_end:
        prev_day = dt_ist.date() - timedelta(days=1)
        return prev_day.strftime("%Y-%m-%d")
    return dt_ist.date().strftime("%Y-%m-%d")

def compute_late_minutes(punch_in_utc: datetime, attendance_date_str: str, shift_start: str) -> int:
    """
    R2: Late minutes from shift start with a 10-minute grace period.
    Strictly greater than 10 minutes -> floor elapsed minutes since shift start. Otherwise 0.
    """
    y, m, d = map(int, attendance_date_str.split("-"))
    sh, sm = map(int, shift_start.split(":"))
    shift_start_ist = datetime(y, m, d, sh, sm, 0, tzinfo=IST)
    punch_in_ist = get_ist_datetime(punch_in_utc)

    diff_seconds = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0

def compute_overtime_minutes(punch_out_utc: datetime, attendance_date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R3: Overtime in whole minutes after applicable shift end.
    Threshold is at least 30 minutes; otherwise 0.
    Overnight shifts end on following calendar day.
    """
    y, m, d = map(int, attendance_date_str.split("-"))
    eh, em = map(int, shift_end.split(":"))
    shift_end_ist = datetime(y, m, d, eh, em, 0, tzinfo=IST)
    if shift_end <= shift_start:
        shift_end_ist += timedelta(days=1)

    punch_out_ist = get_ist_datetime(punch_out_utc)
    diff_seconds = (punch_out_ist - shift_end_ist).total_seconds()
    ot_minutes = int(diff_seconds // 60)
    if ot_minutes >= 30:
        return ot_minutes
    return 0

def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: Elapsed seconds between punch-in and punch-out divided by 3600,
    rounded to two decimal places using half-up rounding.
    """
    elapsed_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    raw_hours = Decimal(str(elapsed_seconds)) / Decimal("3600")
    return float(raw_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: half_day is true when work_hours < 4.50; otherwise false.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50

def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an attendance MongoDB document into the API contract response."""
    history_api = []
    for h in doc.get("history", []):
        at_dt = h.get("at")
        at_ms = datetime_to_ms(at_dt) if isinstance(at_dt, datetime) else at_dt
        changes_api = {}
        for k, v in h.get("changes", {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(f_val, datetime):
                    f_val = datetime_to_ms(f_val)
                if isinstance(t_val, datetime):
                    t_val = datetime_to_ms(t_val)
            changes_api[k] = {"from": f_val, "to": t_val}
        history_api.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_api
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_api
    }

def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an employee MongoDB document into the API contract response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_ms(doc["created_at"])
    }

# -------------------------------------------------------------------------
# Request Models
# -------------------------------------------------------------------------

class EmployeeCreateRequest(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        v = v.strip()
        if not EMP_CODE_REGEX.match(v):
            raise ValueError("emp_code must match EMP followed by 4 to 6 digits")
        return v

    @field_validator("name", "department")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Field must not be empty")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip()
        if not EMAIL_REGEX.match(v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v: str) -> str:
        v = v.strip()
        if not TIME_HH_MM_REGEX.match(v):
            raise ValueError("Shift time must be in HH:MM format (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        v = v.strip()
        if not DATE_YYYY_MM_DD_REGEX.match(v):
            raise ValueError("joined_on must be in YYYY-MM-DD format")
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Invalid calendar date for joined_on")
        return v

    @model_validator(mode="after")
    def validate_shifts_differ(self) -> "EmployeeCreateRequest":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: str = "PRESENT"

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        return v.strip()

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_timestamp_ms(v, allow_none=True)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        v = v.strip()
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("Punch-in status must be PRESENT, WFH, or ON_DUTY")
        return v

class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        return v.strip()

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_timestamp_ms(v, allow_none=True)

class AttendancePatchRequest(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason length must be between 5 and 200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 1 or len(v) > 50:
            raise ValueError("regularized_by length must be between 1 and 50 characters")
        return v

    @field_validator("status")
    @classmethod
    def validate_patch_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("Invalid status for regularization")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_punch_times(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        return validate_timestamp_ms(v, allow_none=True)

# -------------------------------------------------------------------------
# Endpoints
# -------------------------------------------------------------------------

@app.get("/health")
def health_check():
    """GET /health: Returns 200 when MongoDB is healthy, 503 when unavailable."""
    try:
        database = get_db()
        database.client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")

@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(req: EmployeeCreateRequest):
    """POST /employees: Creates a new employee document."""
    database = get_db()
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = {
        "emp_code": req.emp_code,
        "name": req.name,
        "email": req.email,
        "department": req.department,
        "shift_start": req.shift_start,
        "shift_end": req.shift_end,
        "joined_on": req.joined_on,
        "created_at": now_utc
    }
    try:
        database.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Employee code already exists")
    except PyMongoError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error")

    return format_employee_doc(doc)

@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """GET /employees: Lists employees with pagination and exact department filter."""
    database = get_db()
    query: Dict[str, Any] = {}
    if department is not None:
        query["department"] = department

    try:
        total = database.employees.count_documents(query)
        cursor = database.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
        items = [format_employee_doc(emp) for emp in cursor]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size
        }
    except PyMongoError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error")

@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(req: PunchInRequest):
    """POST /attendance/punch-in: Punches in an employee for attendance."""
    database = get_db()
    # Find employee
    emp = database.employees.find_one({"emp_code": req.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if req.punched_at is not None:
        punch_in_ms = req.punched_at
    else:
        now_ms = int(datetime.now(timezone.utc).timestamp()) * 1000
        punch_in_ms = (now_ms // 1000) * 1000

    punch_in_utc = ms_to_utc_datetime(punch_in_ms)

    # R1: Compute attendance date
    att_date = compute_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])

    # R2: Compute late minutes
    late_min = compute_late_minutes(punch_in_utc, att_date, emp["shift_start"])

    doc = {
        "emp_code": req.emp_code,
        "date": att_date,
        "status": req.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        database.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Attendance record already exists for this date")
    except PyMongoError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error")

    return format_attendance_doc(doc)

@app.post("/attendance/punch-out")
def punch_out(req: PunchOutRequest):
    """POST /attendance/punch-out: Punches out the most recent eligible open record."""
    database = get_db()
    emp = database.employees.find_one({"emp_code": req.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if req.punched_at is not None:
        punch_out_ms = req.punched_at
    else:
        now_ms = int(datetime.now(timezone.utc).timestamp()) * 1000
        punch_out_ms = (now_ms // 1000) * 1000

    punch_out_utc = ms_to_utc_datetime(punch_out_ms)

    # Find employee's most recent attendance record with a punch-in
    record = database.attendance_logs.find_one(
        {"emp_code": req.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)]
    )
    if not record or record.get("punch_in") is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No eligible punch-in record found")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # Validations: punch-out must be after punch-in and within 24 hours
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Punch-out must be after punch-in")

    duration_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if duration_sec > 24 * 3600:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Punch-out cannot be more than 24 hours after punch-in")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record already punched out")

    # Compute derived values
    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_min = compute_overtime_minutes(punch_out_utc, record["date"], emp["shift_start"], emp["shift_end"])
    is_half = compute_half_day(work_hrs)

    # Atomic update ensuring punch_out is still null
    updated = database.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record was concurrently punched out")

    return format_attendance_doc(updated)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """GET /attendance: Lists attendance logs with filters, database pagination, and sorting."""
    database = get_db()
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from cannot be greater than date_to")

    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status_filter:
        query["status"] = status_filter

    if date_from and date_to:
        query["date"] = {"$gte": date_from, "$lte": date_to}
    elif date_from:
        query["date"] = {"$gte": date_from}
    elif date_to:
        query["date"] = {"$lte": date_to}

    try:
        total = database.attendance_logs.count_documents(query)
        cursor = database.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
        items = [format_attendance_doc(log) for log in cursor]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size
        }
    except PyMongoError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error")

@app.patch("/attendance/{emp_code}/{att_date}")
def regularize_attendance(emp_code: str, att_date: str, req: AttendancePatchRequest):
    """PATCH /attendance/{emp_code}/{date}: Corrects attendance with audit history."""
    database = get_db()
    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = database.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    target_status = req.status if req.status is not None else record["status"]

    # ABSENT or LEAVE checks
    if target_status in ("ABSENT", "LEAVE"):
        if req.punch_in is not None or req.punch_out is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Punch times cannot be supplied for ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status
        if req.punch_in is not None:
            final_punch_in = ms_to_utc_datetime(req.punch_in)
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Presence status requires punch_in")

        if final_punch_in.tzinfo is None:
            final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        # Validate punch_in stays on record's attendance date under R1
        computed_date = compute_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != att_date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_in must belong to the record's attendance date")

        if req.punch_out is not None:
            final_punch_out = ms_to_utc_datetime(req.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        if final_punch_out is not None:
            if final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be after punch_in")
            duration_sec = (final_punch_out - final_punch_in).total_seconds()
            if duration_sec > 24 * 3600:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime_minutes(final_punch_out, att_date, emp["shift_start"], emp["shift_end"])
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, att_date, emp["shift_start"])

    # Determine what actually changed
    changes_stored: Dict[str, Any] = {}

    def check_diff(field_name: str, old_val: Any, new_val: Any):
        if old_val != new_val:
            changes_stored[field_name] = {"from": old_val, "to": new_val}

    check_diff("status", record.get("status"), target_status)
    check_diff("punch_in", record.get("punch_in"), final_punch_in)
    check_diff("punch_out", record.get("punch_out"), final_punch_out)
    check_diff("work_hours", record.get("work_hours"), final_work_hours)
    check_diff("late_minutes", record.get("late_minutes", 0), final_late_minutes)
    check_diff("overtime_minutes", record.get("overtime_minutes", 0), final_overtime_minutes)
    check_diff("half_day", record.get("half_day", False), final_half_day)

    if not changes_stored:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No changes detected")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": req.regularized_by,
        "reason": req.reason,
        "changes": changes_stored
    }

    # Atomic optimistic lock on history array length
    current_history = record.get("history", [])
    updated = database.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(current_history)}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated)

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str = Query(...)):
    """GET /analytics/employees/{emp_code}/monthly: Employee monthly attendance metrics."""
    database = get_db()
    if not DATE_YYYY_MM_REGEX.match(month):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month must be in YYYY-MM format")

    emp = database.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    first_month_date = date(y, m, 1)
    last_month_date = date(y, m, num_days)

    joined_on_date = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    start_date = max(first_month_date, joined_on_date)

    working_days = 0
    if start_date <= last_month_date:
        cur = start_date
        while cur <= last_month_date:
            if cur.weekday() < 5:  # Monday to Friday
                working_days += 1
            cur += timedelta(days=1)

    # Aggregation pipeline across attendance_logs
    month_regex = f"^{month}-"
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": month_regex}}},
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
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
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
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
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]

    agg_res = list(database.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_min = int(row.get("total_late_minutes", 0))
        total_ot_min = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_min = 0
        total_ot_min = 0

    if working_days > 0:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(raw_pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_min,
        "total_overtime_minutes": total_ot_min,
        "attendance_pct": attendance_pct
    }

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(...),
    department: Optional[str] = Query(None)
):
    """GET /analytics/departments/summary: Aggregates attendance metrics per department."""
    database = get_db()
    if not DATE_YYYY_MM_REGEX.match(month):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month must be in YYYY-MM format")

    y, m = map(int, month.split("-"))
    num_days = calendar.monthrange(y, m)[1]
    month_end_date_str = f"{month}-{num_days:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end_date_str}}
    if department is not None:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", month_end_date_str]}
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "flattened_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$flattened_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": ["$$l.day_of_week", [2, 3, 4, 5, 6]]}
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "qualifying_hours": {
                    "$filter": {
                        "input": "$flattened_logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]}
                            ]
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flattened_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]}
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$flattened_logs.late_minutes"
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flattened_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]}
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flattened_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$qualifying_hours"}, 0]},
                        {"$avg": "$qualifying_hours.work_hours"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]

    results = list(database.employees.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r.get("present_days", 0.0), 2),
            "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
            "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "leave_count": r.get("leave_count", 0),
            "on_duty_count": r.get("on_duty_count", 0)
        })

    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    """GET /analytics/leaderboard/late: Competition ranked leaderboard of late employees."""
    database = get_db()
    if not DATE_YYYY_MM_REGEX.match(month):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month must be in YYYY-MM format")

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$regex": f"^{month}-"},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"}
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}}
    ])

    results = list(database.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": results}

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    """GET /analytics/departments/{department}/trend: Daily attendance trend with moving average."""
    database = get_db()
    if not DATE_YYYY_MM_DD_REGEX.match(from_date) or not DATE_YYYY_MM_DD_REGEX.match(to_date):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="from and to must be in YYYY-MM-DD format")

    try:
        d_from = datetime.strptime(from_date, "%Y-%m-%d").date()
        d_to = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date in from/to")

    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to cannot be earlier than from")

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    # Check department existence
    dept_emp_count = database.employees.count_documents({"department": department})
    if dept_emp_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department has no employees")

    from_bson_date = datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0, tzinfo=timezone.utc)

    # MongoDB aggregation pipeline for daily series, gap-filling, headcount, and 7-day moving average
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "days": {
                    "$map": {
                        "input": {"$range": [0, num_days]},
                        "as": "offset",
                        "in": {
                            "date_obj": {
                                "$dateAdd": {
                                    "startDate": from_bson_date,
                                    "unit": "day",
                                    "amount": "$$offset"
                                }
                            }
                        }
                    }
                }
            }
        },
        {"$unwind": "$days"},
        {
            "$project": {
                "date_obj": "$days.date_obj",
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$days.date_obj"
                    }
                },
                "day_of_week": {
                    "$dayOfWeek": "$days.date_obj"
                }
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": ["$day_of_week", [2, 3, 4, 5, 6]]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$cur_date"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp"
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp.department", department]}
                        }
                    }
                ],
                "as": "dept_logs"
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$dept_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": ASCENDING}}
    ]

    results = list(database.employees.aggregate(pipeline))
    output = []
    for r in results:
        rate = round_half_up(r.get("attendance_rate"), 4)
        m_avg = round_half_up(r.get("moving_avg_7d"), 4)
        output.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": round_half_up(r.get("present_count", 0.0), 2),
            "late_count": r.get("late_count", 0),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg
        })

    return output

@app.get("/admin/explain/{endpoint_name}")
def admin_explain(
    endpoint_name: str,
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """GET /admin/explain/{endpoint}: Returns raw MongoDB explain executionStats."""
    database = get_db()
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    }
    if endpoint_name not in allowed_endpoints:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid endpoint name: {endpoint_name}")

    try:
        if endpoint_name == "attendance_list":
            query: Dict[str, Any] = {}
            if emp_code:
                query["emp_code"] = emp_code
            if status_filter:
                query["status"] = status_filter
            if date_from and date_to:
                if date_from > date_to:
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from > date_to")
                query["date"] = {"$gte": date_from, "$lte": date_to}
            elif date_from:
                query["date"] = {"$gte": date_from}
            elif date_to:
                query["date"] = {"$lte": date_to}

            explain_output = database.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size).explain()
            return {
                "endpoint": endpoint_name,
                "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain_output))
            }

        elif endpoint_name == "employee_monthly":
            if not month or not DATE_YYYY_MM_REGEX.match(month) or not emp_code:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month (YYYY-MM) and emp_code are required")
            pipeline = [
                {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
                {"$group": {"_id": None, "present_days": {"$sum": 1}}}
            ]
            explain_output = database.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
            return {
                "endpoint": endpoint_name,
                "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain_output))
            }

        elif endpoint_name == "department_summary":
            if not month or not DATE_YYYY_MM_REGEX.match(month):
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month (YYYY-MM) is required")
            y, m = map(int, month.split("-"))
            num_days = calendar.monthrange(y, m)[1]
            month_end = f"{month}-{num_days:02d}"
            emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
            if department:
                emp_match["department"] = department
            pipeline = [
                {"$match": emp_match},
                {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}
            ]
            explain_output = database.command("aggregate", "employees", pipeline=pipeline, explain=True)
            return {
                "endpoint": endpoint_name,
                "collection": "employees",
                "explain": json.loads(json_util.dumps(explain_output))
            }

        elif endpoint_name == "late_leaderboard":
            if not month or not DATE_YYYY_MM_REGEX.match(month):
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month (YYYY-MM) is required")
            pipeline = [
                {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
                {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}
            ]
            explain_output = database.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
            return {
                "endpoint": endpoint_name,
                "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain_output))
            }

        elif endpoint_name == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department, from and to are required")
            pipeline = [
                {"$match": {"department": department}},
                {"$limit": 1}
            ]
            explain_output = database.command("aggregate", "employees", pipeline=pipeline, explain=True)
            return {
                "endpoint": endpoint_name,
                "collection": "employees",
                "explain": json.loads(json_util.dumps(explain_output))
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
