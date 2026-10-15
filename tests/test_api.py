"""
Tests for the attendance API.

  pytest tests/                       -> runs against a real MongoDB (MONGO_URI / MONGO_DB; the DB is wiped!)
  USE_MONGOMOCK=1 pytest tests/       -> in-memory fake; runs everything except the aggregation-pipeline tests

Install: pip install pytest httpx mongomock
"""
import os
import threading
from datetime import datetime, timezone

import pytest

USE_MOCK = os.environ.get("USE_MONGOMOCK") == "1"
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

if USE_MOCK:
    import mongomock

    main.client = mongomock.MongoClient(tz_aware=True)
    main.db = main.client["attendance_test"]

needs_mongo = pytest.mark.skipif(USE_MOCK, reason="needs a real MongoDB (window functions, $densify, $lookup)")

IST_OFFSET_MS = 5 * 3600_000 + 30 * 60_000


def ms(y, mo, d, h=0, mi=0, s=0):
    """IST wall-clock -> epoch ms."""
    utc = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    return int(utc.timestamp() * 1000) - IST_OFFSET_MS


@pytest.fixture()
def api():
    main.db.employees.delete_many({})
    main.db.attendance_logs.delete_many({})
    main.ensure_indexes()
    with TestClient(main.app) as c:
        yield c


def add_emp(api, code="EMP0001", dept="Eng", start="09:30", end="18:30", joined="2026-01-05"):
    r = api.post("/employees", json={"emp_code": code, "name": code, "email": f"{code}@x.com",
                                     "department": dept, "shift_start": start, "shift_end": end,
                                     "joined_on": joined})
    assert r.status_code == 201, r.text
    return r.json()


# ----------------------------- pure rule helpers ----------------------------- #
def dt(y, mo, d, h, mi, s=0):
    return datetime.fromtimestamp(ms(y, mo, d, h, mi, s) / 1000, tz=timezone.utc)


@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 0, 0, 0), (9, 30, 0, 0), (9, 41, 0, 11),
])
def test_r2_late(h, mi, s, expected):
    assert main.compute_late_minutes(dt(2026, 7, 6, h, mi, s), "2026-07-06", "09:30", "18:30") == expected


def test_r3_overtime():
    f = lambda h, mi, s=0: main.compute_overtime(dt(2026, 7, 6, h, mi, s), "2026-07-06", "09:30", "18:30")
    assert f(18, 59, 59) == 0  # 29 min 59 s -> floor 29 -> below minimum
    assert f(19, 0) == 30
    assert f(19, 40, 59) == 70
    assert f(17, 0) == 0
    # overnight: shift ends next day
    assert main.compute_overtime(dt(2026, 7, 7, 6, 40), "2026-07-06", "22:00", "06:00") == 40


def test_r4_r5_work_hours_half_up():
    a = dt(2026, 7, 6, 9, 0)
    assert main.compute_work_hours(a, dt(2026, 7, 6, 18, 0, 18)) == 9.01  # 9.005 -> half-up 9.01 (round() gives 9.0)
    assert main.compute_work_hours(a, dt(2026, 7, 6, 13, 30)) == 4.5
    assert main.is_half_day(4.49) and not main.is_half_day(4.5)


def test_r1_attendance_date():
    # overnight 22:00-06:00: 02:00 IST on the 7th belongs to the shift that started on the 6th
    assert main.attendance_date(dt(2026, 7, 7, 2, 0), "22:00", "06:00") == "2026-07-06"
    assert main.attendance_date(dt(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"
    assert main.attendance_date(dt(2026, 7, 7, 6, 0), "22:00", "06:00") == "2026-07-07"  # not earlier than end
    assert main.attendance_date(dt(2026, 7, 6, 0, 30), "09:30", "18:30") == "2026-07-06"  # day shift: IST date, not UTC


def test_round_half_up():
    assert main.round_half_up(0.285) == 0.29
    assert main.round_half_up(2.5, 0) == 3.0
    assert main.round_half_up(0.12345, 4) == 0.1235


# ----------------------------- employees ----------------------------- #
def test_create_employee_and_duplicates(api):
    e = add_emp(api)
    assert "_id" not in e and isinstance(e["created_at"], int)
    assert api.post("/employees", json={"emp_code": "EMP0001", "name": "n", "email": "a@b.co",
                                        "department": "x", "joined_on": "2026-01-01"}).status_code == 409


@pytest.mark.parametrize("patch", [
    {"email": "nope"}, {"shift_start": "9:30"}, {"shift_end": "25:00"}, {"joined_on": "2026-02-30"},
    {"joined_on": "20260105"}, {"emp_code": ""}, {"emp_code": "E1"}, {"emp_code": "EMP12"}, {"emp_code": "EMP1234567"},
    {"name": "  "}, {"department": ""}, {"shift_start": "09:00", "shift_end": "09:00"}, {"name": "x" * 101},
])
def test_create_employee_422(api, patch):
    body = {"emp_code": "EMP0009", "name": "n", "email": "a@b.co", "department": "d", "joined_on": "2026-01-01", **patch}
    assert api.post("/employees", json=body).status_code == 422


def test_list_employees_pagination_and_total(api):
    for i in range(5):
        add_emp(api, f"EMP{i:04d}", dept="A" if i < 3 else "B")
    r = api.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0000", "EMP0001"] and r["total"] == 5
    r = api.get("/employees", params={"department": "A", "page": 2, "page_size": 2}).json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0002"] and r["total"] == 3
    for bad in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
        assert api.get("/employees", params=bad).status_code == 422


# ----------------------------- punch-in ----------------------------- #
def punch_in(api, code="EMP0001", at=None, **kw):
    body = {"emp_code": code, **kw}
    if at is not None:
        body["punched_at"] = at
    return api.post("/attendance/punch-in", json=body)


def test_punch_in_basic_and_truncation(api):
    add_emp(api)
    r = punch_in(api, at=ms(2026, 7, 6, 9, 40, 1) + 999)
    assert r.status_code == 201
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 10 and j["status"] == "PRESENT"
    assert j["punch_in"] == ms(2026, 7, 6, 9, 40, 1)  # truncated to whole seconds
    assert "id" not in j and "_id" not in j and j["history"] == [] and j["work_hours"] is None
    assert punch_in(api, at=ms(2026, 7, 6, 11)).status_code == 409


def test_punch_in_errors(api):
    add_emp(api)
    assert punch_in(api, code="NOPE", at=ms(2026, 7, 6, 9)).status_code == 404
    assert punch_in(api, at=ms(2026, 7, 6, 9), status="ABSENT").status_code == 422
    assert punch_in(api, at=ms(2026, 7, 6, 9), status="HOLIDAY").status_code == 422
    for bad in (1751770000, "1751770000000", 1751770000000.5, True, 0, -5, "x"):
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422, bad


def test_punch_in_overnight_date(api):
    add_emp(api, "EMP0301", start="22:00", end="06:00")
    j = punch_in(api, "EMP0301", at=ms(2026, 7, 7, 2, 0)).json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 240


def test_punch_in_race_exactly_one_201(api):
    add_emp(api)
    codes, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        codes.append(punch_in(api, at=ms(2026, 7, 6, 9)).status_code)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [201] + [409] * 7


# ----------------------------- punch-out ----------------------------- #
def punch_out(api, code="EMP0001", at=None):
    body = {"emp_code": code}
    if at is not None:
        body["punched_at"] = at
    return api.post("/attendance/punch-out", json=body)


def test_punch_out_rules(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 6, 10, 5))
    r = punch_out(api, at=ms(2026, 7, 6, 19, 10))
    assert r.status_code == 200
    j = r.json()
    assert (j["work_hours"], j["late_minutes"], j["overtime_minutes"], j["half_day"]) == (9.08, 35, 40, False)
    assert punch_out(api, at=ms(2026, 7, 6, 20)).status_code == 409


def test_punch_out_422_rules(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 6, 9, 30))
    assert punch_out(api, at=ms(2026, 7, 6, 9, 30)).status_code == 422          # equal to punch_in
    assert punch_out(api, at=ms(2026, 7, 7, 9, 30, 1)).status_code == 422        # more than 24h later
    assert punch_out(api, at=ms(2026, 7, 7, 9, 30)).status_code == 200          # exactly 24h is allowed


def test_punch_out_half_day_and_errors(api):
    add_emp(api)
    assert punch_out(api, at=ms(2026, 7, 6, 9)).status_code == 404  # no record
    assert punch_out(api, code="NOPE", at=ms(2026, 7, 6, 9)).status_code == 404
    punch_in(api, at=ms(2026, 7, 6, 9, 30))
    assert punch_out(api, at=ms(2026, 7, 6, 9, 0)).status_code == 404  # before any punch-in
    j = punch_out(api, at=ms(2026, 7, 6, 13, 0)).json()
    assert j["work_hours"] == 3.5 and j["half_day"] is True


def test_punch_out_overnight_across_midnight(api):
    add_emp(api, "EMP0301", start="22:00", end="06:00")
    punch_in(api, "EMP0301", at=ms(2026, 7, 6, 21, 55))
    j = punch_out(api, "EMP0301", at=ms(2026, 7, 7, 6, 40)).json()
    assert j["date"] == "2026-07-06" and j["work_hours"] == 8.75 and j["overtime_minutes"] == 40


def test_punch_out_race_exactly_one_200(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 6, 9))
    codes, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        codes.append(punch_out(api, at=ms(2026, 7, 6, 18)).status_code)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200] + [409] * 7


# ----------------------------- regularize (PATCH) ----------------------------- #
def patch(api, code="EMP0001", date="2026-07-07", **body):
    body.setdefault("regularized_by", "hr.admin")
    body.setdefault("reason", "valid reason")
    return api.patch(f"/attendance/{code}/{date}", json=body)


def test_regularize_recomputes_and_audits(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 7, 10, 5))
    punch_out(api, at=ms(2026, 7, 7, 18, 30))
    r = patch(api, punch_in=ms(2026, 7, 7, 9, 28), reason="biometric glitch")
    assert r.status_code == 200
    j = r.json()
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.03 and j["punch_in"] == ms(2026, 7, 7, 9, 28)
    h = j["history"]
    assert len(h) == 1 and h[0]["by"] == "hr.admin" and h[0]["reason"] == "biometric glitch"
    assert set(h[0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}  # only what changed
    assert h[0]["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert h[0]["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert isinstance(h[0]["at"], int)
    # a second correction appends, never replaces
    assert len(patch(api, status="WFH").json()["history"]) == 2


def test_regularize_to_absent_and_back(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 7, 10, 5))
    j = patch(api, status="LEAVE").json()
    assert j["punch_in"] is None and j["late_minutes"] == 0 and j["status"] == "LEAVE"
    assert patch(api, status="PRESENT").status_code == 422  # needs punch_in
    assert patch(api, status="ABSENT", punch_in=ms(2026, 7, 7, 9)).status_code == 422
    j = patch(api, status="PRESENT", punch_in=ms(2026, 7, 7, 9, 0), punch_out=ms(2026, 7, 7, 17, 0)).json()
    assert j["work_hours"] == 8.0 and len(j["history"]) == 2  # the two 422s wrote nothing


def test_regularize_errors(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 7, 10, 5))
    assert patch(api, code="NOPE", status="LEAVE").status_code == 404
    assert patch(api, date="2026-07-09", status="LEAVE").status_code == 404
    assert patch(api, date="2026-13-01", status="LEAVE").status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-07", json={"regularized_by": "x", "reason": "short"}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-07", json={"regularized_by": "x", "reason": "long enough"}).status_code == 422  # changes nothing
    assert patch(api, status="PRESENT").status_code == 422                       # same status: changes nothing
    assert patch(api, punch_in=ms(2026, 7, 7, 10, 5)).status_code == 422        # same punch_in: changes nothing
    assert patch(api, punch_in=ms(2026, 7, 8, 10, 5)).status_code == 422        # moves to another attendance date
    assert patch(api, punch_in=None).status_code == 422                          # null is not a valid instant
    assert patch(api, reason="abcd", status="WFH").status_code == 422           # reason too short
    assert patch(api, status="NOPE").status_code == 422
    assert patch(api, punch_in="123").status_code == 422
    assert patch(api, punch_in=1751770000).status_code == 422
    assert patch(api, punch_out=ms(2026, 7, 7, 9, 0)).status_code == 422  # before punch_in
    assert patch(api, punch_out=ms(2026, 7, 8, 10, 6)).status_code == 422  # more than 24h after punch_in


def test_regularize_concurrent_no_lost_history(api):
    add_emp(api)
    punch_in(api, at=ms(2026, 7, 7, 10, 5))
    n, barrier, codes = 8, threading.Barrier(8), []

    def go(i):
        barrier.wait()
        codes.append(patch(api, reason=f"reason {i}", punch_in=ms(2026, 7, 7, 9, i)).status_code)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert codes.count(200) == n
    hist = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["history"]
    assert sorted(h["reason"] for h in hist) == sorted(f"reason {i}" for i in range(n))


# ----------------------------- list attendance ----------------------------- #
def test_list_attendance_filters_sort_pagination(api):
    add_emp(api, "EMP0001")
    add_emp(api, "EMP0002")
    for day in (6, 7, 8):
        punch_in(api, "EMP0001", at=ms(2026, 7, day, 9))
    punch_in(api, "EMP0002", at=ms(2026, 7, 8, 9), status="WFH")
    r = api.get("/attendance", params={"page_size": 2}).json()
    assert r["total"] == 4
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002")]
    r = api.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-08"}).json()
    assert r["total"] == 2 and [i["date"] for i in r["items"]] == ["2026-07-08", "2026-07-07"]
    assert api.get("/attendance", params={"status": "WFH"}).json()["total"] == 1
    for bad in ({"date_from": "2026-7-1"}, {"status": "X"}, {"page": 0}, {"page_size": 500},
                {"date_from": "2026-07-09", "date_to": "2026-07-01"}):
        assert api.get("/attendance", params=bad).status_code == 422


def test_legacy_record_defaults(api):
    add_emp(api)
    main.db.attendance_logs.insert_one({
        "emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc),
        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    i = api.get("/attendance").json()["items"][0]
    assert i["half_day"] is False and i["history"] == [] and isinstance(i["punch_in"], int)


def test_health(api):
    assert api.get("/health").status_code in (200, 503)


# ----------------------------- analytics (real MongoDB only) ----------------------------- #
def seed(code, date, status="PRESENT", late=0, ot=0, wh=None, half=False, pin=True):
    d = datetime.fromisoformat(date)
    main.db.attendance_logs.insert_one({
        "emp_code": code, "date": date, "status": status,
        "punch_in": d.replace(hour=4, tzinfo=timezone.utc) if pin else None, "punch_out": None,
        "work_hours": wh, "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []})


@needs_mongo
def test_monthly_summary(api):
    add_emp(api, "EMP0101", joined="2026-07-08")   # mid-month joiner: weekdays from Wed 8 Jul = 18
    seed("EMP0101", "2026-07-08", late=20, wh=8.12, ot=40)             # Wed
    seed("EMP0101", "2026-07-09", wh=3.5, half=True)                   # Thu, half day -> 0.5
    seed("EMP0101", "2026-07-10", status="ABSENT", pin=False)          # Fri, absent: not present, not leave
    seed("EMP0101", "2026-07-13", status="LEAVE", pin=False)           # Mon
    seed("EMP0101", "2026-07-11", late=15, ot=60, wh=2.0, half=True)   # Saturday: late/overtime only
    j = api.get("/analytics/employees/EMP0101/monthly", params={"month": "2026-07"}).json()
    assert j == {"emp_code": "EMP0101", "month": "2026-07", "working_days": 18, "present_days": 1.5,
                 "leave_days": 1, "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 100,
                 "attendance_pct": 8.33}   # 1.5 / 18 * 100 = 8.333


@needs_mongo
def test_monthly_404_422_and_empty(api):
    assert api.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    add_emp(api, "EMP0101")
    assert api.get("/analytics/employees/EMP0101/monthly", params={"month": "2026-13"}).status_code == 422
    assert api.get("/analytics/employees/EMP0101/monthly").status_code == 422
    j = api.get("/analytics/employees/EMP0101/monthly", params={"month": "2026-07"}).json()
    assert j["present_days"] == 0 and j["working_days"] == 23 and j["attendance_pct"] == 0.0
    add_emp(api, "EMP0102", joined="2026-08-01")  # joins after the month: no working days -> null pct
    j = api.get("/analytics/employees/EMP0102/monthly", params={"month": "2026-07"}).json()
    assert j["working_days"] == 0 and j["attendance_pct"] is None


@needs_mongo
def test_department_summary(api):
    add_emp(api, "EMP0201", dept="Eng", joined="2026-01-01")
    add_emp(api, "EMP0202", dept="Eng", joined="2026-01-01")
    add_emp(api, "EMP0203", dept="Eng", joined="2026-08-01")      # not joined yet -> not counted
    add_emp(api, "EMP0204", dept="Eng", joined="2026-01-01")      # no logs at all -> still counted
    add_emp(api, "EMP0211", dept="Ops", joined="2026-08-15")      # whole department joins later -> omitted
    seed("EMP0201", "2026-07-06", wh=10.0)
    seed("EMP0201", "2026-07-07", wh=8.0)
    seed("EMP0201", "2026-07-08", wh=6.0, late=12)
    seed("EMP0201", "2026-07-09", status="ON_DUTY")                # present, no work_hours yet
    seed("EMP0201", "2026-06-30", wh=1.0)                          # outside the month
    seed("EMP0202", "2026-07-06", wh=2.0, late=5)
    seed("EMP0202", "2026-07-07", status="LEAVE", pin=False)
    seed("EMP0202", "2026-07-11", status="WFH", wh=4.0)            # weekend: not a present day, but a record with hours
    j = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert j["month"] == "2026-07" and [i["department"] for i in j["items"]] == ["Eng"]
    eng = j["items"][0]
    assert eng == {"department": "Eng", "headcount": 3, "present_days": 5.0,
                   "avg_work_hours": 6.0,                 # (10+8+6+2+4)/5 records, not an average of averages
                   "late_count": 2, "total_late_minutes": 17, "leave_count": 1, "on_duty_count": 1}
    assert api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Nope"}).json()["items"] == []


@needs_mongo
def test_leaderboard_ties_and_limit(api):
    for c, late in (("EMP0101", 50), ("EMP0102", 40), ("EMP0103", 40), ("EMP0104", 30), ("EMP0105", 0)):
        add_emp(api, c, dept="Eng" if c != "EMP0104" else "Ops")
        seed(c, "2026-07-06", late=late)
    seed("EMP0999", "2026-07-06", late=99)  # log of an unknown employee is ignored
    get = lambda **kw: [(r["rank"], r["emp_code"]) for r in api.get(
        "/analytics/leaderboard/late", params={"month": "2026-07", **kw}).json()["items"]]
    assert get() == [(1, "EMP0101"), (2, "EMP0102"), (2, "EMP0103"), (4, "EMP0104")]
    assert get(limit=1) == [(1, "EMP0101")]
    assert get(limit=2) == [(1, "EMP0101"), (2, "EMP0102"), (2, "EMP0103")]   # every row with rank <= limit
    assert get(limit=3) == [(1, "EMP0101"), (2, "EMP0102"), (2, "EMP0103")]
    assert get(limit=4)[-1] == (4, "EMP0104")
    assert get(department="Eng") == [(1, "EMP0101"), (2, "EMP0102"), (2, "EMP0103")]   # ranked inside the department
    assert get(department="Ops") == [(1, "EMP0104")]
    row = api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"][0]
    assert set(row) == {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
    for bad in ({"limit": 0}, {"limit": 51}, {"month": "2026-7"}):
        assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", **bad}).status_code == 422


@needs_mongo
def test_trend(api):
    add_emp(api, "EMP0101", dept="Eng", joined="2026-01-05")
    add_emp(api, "EMP0102", dept="Eng", joined="2026-01-05")
    add_emp(api, "EMP0103", dept="Eng", joined="2026-07-08")   # joins on Wed the 8th
    seed("EMP0101", "2026-07-06", late=10)
    seed("EMP0102", "2026-07-06", late=20)
    seed("EMP0101", "2026-07-08", half=True)                    # half day = 0.5
    r = api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-04", "to": "2026-07-10"})
    assert r.status_code == 200
    j = r.json()
    assert j["department"] == "Eng" and len(j["items"]) == 7
    rows = j["items"]
    assert [x["date"] for x in rows] == [f"2026-07-{d:02d}" for d in range(4, 11)]   # weekend + empty days kept
    assert [x["is_working_day"] for x in rows] == [False, False, True, True, True, True, True]
    assert [x["headcount"] for x in rows] == [2, 2, 2, 2, 3, 3, 3]
    assert [x["present_count"] for x in rows] == [0, 0, 2, 0, 0.5, 0, 0]
    assert [x["late_count"] for x in rows] == [0, 0, 2, 0, 0, 0, 0]
    assert [x["attendance_rate"] for x in rows] == [None, None, 1.0, 0.0, 0.1667, 0.0, 0.0]
    assert [x["moving_avg_7d"] for x in rows] == [None, None, 1.0, 0.5, 0.3889, 0.2917, 0.2333]
    q = lambda **kw: api.get("/analytics/departments/Eng/trend", params=kw).status_code
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-04", "to": "2026-07-10"}).status_code == 404
    assert q(**{"from": "2026-07-10", "to": "2026-07-04"}) == 422
    assert q(**{"from": "2026-01-01", "to": "2026-04-03"}) == 422     # 93 days
    assert q(**{"from": "2026-01-01", "to": "2026-04-02"}) == 200     # exactly 92 days
    assert q(**{"from": "2026-07-04"}) == 422


@needs_mongo
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0101", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"status": "WFH"}),
    ("employee_monthly", {"emp_code": "EMP0101", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_index(api, endpoint, params):
    add_emp(api, "EMP0101", dept="Eng")
    for d in range(1, 29):
        seed("EMP0101", f"2026-07-{d:02d}", status="WFH" if d % 2 else "PRESENT")
    r = api.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200
    j = r.json()
    assert j["endpoint"] == endpoint and j["collection"] in ("employees", "attendance_logs") and isinstance(j["explain"], dict)
    text = r.text
    assert "IXSCAN" in text and "COLLSCAN" not in text, text[:2000]


def test_explain_bad_input(api):
    assert api.get("/admin/explain/nope").status_code == 422                       # not in the enum
    assert api.get("/admin/explain/department_summary").status_code == 422        # month missing
    assert api.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert api.get("/admin/explain/department_trend", params={"department": "Eng", "from": "2026-07-01"}).status_code == 422
