"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file: config -> helpers (time + rules) -> models -> indexes -> pipelines -> endpoints.
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, StrictInt, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# override=False (the default): real environment variables take priority over .env
load_dotenv()
log = logging.getLogger("attendance")

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set (environment or .env)")

# tz_aware=True: PyMongo hands back UTC-aware datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NOT_PRESENT_STATUSES = ["ABSENT", "LEAVE"]
ALL_STATUSES = PRESENT_STATUSES + NOT_PRESENT_STATUSES

GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5

# Epoch-millisecond sanity window. Anything below 1e11 ms (March 1973) is almost certainly
# a value in *seconds*; the contract says those are rejected with 422.
MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000  # 2100-01-01

MAX_TREND_DAYS = 92
MAX_SHIFT = timedelta(hours=24)  # punch-out / corrected punch_out must be within 24h of punch_in


# --------------------------------------------------------------------------- #
# Helpers: time conversion
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC-aware datetime, truncated to whole seconds (R1)."""
    ms = ms - ms % 1000
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> epoch ms using integer arithmetic (no float rounding)."""
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_utc_ms() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date of the punch-in; for an overnight shift, a punch-in earlier
    than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if (local.hour, local.minute, local.second) < (eh, em, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that belongs to attendance date `date_str` (IST)."""
    d = date.fromisoformat(date_str)
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if end <= start:  # overnight: the shift ends on the next calendar day
        end += timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# Helpers: business rules R2 - R5, R8
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes from shift_end to punch_out, counted only if at least 30."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up (Decimal, not the float banker's round)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(work_hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50."""
    return work_hours is not None and Decimal(str(work_hours)) < HALF_DAY_BELOW


def round_half_up(value: Any, places: int = 2) -> float:
    """R8: half-up rounding for reported numbers (rates use places=4)."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def ratio(numerator: Any, denominator: Any, places: int) -> float:
    if not denominator:
        return 0.0
    return round_half_up(Decimal(str(numerator)) / Decimal(str(denominator)), places)


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                   date_str: str, emp: dict) -> dict:
    """The four API-computed fields for a record in the given state (see DATA_MODEL.md table)."""
    if status in NOT_PRESENT_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, emp["shift_start"], emp["shift_end"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"]),
        "half_day": is_half_day(hours),
    }


# --------------------------------------------------------------------------- #
# Helpers: serialization (BSON datetimes <-> epoch ms at the API boundary)
# --------------------------------------------------------------------------- #
def _ms_or_none(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def public_employee(doc: dict) -> dict:
    out = {k: doc.get(k) for k in
           ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}
    out["created_at"] = _ms_or_none(doc.get("created_at"))
    return out


def public_record(doc: dict) -> dict:
    """Attendance record as the API returns it: no _id, instants in ms, legacy gaps filled."""
    history = []
    for h in doc.get("history") or []:
        changes = {
            field: {"from": _ms_or_none(c.get("from")), "to": _ms_or_none(c.get("to"))}
            for field, c in (h.get("changes") or {}).items()
        }
        history.append({"at": _ms_or_none(h.get("at")), "by": h.get("by"),
                        "reason": h.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Validation types
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def check_date(value: str) -> str:
    if not DATE_RE.match(value):
        raise ValueError("must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a real calendar date")
    return value


def check_ms(value: int) -> int:
    if not (MIN_MS <= value <= MAX_MS):
        raise ValueError("must be an integer in epoch milliseconds (seconds are rejected)")
    return value


EmpCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
DateStr = Annotated[str, AfterValidator(check_date)]
EpochMs = Annotated[StrictInt, AfterValidator(check_ms)]  # StrictInt: no "123", 1.0 or True


PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
EmployeeCode = Annotated[str, StringConstraints(pattern=r"^EMP\d{4,6}$")]  # contract: EMP + 4-6 digits
Name100 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Dept50 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Email120 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120,
                                            pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=200)]
Actor = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class EmployeeIn(BaseModel):
    emp_code: EmployeeCode
    name: Name100
    email: Email120
    department: Dept50
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: EmpCode
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: EmpCode
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    reason: Reason
    regularized_by: Actor
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None


def parse_date_param(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return check_date(value)
    except ValueError as e:
        raise HTTPException(422, f"{name}: {e}")


def month_info(month: str) -> tuple[str, str, list[str]]:
    """(first day, last day, all Mon-Fri dates) of a YYYY-MM month."""
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return first.isoformat(), last.isoformat(), [d.isoformat() for d in days if d.weekday() < 5]


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    emp, att = db.employees, db.attendance_logs
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    emp.create_index([("joined_on", ASCENDING)], name="joined_on")
    # One record per (employee, day): race-safe punch-in AND the index behind per-employee queries.
    att.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_date_unique")
    # Date-range scans, the default list order (date desc, emp_code asc) and the leaderboard match.
    att.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code")
    # Status filter with the same sort order.
    att.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
                     name="status_date_emp_code")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Do not crash: /health reports 503 until MongoDB answers.
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Health, employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_ms()
    try:  # the unique index on emp_code decides; no check-then-insert race
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = (db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING)
           .skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance: punch-in, punch-out, regularize, list
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(body.status, ts, None, d, emp),
        "history": [],
    }
    try:  # unique (emp_code, date): of N parallel requests exactly one insert succeeds
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    for _ in range(5):
        # Most recent record whose punch_in is at or before this instant. Looking it up by instant
        # (not by today's date) is what makes overnight shifts work across midnight.
        rec = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
            sort=[("punch_in", DESCENDING)],
        )
        if rec is None:
            raise HTTPException(404, "no punch-in found")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        if ts <= rec["punch_in"] or ts - rec["punch_in"] > MAX_SHIFT:
            raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
        fields = derived_fields(rec["status"], rec["punch_in"], ts, rec["date"], emp)
        # Compare-and-set: only one of several parallel punch-outs can match "punch_out is null".
        updated = db.attendance_logs.find_one_and_update(
            {"emp_code": rec["emp_code"], "date": rec["date"],
             "punch_in": rec["punch_in"], "punch_out": None},
            {"$set": {"punch_out": ts, **fields}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return public_record(updated)
        # Lost the race or the record changed under us: re-read and decide again.
    raise HTTPException(409, "record is being modified concurrently, retry")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date_param(date, "date")
    sent = body.model_fields_set
    for f in ("punch_in", "punch_out"):
        if f in sent and getattr(body, f) is None:
            raise HTTPException(422, f"{f} may not be null")
    sent_in, sent_out = "punch_in" in sent, "punch_out" in sent
    emp = get_employee(emp_code)

    for _ in range(10):
        old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if old is None:
            raise HTTPException(404, "no attendance record for that date")

        status = body.status or old["status"]
        p_in, p_out = old.get("punch_in"), old.get("punch_out")
        if status in NOT_PRESENT_STATUSES:
            if sent_in or sent_out:
                raise HTTPException(422, f"{status} clears punch times; do not send punch_in/punch_out")
            p_in = p_out = None
        else:
            if sent_in:
                p_in = ms_to_dt(body.punch_in)
            if sent_out:
                p_out = ms_to_dt(body.punch_out)
            if p_in is None:
                raise HTTPException(422, f"{status} requires a punch_in")
            if sent_in and attendance_date(p_in, emp["shift_start"], emp["shift_end"]) != date:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if (sent_in or sent_out) and p_out is not None and (p_out <= p_in or p_out - p_in > MAX_SHIFT):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

        new = {"status": status, "punch_in": p_in, "punch_out": p_out,
               **derived_fields(status, p_in, p_out, date, emp)}
        before = {
            "status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out"),
            "work_hours": old.get("work_hours"), "late_minutes": old.get("late_minutes") or 0,
            "overtime_minutes": old.get("overtime_minutes") or 0, "half_day": bool(old.get("half_day")),
        }
        if all(new[k] == before[k] for k in ("status", "punch_in", "punch_out")):
            raise HTTPException(422, "request changes nothing")
        changes = {k: {"from": before[k], "to": new[k]} for k in new if before[k] != new[k]}
        entry = {"at": now_utc_ms(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        # Optimistic concurrency: the update only applies if the record still looks the way we read
        # it (same history length = no other correction landed; same punches/status). $push is
        # atomic, so an entry can never be overwritten, only retried.
        n_hist = len(old.get("history") or [])
        updated = db.attendance_logs.find_one_and_update(
            {"emp_code": emp_code, "date": date,
             "status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out"),
             "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_hist]}},
            {"$set": new, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return public_record(updated)
    raise HTTPException(409, "record is being modified concurrently, retry")


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Validated filter / sort / skip / limit shared by GET /attendance and its explain."""
    date_from = parse_date_param(date_from, "date_from")
    date_to = parse_date_param(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, sort, skip, limit = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    # Sort/skip/limit happen in MongoDB, on an index; total comes from the same filter.
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    items = [public_record(d) for d in db.attendance_logs.find(q, sort=sort, skip=skip, limit=limit)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Aggregation pipelines
#
# Every metric is defined once as a function of a field prefix: "$" reads fields of the current
# document (monthly report), "$$l." reads fields of an array element bound as `l` (department
# summary). Sums of work hours are kept as integer hundredths and rounded half-up in Python (R8),
# because MongoDB's $round rounds half to even.
# --------------------------------------------------------------------------- #
def _is_true(p: str) -> dict:
    return {"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}


def _metric_exprs(p: str, weekdays: list[str]) -> dict[str, Any]:
    status, day = f"{p}status", f"{p}date"
    on_weekday = {"$in": [day, weekdays]}  # R7: only Mon-Fri records count towards present days
    is_present = {"$in": [status, PRESENT_STATUSES]}
    late = {"$ifNull": [f"{p}late_minutes", 0]}
    has_hours = {"$and": [is_present, {"$isNumber": f"{p}work_hours"}]}
    return {
        # R5/R6/R7: present Mon-Fri record = 1 day, a half day = 0.5
        "present_days": {"$cond": [{"$and": [is_present, on_weekday]},
                                   {"$cond": [_is_true(p), 0.5, 1]}, 0]},
        "leave_count": {"$cond": [{"$eq": [status, "LEAVE"]}, 1, 0]},
        "on_duty_count": {"$cond": [{"$eq": [status, "ON_DUTY"]}, 1, 0]},
        # late and overtime totals include weekend records (R7)
        "late_count": {"$cond": [{"$gt": [late, 0]}, 1, 0]},
        "total_late_minutes": late,
        "total_overtime_minutes": {"$ifNull": [f"{p}overtime_minutes", 0]},
        # average work hours: present-status records that have a work_hours value
        "work_cents": {"$cond": [has_hours, {"$round": [{"$multiply": [f"{p}work_hours", 100]}, 0]}, 0]},
        "work_records": {"$cond": [has_hours, 1, 0]},
    }


MONTHLY_METRICS = ["present_days", "leave_count", "late_count", "total_late_minutes", "total_overtime_minutes"]
SUMMARY_METRICS = ["present_days", "late_count", "total_late_minutes", "leave_count", "on_duty_count",
                   "work_cents", "work_records"]


def working_days_in_month(month: str, joined_on: str) -> int:
    """R7: Mon-Fri days of the month, counted from joined_on for a mid-month joiner."""
    _, _, weekdays = month_info(month)
    return sum(1 for d in weekdays if d >= joined_on)


def monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    first, last, weekdays = month_info(month)
    exprs = _metric_exprs("$", weekdays)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None, **{n: {"$sum": exprs[n]} for n in MONTHLY_METRICS}}},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    """Starts from employees so people with no logs are counted (R9), and looks the logs up
    per employee, so an average is a sum over records, never an average of averages."""
    first, last, weekdays = month_info(month)
    exprs = _metric_exprs("$$l.", weekdays)
    match: dict[str, Any] = {"joined_on": {"$lte": last}}  # R9
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}],
                     "as": "logs"}},
        {"$addFields": {f"m_{n}": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": exprs[n]}}}
                        for n in SUMMARY_METRICS}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{n: {"$sum": f"$m_{n}"} for n in SUMMARY_METRICS}}},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict]:
    first, last, _ = month_info(month)
    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": late},
                    "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are ignored
    ]
    if department:  # rank inside the department only
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking: every row with rank <= limit
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, date_from: str, date_to: str) -> list[dict]:
    """One row per calendar day, built inside MongoDB: the days come from $range (so weekends and
    empty days exist by construction), the per-day log stats come from one $lookup, and the moving
    average is a $setWindowFields. Rates are kept as integer ten-thousandths so the half-up rounding
    (R8) is exact instead of depending on floating point."""
    start = datetime.fromisoformat(date_from).replace(tzinfo=UTC)
    n_days = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    late = {"$ifNull": ["$late_minutes", 0]}
    # twice the present count, so a half day (0.5) stays an integer: 2 = full day, 1 = half day
    twice_present = {"$cond": [{"$in": ["$status", PRESENT_STATUSES]}, {"$cond": [_is_true("$"), 1, 2]}, 0]}
    frame = {"documents": [-6, 0]}  # current row + up to 6 before it, inside the range only
    rate_ok = {"$isNumber": "$rate_int"}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        {"$lookup": {"from": "attendance_logs", "localField": "codes", "foreignField": "emp_code",
                     "pipeline": [
                         {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                         {"$group": {"_id": "$date", "tw": {"$sum": twice_present},
                                     "late": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}}}}],
                     "as": "daily"}},
        {"$project": {"_id": 0, "joined": 1, "daily": 1,
                      "days": {"$map": {"input": {"$range": [0, n_days]}, "as": "i",
                                        "in": {"$dateAdd": {"startDate": start, "unit": "day",
                                                            "amount": "$$i"}}}}}},
        {"$unwind": "$days"},
        {"$addFields": {"date": {"$dateToString": {"date": "$days", "format": "%Y-%m-%d"}},
                        "is_working_day": {"$in": [{"$dayOfWeek": "$days"}, [2, 3, 4, 5, 6]]}}},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j",
                                                "cond": {"$lte": ["$$j", "$date"]}}}},  # R9
            "stat": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "d",
                                                   "cond": {"$eq": ["$$d._id", "$date"]}}}, 0]}}},
        {"$addFields": {"tw": {"$ifNull": ["$stat.tw", 0]}, "late_count": {"$ifNull": ["$stat.late", 0]}}},
        {"$addFields": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            # floor(tw/2/headcount*10000 + 0.5), written with integers only
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$tw", 10000]}, "$headcount"]},
                                    {"$multiply": ["$headcount", 2]}]}},
            None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "rate_sum": {"$sum": "$rate_int", "window": frame},
            "rate_n": {"$sum": {"$cond": [rate_ok, 1, 0]}, "window": frame}}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$tw", 2]}, "late_count": 1,
            "attendance_rate": {"$cond": [rate_ok, {"$divide": ["$rate_int", 10000]}, None]},
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$rate_n", 0]},
                # mean of the rounded rates, half-up at 4 decimals: floor((2*sum + n) / (2*n)) / 10000
                {"$divide": [{"$floor": {"$divide": [{"$add": [{"$multiply": ["$rate_sum", 2]}, "$rate_n"]},
                                                     {"$multiply": ["$rate_n", 2]}]}}, 10000]},
                None]}}},
        {"$sort": {"date": 1}},
    ]


def validate_trend_range(date_from: str, date_to: str) -> tuple[str, str]:
    date_from, date_to = parse_date_param(date_from, "from"), parse_date_param(date_to, "to")
    span = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    if span < 1:
        raise HTTPException(422, "to must not be before from")
    if span > MAX_TREND_DAYS:
        raise HTTPException(422, f"range may not exceed {MAX_TREND_DAYS} days")
    return date_from, date_to


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    month_info(month)
    emp = get_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    t = rows[0] if rows else {}
    working_days = working_days_in_month(month, emp["joined_on"])
    present = t.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present),
        "leave_days": t.get("leave_count", 0),
        "late_count": t.get("late_count", 0),
        "total_late_minutes": t.get("total_late_minutes", 0),
        "total_overtime_minutes": t.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(str(present)) * 100 / working_days, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    month_info(month)
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        n_work = int(r["work_records"])
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"]),
            "avg_work_hours": ratio(Decimal(int(r["work_cents"])) / 100, n_work, 2) if n_work else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    month_info(month)
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(..., alias="from"),
                     date_to: str = Query(..., alias="to")):
    date_from, date_to = validate_trend_range(date_from, date_to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    rows = list(db.employees.aggregate(department_trend_pipeline(department, date_from, date_to)))
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Explain: the same filters / pipelines as the real endpoints, run through explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    trend_from: Optional[str] = Query(None, alias="from"),
    trend_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(value: Optional[str], name: str) -> str:
        if not value:
            raise HTTPException(422, f"{name} is required")
        return value

    if endpoint == "attendance_list":
        q, sort, skip, lim = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        cmd = {"find": collection, "filter": q, "sort": dict(sort), "skip": skip, "limit": lim}
    elif endpoint == "employee_monthly":
        month_info(need(month, "month"))
        collection = "attendance_logs"
        cmd = {"aggregate": collection, "pipeline": monthly_pipeline(need(emp_code, "emp_code"), month),
               "cursor": {}}
    elif endpoint == "department_summary":
        month_info(need(month, "month"))
        collection = "employees"
        cmd = {"aggregate": collection, "pipeline": department_summary_pipeline(month, department),
               "cursor": {}}
    elif endpoint == "late_leaderboard":
        month_info(need(month, "month"))
        collection = "attendance_logs"
        cmd = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(month, limit, department),
               "cursor": {}}
    else:  # department_trend
        d_from, d_to = validate_trend_range(need(trend_from, "from"), need(trend_to, "to"))
        collection = "employees"
        cmd = {"aggregate": collection,
               "pipeline": department_trend_pipeline(need(department, "department"), d_from, d_to),
               "cursor": {}}

    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))}
