#!/usr/bin/env bash
# Run the comparison against a deployed stack (infra/aws with -c apps=bench).
#
#   benchmarks/event-vs-web/run.sh rate  <stack-outputs.json> <results-dir>
#   benchmarks/event-vs-web/run.sh burst <stack-outputs.json> <results-dir> [concurrency]
#
# rate:  all targets in parallel; each runs a 30 s warm-up (not analysed), then
#        one minute each of /bench/fast at 50 rps, /bench/io (200 ms wait) at 50
#        and 90 rps, and /bench/cpu (50 ms) at 20 rps. GAP seconds (default 150)
#        separate profiles so Lambda's per-minute usage windows never span two.
# burst: one simultaneous burst of /bench/io per target; run it when every
#        target is cold (Web Function environments retire after ~5 minutes
#        idle; change the event function's configuration to discard its own).
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
phase="$1" outputs="$2" results="$3" concurrency="${4:-40}" gap="${GAP:-150}"
mkdir -p "$results"
targets() {
    python3 - "$outputs" <<'PY'
import json, re, sys
outputs = next(iter(json.load(open(sys.argv[1])).values()))
for key, url in sorted(outputs.items()):
    match = re.fullmatch(r"bench(event|c\d+)Url[0-9A-F]+", key)
    if match:
        print(f"bench-{match.group(1)} {url.rstrip('/')}")
PY
}
profiles() {
    local name="$1" url="$2" load="$here/load.py"
    python3 "$load" rate "$name" "$url" "/bench/io?ms=200" 30 30 "$results/warmup-$name.json" >/dev/null; sleep 20
    python3 "$load" rate "$name" "$url" /bench/fast 50 60 "$results/$name-fast.json"; sleep "$gap"
    python3 "$load" rate "$name" "$url" "/bench/io?ms=200" 50 60 "$results/$name-io.json"; sleep "$gap"
    python3 "$load" rate "$name" "$url" "/bench/io?ms=200" 90 60 "$results/$name-io90.json"; sleep "$gap"
    python3 "$load" rate "$name" "$url" "/bench/cpu?ms=50" 20 60 "$results/$name-cpu.json"
}
if [[ "$phase" == rate ]]; then
    while read -r name url; do profiles "$name" "$url" & done < <(targets)
    wait
else
    while read -r name url; do
        python3 "$here/load.py" burst "$name" "$url" "/bench/io?ms=200" "$concurrency" "$results/$name-burst.json" &
    done < <(targets)
    wait
fi
