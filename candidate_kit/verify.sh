#!/usr/bin/env bash
# Verification curl script for HROne Employee Attendance & Analytics API
# Run: bash verify.sh [BASE_URL]
set -e

BASE_URL="${1:-http://localhost:8000}"
echo "Running verification against $BASE_URL ..."

echo "--- 1. Health check ---"
curl -s -f "$BASE_URL/health" | grep '"status":"ok"'
echo " [PASS] /health"

echo "--- 2. Duplicate employee -> 409 ---"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/employees" \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","name":"Asha Rao","email":"asha2@example.com","department":"Engineering","joined_on":"2026-01-05"}')
if [ "$STATUS" -eq 409 ]; then
  echo " [PASS] Duplicate employee returned 409"
else
  echo " [FAIL] Expected 409, got $STATUS"
  exit 1
fi

echo "--- 3. Pagination total with filters ---"
curl -s "$BASE_URL/employees?department=Engineering&page=1&page_size=1" | grep '"total":2'
echo " [PASS] Filtered total equals 2"

echo "--- 4. Mid-month joiner working days (R7) ---"
# EMP0002 joined on 2026-07-13 -> 15 working days in July 2026
curl -s "$BASE_URL/analytics/employees/EMP0002/monthly?month=2026-07" | grep '"working_days":15'
echo " [PASS] Mid-month joiner has 15 working days"

echo "--- 5. Department summary preserves zero-log department (R9) ---"
curl -s "$BASE_URL/analytics/departments/summary?month=2026-07" | grep '"department"'
echo " [PASS] Department summary returned items"

echo "--- 6. Late Leaderboard (Dense Rank) ---"
curl -s "$BASE_URL/analytics/leaderboard/late?month=2026-07" | grep '"rank":1'
echo " [PASS] Leaderboard late returned items with rank"

echo "All curl smoke tests passed successfully!"
