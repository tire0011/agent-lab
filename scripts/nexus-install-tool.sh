#!/usr/bin/env bash
# nexus-install-tool.sh <package>
# Installs one Ubuntu package for Nexus' install_tool operation. Runs as root
# through a single sudoers rule (setup-nexus-permissions.sh copies it to
# /usr/local/sbin/nexus-install-tool). Only apt with the sources root already
# configured; a package, or any dependency it pulls in, matching a pattern in
# the denylist is refused. No denylist means no installs.

set -euo pipefail

DENYLIST="${NEXUS_TOOL_DENYLIST:-/etc/agent-lab/tool-denylist}"
pkg="${1:-}"

if [[ $# -ne 1 || ! "$pkg" =~ ^[a-z0-9][a-z0-9+.-]{1,62}$ ]]; then
    echo "refused: exactly one package name is allowed" >&2
    exit 2
fi
if [[ ! -r "$DENYLIST" ]]; then
    echo "refused: no denylist at $DENYLIST" >&2
    exit 2
fi

denied() {
    local name="$1" pattern
    while read -r pattern; do
        [[ -z "$pattern" || "$pattern" == \#* ]] && continue
        # shellcheck disable=SC2053
        [[ "$name" == $pattern ]] && return 0
    done < "$DENYLIST"
    return 1
}

plan="$(DEBIAN_FRONTEND=noninteractive apt-get -s -q install --no-install-recommends -- "$pkg" 2>&1)" || {
    echo "$plan" | tail -5 >&2
    exit 1
}
for name in $(echo "$plan" | awk '/^Inst /{print $2}'); do
    if denied "$name"; then
        echo "refused: $name is on the denylist" >&2
        logger -t nexus-install-tool "refused $pkg ($name denied)" 2>/dev/null || true
        exit 3
    fi
done

DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends -- "$pkg" 2>&1 | tail -5
logger -t nexus-install-tool "installed $pkg" 2>/dev/null || true
echo "installed: $pkg"
