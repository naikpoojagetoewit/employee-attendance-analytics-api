# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
`MONGO_URI` and `MONGO_DB` are required; real environment variables win over `.env`. Indexes are created at
startup (idempotent). `GET /health` returns 503 if MongoDB does not answer.

## Tests
```bash
pip install pytest httpx mongomock
USE_MONGOMOCK=1 pytest tests/      # in-memory fake: rules, CRUD, races (skips the aggregation tests)
pytest tests/                      # real MongoDB 6.0+ via MONGO_URI/MONGO_DB: also analytics + explain (wipes that DB!)
```

## Notes
- Part A findings: `REVIEW.md`. Part C answers: `DECISIONS.md`.
- Instants are epoch milliseconds in the API and UTC BSON dates in MongoDB (client uses `tz_aware=True`).
- Analytics and explain use aggregation pipelines (`$lookup`, `$setWindowFields`, `$range`) and need MongoDB 6.0+.
