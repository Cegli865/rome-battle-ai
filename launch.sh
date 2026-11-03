#!/bin/bash
# Steam launch options for Rome Remastered:
#     /full/path/to/rome-battle-ai/launch.sh %command%
#
# The mod is loaded ONLY if the AI server is listening (python -m rome_ai.server / rome_ai.manual)
# or probe mode is requested. Otherwise the game starts exactly as without this script.
#   ROME_AI_PROBE=1 can also be enabled with:  touch hook/PROBE  (in this folder)
HERE="$(cd "$(dirname "$0")" && pwd)"
LIB="$HERE/hook/librome_ai.so"
PORT="${ROME_AI_PORT:-7777}"

if [ -f "$HERE/hook/PROBE" ]; then
    export ROME_AI_PROBE=1
fi
server_up() {  # without connecting to it (ss), falling back to a probe connection
    if command -v ss >/dev/null; then ss -Hltn "sport = :$PORT" | grep -q .; else (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; fi
}
if [ -f "$LIB" ] && { [ "${ROME_AI_PROBE:-0}" = 1 ] || server_up; }; then
    export LD_PRELOAD="$LIB${LD_PRELOAD:+:$LD_PRELOAD}"
    export ROME_AI_DIR="$HERE/hook"   # hook.log and probe/ go here; if Steam mounts it read-only: ~/.local/share/rome-ai
    echo "rome-ai: mod enabled ($LIB)" >&2
fi
exec "$@"
