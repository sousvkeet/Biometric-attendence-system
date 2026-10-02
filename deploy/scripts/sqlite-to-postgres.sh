#!/usr/bin/env bash
# Copy all portal data from the current database (usually SQLite) into an EMPTY PostgreSQL database.
#
#   deploy/scripts/sqlite-to-postgres.sh postgres://bioattend:PASSWORD@localhost:5432/bioattend
#
# The current database is only read. Stop the portal first so no punches arrive during the copy.
set -euo pipefail
TARGET="${1:?usage: $0 postgres://user:password@host:5432/database}"
cd "$(dirname "$0")/../.."
PY="${PYTHON:-.venv/bin/python}"
umask 077
DUMP="var/migration-$(date +%Y%m%d-%H%M%S).json"
trap 'rm -f "$DUMP"' EXIT

count_rows() {  # prints "app.model count" for every model
  "$PY" manage.py shell -v 0 -c "
from django.apps import apps
for m in apps.get_models():
    if m._meta.app_label in ('contenttypes', 'sessions', 'admin') or m._meta.model_name == 'permission': continue
    print(m._meta.label, m.objects.count())"
}

echo "1/5 Exporting the current data…"
"$PY" manage.py dumpdata --natural-foreign --natural-primary \
  -e contenttypes -e auth.permission -e sessions -e admin.logentry -o "$DUMP"
count_rows > "$DUMP.source-counts"; trap 'rm -f "$DUMP" "$DUMP.source-counts" "$DUMP.target-counts"' EXIT

echo "2/5 Creating tables in PostgreSQL…"
DATABASE_URL="$TARGET" "$PY" manage.py migrate --noinput -v 0

echo "3/5 Checking that the target is empty…"
EXISTING=$(DATABASE_URL="$TARGET" "$PY" manage.py shell -v 0 -c "
from employees.models import Employee; from attendance.models import AttendanceLog
print(Employee.objects.count() + AttendanceLog.objects.count())")
if [ "$EXISTING" != "0" ]; then
  echo "The target database already contains data – refusing to overwrite it." >&2; exit 1
fi

echo "4/5 Importing (this can take a few minutes)…"
DATABASE_URL="$TARGET" "$PY" manage.py loaddata "$DUMP" -v 0

echo "5/5 Verifying row counts…"
DATABASE_URL="$TARGET" count_rows > "$DUMP.target-counts"
if ! diff -u "$DUMP.source-counts" "$DUMP.target-counts"; then
  echo "Row counts differ – do NOT switch over; keep using the old database." >&2; exit 1
fi
echo
echo "Done: $(awk '{s+=$2} END {print s}' "$DUMP.target-counts") rows copied and verified."
echo "Next: put DATABASE_URL=$TARGET into .env (keep the old db.sqlite3 as a fallback) and start the portal."
