"""
Comprehensive API and Business Rules Verification Test Suite for
HROne Employee Attendance & Analytics API.

Tests:
1. Sample data seeds correctly (6 employees, 9 attendance logs)
2. Duplicate employee -> 409 Conflict
3. Concurrent punch-in (using threads) -> exactly one 201, rest 409
4. Overnight shift attendance_date (punch-in before shift_end belongs to previous date)
5. Late at exactly 10 min (not late) vs 11 min (late) [R2]
6. Overtime at 29 vs 30 minutes [R3]
7. Half-up rounding (e.g. 7.125 -> 7.13, not banker's 7.12) [R4]
8. Pagination total reflects applied filters [R10]
9. Mid-month joiner working days (Mon-Fri from joined_on) [R7]
10. Dense-rank ties in leaderboard [Leaderboard Dense Rank]
11. Zero-log department headcount preserved in department summary [R9]

Run via:
    pytest test_api.py -v
or:
    python test_api.py
"""
import concurrent.futures
import os
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

# Ensure app package is importable
current_dir = pathlib.Path(__file__).resolve().parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

IST = timezone(timedelta(hours=5, minutes=30))

# Try importing TestClient from FastAPI / Starlette
try:
    from fastapi.testclient import TestClient
    from app.main import app, db, lifespan
    has_app = True
except Exception as e:
    has_app = False
    print(f"Warning: Could not import app directly ({e}). Running in external mode if BASE_URL is set.")

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")


class ClientWrapper:
    """Wrapper that can send requests via FastAPI TestClient or live HTTP."""
    def __init__(self):
        if BASE_URL:
            import requests
            self.mode = "http"
            self.session = requests.Session()
            self.base = BASE_URL
        elif has_app:
            self.mode = "testclient"
            self.client = TestClient(app)
        else:
            raise RuntimeError("Neither app nor BASE_URL is available for testing.")

    def get(self, url: str, **kwargs):
        if self.mode == "http":
            return self.session.get(f"{self.base}{url}", **kwargs)
        return self.client.get(url, **kwargs)

    def post(self, url: str, **kwargs):
        if self.mode == "http":
            return self.session.post(f"{self.base}{url}", **kwargs)
        return self.client.post(url, **kwargs)

    def patch(self, url: str, **kwargs):
        if self.mode == "http":
            return self.session.patch(f"{self.base}{url}", **kwargs)
        return self.client.patch(url, **kwargs)


client = ClientWrapper()


def seed_sample_data():
    """Resets database and loads sample_data/ from JSON files."""
    mongo = MongoClient(MONGO_URI, tz_aware=True)
    database = mongo[MONGO_DB]
    here = current_dir / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)

    # Ensure indexes are established
    database.employees.create_index("emp_code", unique=True)
    database.employees.create_index([("department", 1), ("emp_code", 1)])
    database.employees.create_index([("department", 1), ("joined_on", 1)])
    database.employees.create_index([("joined_on", 1)])

    database.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    database.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    database.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    database.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    database.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    database.attendance_logs.create_index([("status", 1), ("date", -1)])

    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        database[name].delete_many({})
        database[name].insert_many(docs)
    return database


# =========================================================================== #
# Test 1: Sample Data Seeds Correctly
# =========================================================================== #
def test_01_sample_data_seeds_correctly():
    """Verify that sample data seeds with 6 employees and 9 attendance records."""
    seed_sample_data()

    # Verify employees
    res_emp = client.get("/employees?page=1&page_size=20")
    assert res_emp.status_code == 200, f"Expected 200, got {res_emp.status_code}: {res_emp.text}"
    emp_data = res_emp.json()
    assert emp_data["total"] == 6, f"Expected 6 employees, got {emp_data['total']}"
    assert len(emp_data["items"]) == 6
    codes = [e["emp_code"] for e in emp_data["items"]]
    assert codes == ["EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0005", "EMP0006"]

    # Verify attendance
    res_att = client.get("/attendance?page=1&page_size=20")
    assert res_att.status_code == 200, f"Expected 200, got {res_att.status_code}: {res_att.text}"
    att_data = res_att.json()
    assert att_data["total"] == 9, f"Expected 9 attendance logs, got {att_data['total']}"

    # Verify specific seeded record values and history
    emp1_july6 = next(x for x in att_data["items"] if x["emp_code"] == "EMP0001" and x["date"] == "2026-07-06")
    assert emp1_july6["work_hours"] == 9.12
    assert emp1_july6["late_minutes"] == 0
    assert emp1_july6["overtime_minutes"] == 0
    assert emp1_july6["history"] == []

    emp1_july7 = next(x for x in att_data["items"] if x["emp_code"] == "EMP0001" and x["date"] == "2026-07-07")
    assert emp1_july7["work_hours"] == 9.03
    assert len(emp1_july7["history"]) == 1
    assert emp1_july7["history"][0]["by"] == "hr.admin"
    assert emp1_july7["history"][0]["reason"] == "biometric glitch"


# =========================================================================== #
# Test 2: Duplicate Employee Conflict -> 409
# =========================================================================== #
def test_02_duplicate_employee_conflict():
    """Attempting to create an employee with an existing emp_code must return 409."""
    seed_sample_data()

    payload = {
        "emp_code": "EMP0001",  # already exists
        "name": "Duplicate Asha",
        "email": "duplicate@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 409, f"Expected 409 Conflict, got {res.status_code}: {res.text}"
    assert "detail" in res.json()


# =========================================================================== #
# Test 3: Concurrent Punch-In (Using Threads) -> Exactly One 201
# =========================================================================== #
def test_03_concurrent_punch_in_race_condition():
    """Concurrent punch-in for the same employee and same date must yield exactly one 201 and rest 409."""
    seed_sample_data()

    # 2026-07-20 09:30:00 IST -> 2026-07-20 04:00:00 UTC
    dt_utc = datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc)
    ts_ms = int(dt_utc.timestamp() * 1000)

    payload = {
        "emp_code": "EMP0002",
        "punched_at": ts_ms,
        "status": "PRESENT",
    }

    results = []

    def make_punch():
        # Fresh local client call per thread
        return client.post("/attendance/punch-in", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(make_punch) for _ in range(10)]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    assert results.count(201) == 1, f"Expected exactly one 201, got {results.count(201)} in {results}"
    assert results.count(409) == 9, f"Expected nine 409s, got {results.count(409)} in {results}"


# =========================================================================== #
# Test 4: Overnight Shift attendance_date
# =========================================================================== #
def test_04_overnight_shift_attendance_date():
    """
    R1: Overnight shift (shift_end <= shift_start, e.g. 22:00-06:00).
    A punch-in before shift_end (e.g. 05:30 IST) belongs to the PREVIOUS calendar day.
    """
    seed_sample_data()

    # EMP0005 has shift 22:00 to 06:00
    # Punch-in at 2026-07-21 05:30:00 IST (00:00:00 UTC)
    # Since 05:30 < 06:00, date belongs to 2026-07-20
    dt_early_morning = datetime(2026, 7, 21, 0, 0, 0, tzinfo=timezone.utc)
    early_ms = int(dt_early_morning.timestamp() * 1000)

    res1 = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": early_ms, "status": "PRESENT"},
    )
    assert res1.status_code == 201, f"Expected 201, got {res1.status_code}: {res1.text}"
    assert res1.json()["date"] == "2026-07-20", f"Expected date 2026-07-20, got {res1.json()['date']}"

    # Punch-in at 2026-07-22 22:30:00 IST (17:00:00 UTC)
    # Since 22:30 >= 06:00, date belongs to 2026-07-22
    dt_night = datetime(2026, 7, 22, 17, 0, 0, tzinfo=timezone.utc)
    night_ms = int(dt_night.timestamp() * 1000)

    res2 = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0005", "punched_at": night_ms, "status": "PRESENT"},
    )
    assert res2.status_code == 201, f"Expected 201, got {res2.status_code}: {res2.text}"
    assert res2.json()["date"] == "2026-07-22", f"Expected date 2026-07-22, got {res2.json()['date']}"


# =========================================================================== #
# Test 5: Late at Exactly 10 min (Not Late) vs 11 min (Late)
# =========================================================================== #
def test_05_late_at_10_min_vs_11_min():
    """
    R2: Grace period 10 minutes. Late only if strictly > 10:00 (600s) after shift_start.
    When late, late_minutes is measured from shift_start.
    Shift: 09:30:00 IST.
    09:40:00 IST (600s) -> 0 late_minutes.
    09:41:00 IST (660s) -> 11 late_minutes.
    """
    seed_sample_data()

    # Employee EMP0002 has shift 09:30 - 18:30
    # Day 1: 2026-07-21 at 09:40:00 IST (04:10:00 UTC) -> Exactly 10 min
    t_10min = datetime(2026, 7, 21, 4, 10, 0, tzinfo=timezone.utc)
    res_10 = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0002", "punched_at": int(t_10min.timestamp() * 1000)},
    )
    assert res_10.status_code == 201, res_10.text
    assert res_10.json()["late_minutes"] == 0, f"Expected 0 late minutes, got {res_10.json()['late_minutes']}"

    # Day 2: 2026-07-22 at 09:41:00 IST (04:11:00 UTC) -> Exactly 11 min
    t_11min = datetime(2026, 7, 22, 4, 11, 0, tzinfo=timezone.utc)
    res_11 = client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0002", "punched_at": int(t_11min.timestamp() * 1000)},
    )
    assert res_11.status_code == 201, res_11.text
    assert res_11.json()["late_minutes"] == 11, f"Expected 11 late minutes, got {res_11.json()['late_minutes']}"


# =========================================================================== #
# Test 6: Overtime at 29 vs 30 minutes
# =========================================================================== #
def test_06_overtime_at_29_vs_30_minutes():
    """
    R3: overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30, else 0.
    Shift end: 18:30:00 IST.
    18:59:00 IST -> 29 min -> 0 overtime.
    19:00:00 IST -> 30 min -> 30 overtime.
    """
    seed_sample_data()

    # Day 1: EMP0002 on 2026-07-21
    # Punch in 09:30 IST (04:00 UTC), punch out 18:59 IST (13:29 UTC)
    t_in1 = datetime(2026, 7, 21, 4, 0, 0, tzinfo=timezone.utc)
    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0002", "punched_at": int(t_in1.timestamp() * 1000)},
    )

    t_out29 = datetime(2026, 7, 21, 13, 29, 0, tzinfo=timezone.utc)
    res29 = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0002", "punched_at": int(t_out29.timestamp() * 1000)},
    )
    assert res29.status_code == 200, res29.text
    assert res29.json()["overtime_minutes"] == 0, f"Expected 0 overtime, got {res29.json()['overtime_minutes']}"

    # Day 2: EMP0002 on 2026-07-22
    # Punch in 09:30 IST (04:00 UTC), punch out 19:00 IST (13:30 UTC)
    t_in2 = datetime(2026, 7, 22, 4, 0, 0, tzinfo=timezone.utc)
    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0002", "punched_at": int(t_in2.timestamp() * 1000)},
    )

    t_out30 = datetime(2026, 7, 22, 13, 30, 0, tzinfo=timezone.utc)
    res30 = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0002", "punched_at": int(t_out30.timestamp() * 1000)},
    )
    assert res30.status_code == 200, res30.text
    assert res30.json()["overtime_minutes"] == 30, f"Expected 30 overtime, got {res30.json()['overtime_minutes']}"


# =========================================================================== #
# Test 7: Half-Up Rounding (e.g. 7.125 -> 7.13)
# =========================================================================== #
def test_07_half_up_rounding_work_hours():
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals HALF-UP.
    25650 seconds (7h 7m 30s) / 3600 = 7.125.
    Banker's rounding (round to even) produces 7.12.
    ROUND_HALF_UP produces 7.13.
    """
    seed_sample_data()

    # Punch in: 2026-07-21 09:30:00 IST (04:00:00 UTC)
    t_in = datetime(2026, 7, 21, 4, 0, 0, tzinfo=timezone.utc)
    # Punch out: 2026-07-21 16:37:30 IST (11:07:30 UTC) -> 25650 seconds elapsed
    t_out = datetime(2026, 7, 21, 11, 7, 30, tzinfo=timezone.utc)

    client.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP0002", "punched_at": int(t_in.timestamp() * 1000)},
    )
    res = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0002", "punched_at": int(t_out.timestamp() * 1000)},
    )
    assert res.status_code == 200, res.text
    assert res.json()["work_hours"] == 7.13, f"Expected 7.13 (half-up), got {res.json()['work_hours']}"


# =========================================================================== #
# Test 8: Pagination Total With Filters
# =========================================================================== #
def test_08_pagination_total_with_filters():
    """
    R10: Pagination total always reflects the filtered set, not total collection count.
    """
    seed_sample_data()

    # Filter employees by department
    res_eng = client.get("/employees?department=Engineering&page=1&page_size=1")
    assert res_eng.status_code == 200
    data_eng = res_eng.json()
    assert data_eng["total"] == 2, f"Expected total 2 for Engineering, got {data_eng['total']}"
    assert len(data_eng["items"]) == 1

    # Page 2 of filtered employees
    res_eng_p2 = client.get("/employees?department=Engineering&page=2&page_size=1")
    assert res_eng_p2.status_code == 200
    data_eng_p2 = res_eng_p2.json()
    assert data_eng_p2["total"] == 2
    assert len(data_eng_p2["items"]) == 1
    assert data_eng["items"][0]["emp_code"] != data_eng_p2["items"][0]["emp_code"]

    # Filter attendance by status
    res_present = client.get("/attendance?status=PRESENT&page=1&page_size=2")
    assert res_present.status_code == 200
    data_present = res_present.json()
    assert data_present["total"] == 6, f"Expected 6 PRESENT records in seed data, got {data_present['total']}"
    assert len(data_present["items"]) == 2


# =========================================================================== #
# Test 9: Mid-Month Joiner Working Days
# =========================================================================== #
def test_09_mid_month_joiner_working_days():
    """
    R7: Mon-Fri only, counted from joined_on for mid-month joiners.
    EMP0002 joined on 2026-07-13.
    July 2026 has 23 weekdays total.
    From July 13 to July 31, there are exactly 15 weekdays.
    """
    seed_sample_data()

    res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["working_days"] == 15, f"Expected 15 working days for mid-month joiner, got {data['working_days']}"


# =========================================================================== #
# Test 10: Dense-Rank Ties in Leaderboard
# =========================================================================== #
def test_10_dense_rank_ties():
    """
    Leaderboard ranking with DENSE RANK semantics:
    Tied employees share rank, next rank is NOT skipped (1, 2, 2, 3).
    Deterministic tie-breaker: emp_code ascending.
    """
    seed_sample_data()

    # Seed data has EMP0003 with 35 late minutes on 2026-07-06.
    # Let's add a punch for EMP0004 with 35 late minutes on 2026-07-06 (tied with EMP0003).
    # And EMP0001 with 20 late minutes on 2026-07-21.
    db = seed_sample_data()
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP0004",
            "date": "2026-07-20",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 20, 4, 35, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 20, 13, 0, 0, tzinfo=timezone.utc),
            "work_hours": 8.42,
            "late_minutes": 35,  # tied with EMP0003
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        {
            "emp_code": "EMP0006",
            "date": "2026-07-21",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 21, 4, 20, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 21, 13, 0, 0, tzinfo=timezone.utc),
            "work_hours": 8.67,
            "late_minutes": 20,  # next rank
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
    ])

    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) >= 3

    # EMP0003 and EMP0004 both have 35 late minutes -> both rank 1
    rank1_items = [i for i in items if i["rank"] == 1]
    assert len(rank1_items) == 2, f"Expected 2 tied items at rank 1, got {rank1_items}"
    assert rank1_items[0]["total_late_minutes"] == 35
    assert rank1_items[1]["total_late_minutes"] == 35

    # Deterministic tie-breaker: emp_code ascending
    assert rank1_items[0]["emp_code"] == "EMP0003"
    assert rank1_items[1]["emp_code"] == "EMP0004"

    # Under dense rank, next rank is 2 (not 3)
    rank2_items = [i for i in items if i["rank"] == 2]
    assert len(rank2_items) >= 1
    assert rank2_items[0]["emp_code"] == "EMP0006"
    assert rank2_items[0]["total_late_minutes"] == 20


# =========================================================================== #
# Test 11: Zero-Log Department in Summary
# =========================================================================== #
def test_11_zero_log_department_summary():
    """
    R9: Headcount includes employees whose joined_on <= period end,
    even if they have zero attendance logs.
    """
    db = seed_sample_data()

    # Insert an employee in a brand-new department with NO attendance records in 2026-07
    db.employees.insert_one({
        "emp_code": "EMP0007",
        "name": "Tanvi Gupta",
        "email": "tanvi@example.com",
        "department": "Security",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime(2026, 1, 1, 4, 30, 0, tzinfo=timezone.utc),
    })

    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200, res.text
    items = res.json()["items"]

    sec_dept = next((d for d in items if d["department"] == "Security"), None)
    assert sec_dept is not None, f"Zero-log department 'Security' missing from summary: {items}"
    assert sec_dept["headcount"] == 1
    assert sec_dept["present_days"] == 0.0
    assert sec_dept["avg_work_hours"] is None
    assert sec_dept["late_count"] == 0
    assert sec_dept["total_late_minutes"] == 0
    assert sec_dept["leave_count"] == 0
    assert sec_dept["on_duty_count"] == 0


# =========================================================================== #
# Direct script execution runner
# =========================================================================== #
if __name__ == "__main__":
    tests = [
        test_01_sample_data_seeds_correctly,
        test_02_duplicate_employee_conflict,
        test_03_concurrent_punch_in_race_condition,
        test_04_overnight_shift_attendance_date,
        test_05_late_at_10_min_vs_11_min,
        test_06_overtime_at_29_vs_30_minutes,
        test_07_half_up_rounding_work_hours,
        test_08_pagination_total_with_filters,
        test_09_mid_month_joiner_working_days,
        test_10_dense_rank_ties,
        test_11_zero_log_department_summary,
    ]

    passed = 0
    failed = 0
    print(f"\nRunning {len(tests)} verification tests...\n" + "=" * 60)
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            failed += 1

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)} tests.")
    if failed > 0:
        sys.exit(1)
