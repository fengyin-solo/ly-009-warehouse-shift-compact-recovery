#!/bin/bash
set -euo pipefail

SOLUTION_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SOLUTION_DIR/reference.py" /app/warehouse_policy.py
chmod 0755 /app/warehouse_policy.py

cd /app
python3 warehouse_policy.py data/trace.json data/result.json data/simulation-ledger.jsonl
python3 warehouse_policy.py data/trace.json data/result.json data/simulation-ledger.jsonl
