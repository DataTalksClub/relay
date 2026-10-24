#!/bin/sh
set -eu

# Every image role (web, worker, scheduler, dispatcher) inherits this gate.
# A failed migration exits here before any new application process starts.
python /app/manage.py migrate_before_start
exec "$@"
