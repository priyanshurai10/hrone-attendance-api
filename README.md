# Employee Attendance & Analytics API

A fast, production-ready REST API built with FastAPI and MongoDB for managing employee attendance logs, punch events, HR regularizations, and business analytics.

## Tech Stack & Requirements
- **Python:** 3.11+
- **Database:** MongoDB 6.0+
- **Frameworks:** FastAPI, PyMongo, Pydantic v2, Uvicorn

---

## Setup & Installation

### 1. Create and Activate Virtual Environment
```bash
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1

# Linux / macOS
python -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env` or set system environment variables:
```bash
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 4. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

---

## Running the Application

Start the development server:
```bash
uvicorn app.main:app --port 8000
```

- **Interactive API Documentation (Swagger UI):** [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Check Endpoint:** [http://localhost:8000/health](http://localhost:8000/health)

---

## Running Automated Tests

Run the complete test suite using pytest:
```bash
pytest -v
```

The test suite includes comprehensive unit tests and mock-database API tests that run standalone without requiring a local MongoDB server, as well as live MongoDB integration tests that execute when a real instance is reachable.

---

## Key Features & Endpoints

| Category | Method & Path | Description |
|---|---|---|
| **System** | `GET /health` | Database ping check (200 on success, 503 if unavailable) |
| **Employees** | `POST /employees` | Register employee with unique code, department, and shifts |
| | `GET /employees` | Paginated listing with exact department filtering and sorting |
| **Attendance** | `POST /attendance/punch-in` | Punch in with overnight shift date deduction (R1) & late calculation (R2) |
| | `POST /attendance/punch-out` | Punch out with work duration (R4), overtime (R3) & half-day (R5) |
| | `GET /attendance` | Filtered attendance logs with database pagination and sorting |
| | `PATCH /attendance/{emp_code}/{date}` | Regularization with strict validation and audit history logging |
| **Analytics** | `GET /analytics/employees/{emp_code}/monthly` | Monthly attendance summary with mid-month joiner handling |
| | `GET /analytics/departments/summary` | Department summary with zero-log headcount preservation |
| | `GET /analytics/leaderboard/late` | Competition ranking (`$rank`: 1, 2, 2, 4) with tie preservation |
| | `GET /analytics/departments/{dept}/trend` | Daily trend series with gap-filling and 7-day moving average |
| **Admin** | `GET /admin/explain/{endpoint}` | Execution plan inspection for performance verification |

---

## Known Limitations
- Attendance analytics assume a standard Monday–Friday working week (R7) without a custom company holiday calendar.
- Analytics calculations rely on MongoDB 6.0+ pipeline operators (`$setWindowFields`, `$dateAdd`).
