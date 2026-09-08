#!/usr/bin/env bash
# Full deterministic rebuild of the demo dataset.
#
# Resets the database, reloads reference data, seeds 66 days of synthetic fares
# with a festival surge, computes base prices, and builds the index at all three
# frequencies. Everything here is reproducible from a fixed RNG seed, so two runs
# produce byte-identical index values.
set -euo pipefail
cd "$(dirname "$0")/.."

SEED_START=${SEED_START:-2026-07-01}
SEED_END=${SEED_END:-2026-09-04}
SURGE_START=${SURGE_START:-2026-08-20}

echo "==> resetting schema"
docker compose exec -T postgres psql -U apix -d apix -q -v ON_ERROR_STOP=1 \
  < db/schema.sql 2>&1 | grep -viE 'notice|cascade|^detail' || true

echo "==> loading reference data"
python3 scripts/load_reference.py

echo "==> seeding ${SEED_START} .. ${SEED_END} (surge from ${SURGE_START})"
time python3 -m apix.cli seed \
  --start "$SEED_START" --end "$SEED_END" \
  --surge-start "$SURGE_START" --surge-days 8 --surge-peak 1.45

echo "==> base prices"
python3 -m apix.cli base

echo "==> index"
python3 -m apix.cli index

echo "==> done"
