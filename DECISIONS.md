# DECISIONS.md

### 1. Indexes & ESR Rationale
We created the following indexes at startup via FastAPI's `lifespan` handler:
* `employees`:
  * `{"emp_code": 1}` (**Unique**): Enforces business key uniqueness and serves $O(1)$ lookups in CRUD and aggregation `$lookup` stages.
  * `{"department": 1, "emp_code": 1}`: Adheres to the **Equality-Sort-Range (ESR)** rule for `GET /employees?department=...` — exact equality on `department`, followed by index-order streaming for `emp_code` without in-memory sorting.
  * `{"department": 1, "joined_on": 1}` & `{"joined_on": 1}`: Powers the initial `$match` in department summaries and trends for headcount filtering (`joined_on <= period_end`).
  * *Rejected*: `{"department": 1, "name": 1, "emp_code": 1}`. Rejected because `name` is neither filtered nor sorted in the API; adding unneeded fields degrades write throughput and increases index working set size.
* `attendance_logs`:
  * `{"emp_code": 1, "date": 1}` (**Unique Compound**): Guarantees at most one log per employee per attendance day (Rule R1) and backs point lookups in `PATCH /attendance/{emp_code}/{date}`.
  * `{"date": -1, "emp_code": 1}`: Direct index scan for `GET /attendance` pagination (`date` desc, `emp_code` asc) without an in-memory sort stage.
  * `{"emp_code": 1, "punch_in": -1}`: Powers `POST /attendance/punch-out` finding the latest open record (`punch_in <= punched_at`) in reverse order.
  * `{"date": 1, "late_minutes": 1}`: Powers `GET /analytics/leaderboard/late` by matching the date range and `late_minutes > 0` via `IXSCAN`.
  * `{"status": 1, "date": -1}`: Supports status-filtered attendance listing.

### 2. Punch-in Race Conditions
* **The Failure Mode Before**: Two identical punch-in requests arrive simultaneously. Both execute `find_one({"emp_code": ..., "date": ...})`. Because neither document exists yet, both checks return `None`. Both callers execute `insert_one()`, inserting duplicate documents for the same day or corrupting state.
* **How It Is Solved**: We removed the application-level `find_one` check and enforce a compound unique index on `(emp_code, 1), (date, 1)`. When concurrent requests call `insert_one(doc)`, MongoDB's WiredTiger storage engine serializes the index insertion at the page lock level. The first request commits with HTTP 201. The second encounters a duplicate key collision at the index boundary, raising `pymongo.errors.DuplicateKeyError`. Our code catches this error and returns HTTP 409 (`already punched in for this date`).
* **Concurrency on Punch-Out & Regularization**: For punch-out, atomic `update_one({"_id": record["_id"], "punch_out": None}, ...)` ensures that if two requests attempt to close the same record, only one matches `punch_out == None`; the second matches 0 documents and receives HTTP 409. For `PATCH`, we use optimistic locking on the history array length (`{"history": {"$size": len(current_history)}}`) so concurrent edits cannot silently overwrite audit history.

### 3. Ties & Leaderboard Ranking
* **Rank Semantics**: We implement `$setWindowFields` with `$denseRank` (1, 2, 2, 3) per the explicit specification ("Rank with DENSE RANK semantics: tied employees get the same rank, and the next rank is not skipped. Use $setWindowFields with $denseRank (MongoDB 5.0+)"). We note that `openapi.yaml` line 315 specifies standard competition ranking (`$rank`, 1, 2, 2, 4); switching between the two is a one-operator change (`$denseRank` vs `$rank`) inside the `$setWindowFields` stage. Under both operators, tied employees share the identical rank value.
* **Limit at the Cutoff**: Rule R10 and the OpenAPI contract specify that `limit` applies **after** ranking (`rank <= limit`). If `limit=2` and two employees tie for second place (ranks 1, 2, 2), both tied employees qualify under `rank <= 2`. Consequently, the leaderboard returns all 3 employees rather than truncating at 2.
* **Deterministic Display**: Tied rows are ordered by `emp_code` ascending (`_id: 1` in `$sort`) as the deterministic tie-breaker.

### 4. Zero-Log Headcount in Department Summary
* **Why Start From Employees**: If the aggregation started from `attendance_logs`, employees who worked zero days or took unlogged absences in that month would never appear in the pipeline, corrupting department headcount.
* **Implementation**: We begin the aggregation pipeline on `employees`:
  1. `$match: {"joined_on": {"$lte": month_end}}` (and department if specified) filters out employees who have not yet joined.
  2. `$lookup` against `attendance_logs` fetches records within `[month_start, month_end]`.
  3. `$unwind: {"path": "$logs", "preserveNullAndEmptyArrays": True}` preserves employees with zero logs as outer-join documents (`logs: null`).
  4. `$group` by `department` computes `headcount: {"$size": {"$addToSet": "$emp_code"}}`. Employees with zero records contribute 0 to present days, but their distinct `emp_code` is counted in department headcount.

### 5. 100x Scaling (10M+ Records)
* **What Breaks First**: On-the-fly cross-collection `$lookup` in `department_summary` and the `$densify` + `$setWindowFields` pipeline in `department_trend`. Scanning and joining millions of documents in memory per request will cause MongoDB to exceed the 100MB aggregation RAM limit and introduce second-scale latency.
* **Architectural Evolution**:
  1. **Sharding Key**: Shard `attendance_logs` on a compound shard key `{"emp_code": "hashed", "date": 1}`. This distributes write load evenly across shards while keeping all attendance records for a single employee on a single chunk.
  2. **Pre-aggregated Daily Rollups**: Replace on-demand cross-collection aggregation with an event-driven worker (listening to MongoDB Change Streams) that maintains a `department_daily_summary` collection. Querying monthly analytics reduces from scanning 10M records to summing 30 pre-aggregated daily documents.
  3. **Read Replicas & Materialized Views**: Direct all analytical queries to dedicated read replicas (`ReadPreference.SECONDARY_PREFERRED`), isolating analytical load from punch-in/out transactional writes. Use periodic `$merge` materialized views for moving-average trends.
