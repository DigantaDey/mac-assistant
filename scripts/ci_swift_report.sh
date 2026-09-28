#!/usr/bin/env bash
# CI helper — make a Swift failure readable without downloading artifacts.
#
# The macOS job's raw log lives behind a URL that some networks can't fetch, so
# a broken build is turned into (a) check-run annotations and (b) a pull-request
# comment carrying the tail of the log. Both are readable from anywhere.
#
#   scripts/ci_swift_report.sh <logfile> <label>
set -uo pipefail

LOG="${1:?usage: ci_swift_report.sh <logfile> <label>}"
LABEL="${2:-Swift}"

if [ ! -s "$LOG" ]; then
  echo "::error title=$LABEL::no build log was captured"
  exit 0
fi

# 1 — annotations: show up on the checks tab and through the checks API.
#     The 40-line cap keeps a pathological build from flooding the UI.
grep -E "(error|warning): " "$LOG" | head -40 | while IFS= read -r line; do
  printf '::error title=%s::%s\n' "$LABEL" "${line//\%/%25}"
done

# 2 — a comment with the tail of the log, so nobody has to hunt for it.
if [ -n "${PR_NUMBER:-}" ] && command -v gh >/dev/null 2>&1; then
  {
    echo "### ⚠️ $LABEL failed"
    echo
    echo '```'
    tail -n 80 "$LOG"
    echo '```'
  } > /tmp/aura-ci-report.md
  gh pr comment "$PR_NUMBER" --body-file /tmp/aura-ci-report.md >/dev/null 2>&1 \
    || echo "::warning::$LABEL: couldn't post the log to the pull request"
fi
