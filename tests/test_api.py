import pytest
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
from fastapi.testclient import TestClient
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.main import (
    app,
    get_db,
    round_half_up,
    compute_attendance_date,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    compute_half_day,
    validate_timestamp_ms,
    ms_to_utc_datetime,
    datetime_to_ms,
    IST
)

client = TestClient(app)

# -------------------------------------------------------------------------
# Unit Tests for Helper Functions & Business Rules (R1 - R6)
# -------------------------------------------------------------------------

def test_round_half_up():
    assert round_half_up(9.1166, 2) == 9.12
    assert round_half_up(9.0833, 2) == 9.08
    assert round_half_up(3.5, 2) == 3.5
    assert round_half_up(8.5333, 2) == 8.53
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234
    assert round_half_up(None) is None

def test_validate_timestamp_ms():
    # Valid timestamp
    valid_ms = 1783307880123
    assert validate_timestamp_ms(valid_ms) == 1783307880000  # Truncated to seconds

    # Booleans must be rejected
    with pytest.raises(ValueError, match="boolean"):
        validate_timestamp_ms(True)
    with pytest.raises(ValueError, match="boolean"):
        validate_timestamp_ms(False)

    # Strings and floats must be rejected
    with pytest.raises(ValueError, match="integer"):
        validate_timestamp_ms("1783307880000")
    with pytest.raises(ValueError, match="integer"):
        validate_timestamp_ms(1783307880000.5)

    # Seconds (too small)
    with pytest.raises(ValueError, match="range"):
        validate_timestamp_ms(1783307880)

    # Null when not allowed
    with pytest.raises(ValueError, match="null"):
        validate_timestamp_ms(None, allow_none=False)

    assert validate_timestamp_ms(None, allow_none=True) is None

def test_r1_attendance_date():
    # Normal shift 09:30 - 18:30
    # Punch in 2026-07-06 09:28 IST (03:58 UTC)
    dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    att_date = compute_attendance_date(dt_utc, "09:30", "18:30")
    assert att_date == "2026-07-06"

    # Overnight shift 22:00 - 06:00
    # Punch in 2026-07-06 21:55 IST (16:25 UTC) -> Same day shift
    dt_utc1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    att_date1 = compute_attendance_date(dt_utc1, "22:00", "06:00")
    assert att_date1 == "2026-07-06"

    # Punch in 2026-07-07 02:00 IST (2026-07-06 20:30 UTC) -> Belongs to previous day's shift
    dt_utc2 = datetime(2026, 7, 6, 20, 30, 0, tzinfo=timezone.utc)
    att_date2 = compute_attendance_date(dt_utc2, "22:00", "06:00")
    assert att_date2 == "2026-07-06"

    # Punch in 2026-07-07 06:00 IST -> Not earlier than 06:00, belongs to 2026-07-07
    dt_utc3 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)
    att_date3 = compute_attendance_date(dt_utc3, "22:00", "06:00")
    assert att_date3 == "2026-07-07"

def test_r2_late_minutes():
    # Shift 09:30:00
    # Punch at 09:40:00 (10 min exactly) -> 0 late
    dt_utc_0940 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_utc_0940, "2026-07-06", "09:30") == 0

    # Punch at 09:40:01 (10 min 1 sec) -> 10 late
    dt_utc_094001 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_utc_094001, "2026-07-06", "09:30") == 10

    # Punch at 10:15:59 (45 min 59 sec) -> 45 late
    dt_utc_101559 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_utc_101559, "2026-07-06", "09:30") == 45

    # Sample 2: 10:05 IST -> 35 late
    dt_utc_1005 = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_utc_1005, "2026-07-06", "09:30") == 35

def test_r3_overtime_minutes():
    # Shift end 18:30 IST. Out at 18:35 IST (5 min < 30) -> 0 OT
    out_1835 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(out_1835, "2026-07-06", "09:30", "18:30") == 0

    # Out at 19:10 IST (40 min >= 30) -> 40 OT
    out_1910 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(out_1910, "2026-07-06", "09:30", "18:30") == 40

    # Overnight shift 22:00 - 06:00 on 2026-07-06. Ends 2026-07-07 06:00 IST.
    # Out at 06:40 IST (01:10 UTC on 2026-07-07) -> 40 OT
    out_0640 = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(out_0640, "2026-07-06", "22:00", "06:00") == 40

def test_r4_and_r5_work_hours_and_half_day():
    # 09:28 IST to 18:35 IST = 9.12 hours
    tin = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    tout = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    wh = compute_work_hours(tin, tout)
    assert wh == 9.12
    assert compute_half_day(wh) is False

    # 3.5 hours worked -> half_day True (< 4.50)
    tin_half = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    tout_half = datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc)
    wh_half = compute_work_hours(tin_half, tout_half)
    assert wh_half == 3.50
    assert compute_half_day(wh_half) is True

    # 4.50 hours worked -> half_day False (not < 4.50)
    tin_45 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    tout_45 = datetime(2026, 7, 7, 8, 30, 0, tzinfo=timezone.utc)
    wh_45 = compute_work_hours(tin_45, tout_45)
    assert wh_45 == 4.50
    assert compute_half_day(wh_45) is False

# -------------------------------------------------------------------------
# Mock In-Memory Database Tests for FastAPI Endpoints
# -------------------------------------------------------------------------

class MockCollection:
    def __init__(self, name: str, unique_keys=None):
        self.name = name
        self.docs = []
        self.unique_keys = unique_keys or []

    def create_index(self, keys, **kwargs):
        pass

    def count_documents(self, filter_dict):
        return len(list(self._filter(filter_dict)))

    def insert_one(self, doc):
        # Check uniqueness
        for key in self.unique_keys:
            if isinstance(key, tuple):
                query = {k: doc.get(k) for k in key}
            else:
                query = {key: doc.get(key)}
            if any(all(d.get(k) == v for k, v in query.items()) for d in self.docs):
                raise DuplicateKeyError(f"Duplicate key on {key}")
        import copy
        new_doc = copy.deepcopy(doc)
        if "_id" not in new_doc:
            new_doc["_id"] = str(len(self.docs) + 1)
        self.docs.append(new_doc)
        return type("InsertResult", (), {"inserted_id": new_doc["_id"]})()

    def find_one(self, filter_dict, sort=None):
        matches = list(self._filter(filter_dict))
        if sort:
            for field, order in reversed(sort):
                matches.sort(key=lambda x: (x.get(field) is None, x.get(field)), reverse=(order == DESCENDING or order == -1))
        if matches:
            import copy
            return copy.deepcopy(matches[0])
        return None

    def find(self, filter_dict, projection=None):
        matches = list(self._filter(filter_dict))
        class MockCursor:
            def __init__(self, items):
                self.items = items
            def sort(self, *args, **kwargs):
                if len(args) == 2 and isinstance(args[0], str):
                    sort_spec = [(args[0], args[1])]
                elif len(args) == 1 and isinstance(args[0], str):
                    sort_spec = [(args[0], ASCENDING)]
                elif len(args) == 1 and isinstance(args[0], list):
                    sort_spec = args[0]
                else:
                    sort_spec = []
                for field, order in reversed(sort_spec):
                    self.items.sort(key=lambda x: (x.get(field) is None, x.get(field)), reverse=(order == DESCENDING or order == -1))
                return self
            def skip(self, n):
                self.items = self.items[n:]
                return self
            def limit(self, n):
                self.items = self.items[:n]
                return self
            def __iter__(self):
                import copy
                for it in self.items:
                    yield copy.deepcopy(it)
            def explain(self, *args, **kwargs):
                return {
                    "queryPlanner": {
                        "winningPlan": {
                            "stage": "IXSCAN",
                            "indexName": "date_-1_emp_code_1"
                        }
                    },
                    "executionStats": {
                        "executionSuccess": True,
                        "nReturned": len(self.items),
                        "totalDocsExamined": len(self.items)
                    }
                }
        return MockCursor(matches)

    def find_one_and_update(self, filter_dict, update_dict, return_document=None):
        import copy
        for idx, doc in enumerate(self.docs):
            match = True
            for k, v in filter_dict.items():
                if k == "punch_out" and v is None:
                    if doc.get("punch_out") is not None:
                        match = False
                        break
                elif k == "history" and isinstance(v, dict) and "$size" in v:
                    if len(doc.get("history", [])) != v["$size"]:
                        match = False
                        break
                elif doc.get(k) != v:
                    match = False
                    break
            if match:
                if "$set" in update_dict:
                    for sk, sv in update_dict["$set"].items():
                        doc[sk] = sv
                if "$push" in update_dict:
                    for pk, pv in update_dict["$push"].items():
                        if pk not in doc or doc[pk] is None:
                            doc[pk] = []
                        doc[pk].append(pv)
                self.docs[idx] = doc
                return copy.deepcopy(doc)
        return None

    def aggregate(self, pipeline):
        if hasattr(self, "custom_aggregate_results"):
            return iter(self.custom_aggregate_results)
        return iter([])

    def _filter(self, filter_dict):
        for doc in self.docs:
            matched = True
            for k, v in filter_dict.items():
                if k == "punch_in" and isinstance(v, dict):
                    d_pin = doc.get("punch_in")
                    if "$lte" in v and (d_pin is None or d_pin > v["$lte"]):
                        matched = False
                        break
                    if "$ne" in v and d_pin == v["$ne"]:
                        matched = False
                        break
                elif k == "date" and isinstance(v, dict):
                    doc_date = doc.get("date", "")
                    if "$gte" in v and doc_date < v["$gte"]:
                        matched = False
                        break
                    if "$lte" in v and doc_date > v["$lte"]:
                        matched = False
                        break
                    if "$regex" in v:
                        import re
                        if not re.search(v["$regex"], doc_date):
                            matched = False
                            break
                elif k == "joined_on" and isinstance(v, dict) and "$lte" in v:
                    if doc.get("joined_on", "") > v["$lte"]:
                        matched = False
                        break
                elif doc.get(k) != v:
                    matched = False
                    break
            if matched:
                yield doc

class MockDB:
    def __init__(self):
        self.employees = MockCollection("employees", unique_keys=["emp_code"])
        self.attendance_logs = MockCollection("attendance_logs", unique_keys=[("emp_code", "date")])
        class MockAdmin:
            def command(self, cmd_name, *args, **kwargs):
                return {"ok": 1.0}
        self.client = type("MockClient", (), {"admin": MockAdmin()})()

    def command(self, cmd_name, *args, **kwargs):
        return {
            "queryPlanner": {
                "winningPlan": {
                    "stage": "IXSCAN",
                    "indexName": "emp_code_1_date_1"
                }
            },
            "executionStats": {
                "executionSuccess": True,
                "nReturned": 1,
                "totalDocsExamined": 1
            }
        }

@pytest.fixture
def mock_db(monkeypatch):
    mdb = MockDB()
    import app.main
    monkeypatch.setattr(app.main, "get_db", lambda: mdb)
    return mdb

# -------------------------------------------------------------------------
# API Tests with Mock DB
# -------------------------------------------------------------------------

def test_health_ok(mock_db):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_health_unavailable(mock_db, monkeypatch):
    def bad_command(*args, **kwargs):
        raise Exception("Mongo connection error")
    mock_db.client.admin.command = bad_command
    res = client.get("/health")
    assert res.status_code == 503

def test_create_employee_success_and_duplicate(mock_db):
    body = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    res = client.post("/employees", json=body)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert "_id" not in data
    assert isinstance(data["created_at"], int)

    # Duplicate creation -> 409
    res_dup = client.post("/employees", json=body)
    assert res_dup.status_code == 409

def test_create_employee_validations(mock_db):
    # Invalid emp_code
    res1 = client.post("/employees", json={
        "emp_code": "EMP01",  # Less than 4 digits
        "name": "Test",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res1.status_code == 422

    # Same shift_start and shift_end
    res2 = client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Test",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01"
    })
    assert res2.status_code == 422

def test_employee_pagination_and_filter(mock_db):
    # Add multiple employees
    for i in range(1, 6):
        dept = "Engineering" if i <= 3 else "Sales"
        mock_db.employees.insert_one({
            "emp_code": f"EMP{i:04d}",
            "name": f"Name {i}",
            "email": f"emp{i}@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime.now(timezone.utc)
        })

    # List all
    res = client.get("/employees?page=1&page_size=2")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2

    # Filter by department
    res_dept = client.get("/employees?department=Sales")
    assert res_dept.status_code == 200
    data_dept = res_dept.json()
    assert data_dept["total"] == 2
    assert len(data_dept["items"]) == 2

    # Max page_size > 100
    res_invalid_page = client.get("/employees?page_size=101")
    assert res_invalid_page.status_code == 422

def test_punch_in_and_duplicate(mock_db):
    mock_db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc)
    })

    # Punch in 2026-07-06 09:28 IST (03:58 UTC) -> 1783307880000 ms
    tin_ms = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp()) * 1000
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": tin_ms,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-06"
    assert data["status"] == "PRESENT"
    assert data["late_minutes"] == 0
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert data["history"] == []
    assert "_id" not in data

    # Duplicate punch-in on same date -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": tin_ms + 60000
    })
    assert res_dup.status_code == 409

    # Unknown employee punch-in -> 404
    res_unknown = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9999",
        "punched_at": tin_ms
    })
    assert res_unknown.status_code == 404

def test_punch_in_invalid_timestamp(mock_db):
    # Boolean timestamp
    res_bool = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": True
    })
    assert res_bool.status_code == 422

    # String timestamp
    res_str = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": "1783307880000"
    })
    assert res_str.status_code == 422

def test_punch_out_and_concurrency(mock_db):
    mock_db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc)
    })
    tin_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": tin_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })

    # Valid punch out 18:35 IST (13:05 UTC)
    tout_ms = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp()) * 1000
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": tout_ms
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert data["history"] == []

    # Second punch out -> 409
    res2 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": tout_ms + 1000
    })
    assert res2.status_code == 409

def test_punch_out_invalid_times(mock_db):
    mock_db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc)
    })
    tin_utc = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": tin_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })

    # Punch out earlier than punch in -> 422
    tout_early = int(datetime(2026, 7, 6, 3, 59, 0, tzinfo=timezone.utc).timestamp()) * 1000
    res_early = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": tout_early})
    assert res_early.status_code == 422

    # Punch out > 24 hours after punch in -> 422
    tout_late = int((tin_utc + timedelta(hours=25)).timestamp()) * 1000
    res_late = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": tout_late})
    assert res_late.status_code == 422

def test_regularization_patch_and_history(mock_db):
    mock_db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc)
    })
    # Initial log: 10:05 IST to 18:30 IST
    tin_utc = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
    tout_utc = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": tin_utc,
        "punch_out": tout_utc,
        "work_hours": 8.42,
        "late_minutes": 35,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })

    # Regularize punch_in to 09:28 IST (03:58 UTC)
    new_tin_ms = int(datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc).timestamp()) * 1000
    patch_body = {
        "reason": "biometric glitch",
        "regularized_by": "hr.admin",
        "punch_in": new_tin_ms
    }
    res = client.patch("/attendance/EMP0001/2026-07-07", json=patch_body)
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 0
    assert data["work_hours"] == 9.03
    assert len(data["history"]) == 1
    hist = data["history"][0]
    assert hist["by"] == "hr.admin"
    assert hist["reason"] == "biometric glitch"
    assert "late_minutes" in hist["changes"]
    assert hist["changes"]["late_minutes"] == {"from": 35, "to": 0}

    # No changes -> 422
    res_nochange = client.patch("/attendance/EMP0001/2026-07-07", json=patch_body)
    assert res_nochange.status_code == 422

def test_regularization_absent_and_leave(mock_db):
    mock_db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc)
    })
    mock_db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-08",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 8, 13, 0, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })

    # Cannot supply punch times with LEAVE
    res_bad = client.patch("/attendance/EMP0001/2026-07-08", json={
        "reason": "applied for leave",
        "regularized_by": "manager",
        "status": "LEAVE",
        "punch_in": 1783307880000
    })
    assert res_bad.status_code == 422

    # Correctly regularize to LEAVE
    res_leave = client.patch("/attendance/EMP0001/2026-07-08", json={
        "reason": "approved sick leave",
        "regularized_by": "manager",
        "status": "LEAVE"
    })
    assert res_leave.status_code == 200
    data = res_leave.json()
    assert data["status"] == "LEAVE"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0

def test_attendance_list_filtering_and_sort(mock_db):
    res_invalid_dates = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res_invalid_dates.status_code == 422

def test_explain_validation(mock_db):
    # Invalid endpoint
    res = client.get("/admin/explain/non_existent")
    assert res.status_code == 422

    # Missing required month for employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert res2.status_code == 422

    # Valid attendance_list explain
    res3 = client.get("/admin/explain/attendance_list")
    assert res3.status_code == 200
    data = res3.json()
    assert data["endpoint"] == "attendance_list"
    assert "explain" in data

def test_employee_monthly_analytics(mock_db):
    # Invalid month format
    res1 = client.get("/analytics/employees/EMP0001/monthly?month=2026-7")
    assert res1.status_code == 422

    # Unknown employee
    res2 = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res2.status_code == 404

    # Valid employee joined mid-month (e.g. 2026-07-13, a Monday)
    mock_db.employees.insert_one({
        "emp_code": "EMP0002",
        "name": "Vikram",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-13",
        "created_at": datetime.now(timezone.utc)
    })
    # Total weekdays in July 2026 starting July 13: July 13-17 (5), July 20-24 (5), July 27-31 (5) = 15 working days
    mock_db.attendance_logs.custom_aggregate_results = [{
        "present_days": 1.0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0
    }]
    res3 = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res3.status_code == 200
    data = res3.json()
    assert data["emp_code"] == "EMP0002"
    assert data["working_days"] == 15
    assert data["present_days"] == 1.0
    # 1.0 / 15 * 100 = 6.67
    assert data["attendance_pct"] == 6.67

def test_department_summary_validation(mock_db):
    res_bad_month = client.get("/analytics/departments/summary?month=2026-13")
    assert res_bad_month.status_code == 422

def test_department_trend_validation(mock_db):
    # Department with no employees -> 404
    res_404 = client.get("/analytics/departments/NonExistent/trend?from=2026-07-01&to=2026-07-05")
    assert res_404.status_code == 404

    # Setup department
    mock_db.employees.insert_one({
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
        "created_at": datetime.now(timezone.utc)
    })

    # from > to -> 422
    res_reversed = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-05")
    assert res_reversed.status_code == 422

    # range > 92 days -> 422
    res_too_long = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res_too_long.status_code == 422

def is_real_mongo_available():
    try:
        from pymongo import MongoClient
        import os
        uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        c = MongoClient(uri, serverSelectionTimeoutMS=500)
        c.admin.command("ping")
        return True
    except Exception:
        return False

@pytest.mark.skipif(not is_real_mongo_available(), reason="Real MongoDB service is not running locally")
def test_real_mongodb_integration():
    """Integration test that only runs against a real live MongoDB instance."""
    from sample_seed import seed
    seed()
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
