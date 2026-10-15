# REVIEW.md

Line numbers refer to the original starter `app/main.py`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | L22 `MongoClient(...)` | No `tz_aware=True`, so every datetime read back is naive. Mixing it with the IST-aware values used elsewhere raises `TypeError`. | `compute_overtime` on a stored record: "can't subtract offset-naive and offset-aware datetimes" -> 500. | `tz_aware=True`; all instants are UTC-aware and converted to/from epoch ms in two helpers. |
| 2 | L33-37 `compute_late_minutes` | Grace check is done on the *floored* minutes, so 09:40:01 gives 0 (should be 10). Uses the punch-in's own calendar day and zone, not IST. No handling of overnight shifts. No truncation to whole seconds. | Shift 09:30, punch 09:40:01 -> 0; punch 04:10Z (09:40 IST) is compared against 09:30 *UTC*. Overnight: punch 02:00 on the 7th for a 22:00 shift. | Compare in seconds (`> 600`), then floor to minutes since `shift_start`; the shift start comes from the IST attendance date (`shift_bounds`). |
| 3 | L40-41 `compute_work_hours` | `round()` on a float is banker's rounding and float-noisy. R4 wants half-up. | 9h 0m 18s = 9.005 h -> `round` gives 9.0, expected 9.01. | `Decimal` division with `ROUND_HALF_UP`. |
| 4 | L44-47 `compute_overtime` | No 30-minute minimum (R3). Shift end is always on the record's own date, so overnight shifts are wrong. | 18:45 out on an 18:30 shift -> 15 instead of 0. Overnight out 06:40 next day -> negative -> 0. | Minimum of 30 enforced; the end instant comes from `shift_bounds`, which rolls overnight shifts to the next day. |
| 5 | L53-60 `EmployeeIn` | No validation: empty `emp_code`, `"email": "x"`, `"shift_start": "9:30"`, `joined_on: "2026-02-30"` all accepted. | POST with a bad shift string, then punch-in 500s while parsing it. | Constrained types: non-empty strings, email pattern, `HH:MM` pattern, real `YYYY-MM-DD`. |
| 6 | L63-66 `PunchInIn` | `status` is any string (`ABSENT`, `"HOLIDAY"` accepted). `Optional[int]` coerces `"1751770000000"` and `1.0`; a seconds value is accepted. | POST `{"punched_at": 1751770000}` -> record dated 1970. | Status checked against PRESENT/WFH/ON_DUTY (422). `StrictInt` plus a range check rejects strings, floats, bools and seconds. |
| 7 | L72-74 `/health` | Always `ok`, even when MongoDB is down. | Stop MongoDB; `/health` still 200. | `ping`; 503 on failure. |
| 8 | L77-85 `create_employee` | Check-then-insert race (no unique index): two parallel POSTs can both pass `find_one`. `created_at` is `datetime.now()` (naive, server-local) and is returned as a datetime, not epoch ms. | Two concurrent identical POSTs -> two 201s. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. `created_at` is UTC, returned in ms. |
| 9 | L89-96 `list_employees` | `skip = page * page_size` skips the first page. `total` ignores the department filter. No sort. No bounds: `page=0` gives a negative skip -> 500. | `?page=1` returns rows 21+. `?department=Sales` shows the global total. | `(page-1)*page_size`; `count_documents(filter)`; sorted by `emp_code`; `Query(ge=1, le=100)` -> 422. |
| 10 | L101-113 `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` -> `TypeError` -> 500. | POST an unknown `emp_code`. | 404. |
| 11 | L102 | `if body.punched_at` treats `0` as "not given". `fromtimestamp(ms/1000)` uses server-local time and float division. Not truncated to whole seconds. | `punched_at: 0` silently means "now". Server in a non-IST zone shifts dates. | `is not None`; integer ms -> UTC datetime, truncated to seconds. |
| 12 | L103 | The date is the *server-local* date of the punch-in, not IST, and ignores overnight shifts (R1). | Punch at 23:00Z (04:30 IST next day) is filed on the wrong date. | `attendance_date()` converts to IST and applies the overnight rule. |
| 13 | L104-118 | Check-then-insert race: two parallel punch-ins both see "no record". Nothing in the database enforces one record per (emp, date). | Parallel requests -> two 201s and duplicate records. | Unique index on `(emp_code, date)` and `DuplicateKeyError` -> 409. |
| 14 | L119-121 | Response leaks `id` (stringified `_id`) and returns datetimes as ISO strings, not epoch ms. | Response has `"id": "66f..."` and `"punch_in": "2026-..."`. | `public_record()`: no id, instants in ms, legacy gaps defaulted. |
| 15 | L144-150 `list_attendance` | Loads every matching document and sorts and paginates in Python: O(n) memory, no index, and 100k records means a full scan per request. `total` from `len`. `_id` leaked. Dates and status not validated. Ties on `date` have no stable order. `page=0` gives odd slices. | Time it on 100k docs. `?date_from=garbage` returns empty instead of 422. | `find(...).sort(date desc, emp_code).skip().limit()` on an index; `count_documents` for the total; validation -> 422. |
| 16 | whole file | No indexes are created at startup, so even the "fast" paths scan. | `explain()` shows COLLSCAN. | `ensure_indexes()` in the lifespan hook; idempotent. |

## Looked at and decided it is fine
- `load_dotenv()` does not override real environment variables by default, which is the required priority.
- Endpoints are plain `def`, so FastAPI runs them in a thread pool and they do not block the event loop.
- `MongoClient` created at import time is lazy and thread-safe.
- `IST = UTC+05:30` is correct; India has no DST.
- Comparing `YYYY-MM-DD` strings with `$gte`/`$lte` is correct because they sort lexicographically.
- I changed the hard-coded `localhost` fallback for `MONGO_URI`: the app now refuses to start without it.
