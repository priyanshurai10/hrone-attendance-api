# Engineering Decisions

1. **Indexes.** We created four targeted indexes:
- `employees`: unique `{"emp_code": 1}` to guarantee identifier uniqueness; compound `{"department": 1, "joined_on": 1}` for department lookups and headcount filtering.
- `attendance_logs`: unique compound `{"emp_code": 1, "date": 1}` guaranteeing at most one log per employee per day and supporting monthly queries; compound `{"date": -1, "emp_code": 1}` for paginated listings sorted by date descending.
We considered indexing `punch_in` separately, but rejected it because queries address and filter records by calendar `date`, making an extra timestamp index redundant write overhead.

2. **Punch-in race.** Both concurrent requests find the employee and compute the same attendance date. Both execute `insert_one`. MongoDB's unique compound index on `(emp_code, date)` allows exactly one to succeed (HTTP 201). The second triggers a `DuplicateKeyError`, which our code catches to return HTTP 409.

3. **Ties.** We use MongoDB's `$setWindowFields` with competition ranking (`$rank`). Employees with equal late minutes receive identical ranks (e.g., 1, 2, 2, 4). Because the limit filters by `rank <= limit`, both tied employees at the cutoff are included, sorted by `emp_code` ascending.

4. **Headcount.** The aggregation pipeline starts from `employees` filtered by `joined_on <= month_end` and groups by department to compute `headcount: {"$sum": 1}` before `$lookup` joins attendance logs. Employees with zero logs are preserved.

5. **100x scale change.** I would pre-aggregate daily and monthly department statistics using scheduled background rollups so analytics queries read pre-computed summaries rather than executing multi-stage `$lookup` pipelines over millions of raw log documents.
