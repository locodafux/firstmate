#!/usr/bin/env bash
# tests/fm-chat-bridge.test.sh - run the chat bridge's unit tests (fake PostgREST server and fake fm-inbox, no network).
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

command -v python3 >/dev/null 2>&1 || { echo "skip: python3 not found"; exit 0; }

out=$(python3 "$ROOT/tests/fm-chat-bridge-unit.py" 2>&1) || { echo "$out"; fail "chat bridge unit tests"; }
pass "chat bridge unit tests"

printf 'all chat bridge cases passed\n'
