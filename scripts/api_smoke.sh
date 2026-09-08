#!/usr/bin/env bash
# Smoke-test every endpoint against a running API.
#
#     make api        # in one terminal
#     make api-check  # in another
#
# Checks status codes only. Behaviour is covered by tests/test_api.py; this is
# for confirming a deployment is wired up correctly.
set -uo pipefail
BASE=${BASE:-http://localhost:8000}
fail=0

check() {
  local path="$1" expect="${2:-200}"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")
  if [ "$code" = "$expect" ]; then
    printf '  \033[32m✓\033[0m %-52s %s\n' "$path" "$code"
  else
    printf '  \033[31m✗\033[0m %-52s %s (expected %s)\n' "$path" "$code" "$expect"
    fail=1
  fi
}

echo "APIx API smoke test against $BASE"
check /health
check /
check /openapi.json
check /docs
check /v1/index/latest
check /v1/index/latest?frequency=weekly
check /v1/index/latest?frequency=hourly 422
check /v1/index/series
check "/v1/index/series?format=csv"
check "/v1/index/series?start=2026-12-01&end=2026-01-01" 400
check /v1/index/routes
check /v1/index/routes/BOM-DEL
check /v1/index/routes/XXX-YYY 404
check /v1/index/cells
check /v1/index/leadtime
check /v1/methodology
check /v1/compliance
check /v1/compliance/requests

echo
[ $fail -eq 0 ] && echo "all endpoints OK" || echo "some endpoints FAILED"
exit $fail
