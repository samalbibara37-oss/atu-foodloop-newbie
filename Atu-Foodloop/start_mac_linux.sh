#!/bin/sh
cd "$(dirname "$0")" || exit 1
exec python3 server.py --demo --db instance/demo.sqlite3 --host 0.0.0.0
