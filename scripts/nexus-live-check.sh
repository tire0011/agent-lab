#!/usr/bin/env bash
# Run pytest for one branch in a throwaway worktree on the Nexus VM.
#
#   nexus-live-check.sh <branch> [pytest args...]
#
# Fetches <branch>, checks it out detached into a scratch worktree, prints
# what is under test (commit, llama-server state, free memory), runs
# `python -m pytest <args>` there and removes the worktree again - also when
# a test fails or the run is interrupted. Without pytest args it runs all of
# live_tests/. The exit code is pytest's.
#
# Environment: AGENT_REPO (default /agent), PYTHON (default
# $AGENT_REPO/.venv/bin/python).

set -u

if [ $# -lt 1 ]; then
    echo "usage: $0 <branch> [pytest args...]" >&2
    exit 2
fi

BRANCH=$1
shift
[ $# -gt 0 ] || set -- live_tests

REPO=${AGENT_REPO:-/agent}
PYTHON=${PYTHON:-$REPO/.venv/bin/python}
WT=$(mktemp -d /tmp/nexus-live-check.XXXXXX)

cleanup() {
    cd /
    git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"
    git -C "$REPO" worktree prune >/dev/null 2>&1 || true
}
trap cleanup EXIT

git -C "$REPO" fetch --quiet origin "$BRANCH" || { echo "ERROR: cannot fetch $BRANCH" >&2; exit 2; }
git -C "$REPO" worktree add --quiet --detach "$WT" FETCH_HEAD || exit 2

echo "=== under test ==="
echo "branch:       $BRANCH"
echo "commit:       $(git -C "$WT" --no-pager log --oneline -1)"
echo "llama-server: $(systemctl is-active llama-server 2>/dev/null || echo unknown)"
awk '/^(MemAvailable|SwapFree):/ {printf "%-13s %d MiB\n", $1, $2 / 1024}' /proc/meminfo
echo "pytest args:  $*"
echo

cd "$WT" || exit 2
"$PYTHON" -m pytest -p no:cacheprovider -v --tb=short -rfE "$@"
