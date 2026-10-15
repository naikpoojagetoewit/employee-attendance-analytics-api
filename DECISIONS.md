# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`, `(department, emp_code)` for the filtered, sorted list and the trend, and `joined_on` for the headcount match. `attendance_logs`: unique `(emp_code, date)`, which makes punch-in race-safe and serves per-employee queries; `(date desc, emp_code)` for the default list order and the leaderboard's date range; `(status, date desc, emp_code)` for status filters. I rejected a separate `emp_code`-only index, because the unique compound index already covers it.

2. **Punch-in race.** Both requests compute the same date and try `insert_one`. The unique index lets one insert win; the other raises `DuplicateKeyError`, which I turn into a 409. There is no read-then-write check, so there is nothing to race.

3. **Ties.** `$rank` gives standard competition ranking, so ties share a rank and the next one is skipped (1, 2, 2, 4). `limit` is applied after ranking by keeping every row with `rank <= limit`. A tie at the cutoff is therefore returned in full, and the response can hold more than `limit` rows. Rows are ordered by late minutes, then `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks logs up per employee, so someone with no logs still gives one row.

5. **100x data.** Pre-aggregate monthly totals per employee, updated at punch time, instead of scanning raw logs for every report.
