# Engineering Decisions

1. **Indexes.** I created four indexes:
- `employees`: unique on `emp_code` to enforce uniqueness of the business identifier; compound on `department` and `joined_on` to speed up department filtering and monthly headcount lookups.
- `attendance_logs`: unique compound on `(emp_code, date)` to guarantee only one record exists per employee per calendar day and support monthly lookups; compound on `(date: -1, emp_code: 1)` to support sorted, paginated listing queries.
I considered an index on `punch_in`, but rejected it because queries address and filter logs by calendar date, so adding another timestamp index would introduce unnecessary write overhead without improving reads.

2. **Punch-in race.** When two identical requests arrive simultaneously, both look up the employee and derive the same attendance date. Both try to insert the document. Thanks to the unique compound index on `(emp_code, date)`, MongoDB lets only the first insert succeed (HTTP 201). The second fails with a `DuplicateKeyError`, which my code catches to return HTTP 409.

3. **Ties.** I used MongoDB's `$setWindowFields` with standard competition ranking (`$rank`). Tied employees receive the same rank (e.g. 1, 2, 2, 4). Since the limit filters by `rank <= limit`, both tied employees at the boundary are included, sorted by `emp_code` ascending for deterministic order.

4. **Headcount.** The department summary aggregation starts from the `employees` collection (filtered by `joined_on <= month_end`) and groups by department to count headcount first. Attendance logs are joined via `$lookup`, so employees who have zero attendance logs still count toward headcount.

5. **One thing I would change for 100x data.** I would pre-aggregate daily and monthly department stats using background workers into rollup summary collections, avoiding expensive runtime `$lookup` and `$unwind` aggregations across millions of raw logs.
