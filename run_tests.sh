#!/usr/bin/env bash
# run_tests.sh — run from the repo root. Matches this layout:
#
#   Options-strategy-dashboard/
#     ├── app/            server.py, strategy_*.py, dashboard.html, ...
#     ├── tests/           test_*.py, test_*.js
#     └── run_tests.sh
#
#   ./run_tests.sh          fast tier: offline, deterministic, every push
#   ./run_tests.sh --live   adds the opt-in network test (real yfinance)
#
# The test files themselves do bare `import server`, `import market_cache`,
# etc — they were written assuming the module they test sits next to them.
# Since app/ and tests/ are now separate directories, PYTHONPATH is what
# bridges that: it's set to app/ below so those imports resolve no matter
# what directory this script is invoked from.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/app${PYTHONPATH:+:$PYTHONPATH}"
export DASHBOARD_HTML="$ROOT/app/dashboard.html"   # test_calendar_live.js reads this

LIVE=0
[[ "${1:-}" == "--live" ]] && LIVE=1

declare -a RESULTS

run_py() {
  local name="$1"; shift
  echo "───────────────────────────────────────────────────────────"
  echo "▶ $name"
  if python3 "tests/$name" "$@"; then
    RESULTS+=("$name|PASS|")
  else
    RESULTS+=("$name|FAIL|exit $?")
  fi
}

run_pytest() {
  local name="$1"
  echo "───────────────────────────────────────────────────────────"
  echo "▶ $name (pytest)"
  if python3 -m pytest "tests/$name" -q; then
    RESULTS+=("$name|PASS|")
  else
    RESULTS+=("$name|FAIL|pytest exit $?")
  fi
}

run_js() {
  local name="$1"
  echo "───────────────────────────────────────────────────────────"
  if ! command -v node >/dev/null 2>&1; then
    echo "▶ $name — SKIPPED (node not on PATH)"
    RESULTS+=("$name|SKIP|node not installed")
    return
  fi
  echo "▶ $name (node)"
  if node "tests/$name"; then
    RESULTS+=("$name|PASS|")
  else
    RESULTS+=("$name|FAIL|exit $?")
  fi
}

echo "═══════════════════════════════════════════════════════════"
echo "  FAST TIER — offline, deterministic, no network"
echo "═══════════════════════════════════════════════════════════"

run_py  test_lifecycle.py
run_py  test_market_cache.py
run_py  test_cache_integration.py
run_py  test_cache_scope.py
run_py  test_fundamentals.py            # offline unless --live is passed to IT, not us
run_py  test_provider.py
run_py  test_upsert_state.py
run_pytest test_server_put_selling.py
run_js  test_calendar_payoff.js
run_js  test_calendar_live.js

# test_calendar_range.js / test_calendar_range2.js are deliberately excluded:
# exploration scripts with no assertions and no failing exit path — they
# always exit 0. Keep them for history, don't run them as gates.

if [[ $LIVE -eq 1 ]]; then
  echo
  echo "═══════════════════════════════════════════════════════════"
  echo "  LIVE TIER — hits real yfinance, needs network"
  echo "═══════════════════════════════════════════════════════════"
  run_py test_fundamentals.py --live
fi

echo
echo "═══════════════════════════════════════════════════════════"
echo "  SUMMARY"
echo "═══════════════════════════════════════════════════════════"
fail_count=0
skip_count=0
for r in "${RESULTS[@]}"; do
  IFS='|' read -r name status detail <<< "$r"
  case "$status" in
    PASS) printf "  %-6s %s\n" "PASS" "$name" ;;
    FAIL) printf "  %-6s %s  (%s)\n" "FAIL" "$name" "$detail"; ((fail_count++)) ;;
    SKIP) printf "  %-6s %s  (%s)\n" "SKIP" "$name" "$detail"; ((skip_count++)) ;;
  esac
done
echo "───────────────────────────────────────────────────────────"
echo "  ${#RESULTS[@]} run, $fail_count failed, $skip_count skipped"
echo "═══════════════════════════════════════════════════════════"

[[ $fail_count -eq 0 ]]
