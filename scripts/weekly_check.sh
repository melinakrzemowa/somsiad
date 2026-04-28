#!/usr/bin/env bash
# Weekly health check for somsiad. Runs on the Air via launchd
# (~/Library/LaunchAgents/com.melinakrzemowa.somsiad-weekly.plist).
# Composes a plain-text report and emails it via the same SMTP creds
# Alertmanager uses.
#
# Test manually:
#   /Users/kelu/services/monitoring.melinakrzemowa.pl/scripts/weekly_check.sh
#
# Logs land in /Users/kelu/services/monitoring.melinakrzemowa.pl/scripts/last_run.log

set -uo pipefail
DEPLOY_DIR="/Users/kelu/services/monitoring.melinakrzemowa.pl"
SMTP_HOST="h18.seohost.pl"
SMTP_PORT="465"
SMTP_USER="alert@melinakrzemowa.pl"
SMTP_PASSWORD_FILE="$DEPLOY_DIR/alertmanager/smtp_password"
MAIL_FROM="alert@melinakrzemowa.pl"
MAIL_TO="kelostrada@gmail.com"

# Make brew-installed binaries (docker, jq) reachable from launchd.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DATE_NOW="$(date '+%Y-%m-%d %H:%M %Z')"
DATE_TAG="$(date '+%Y-%m-%d')"
ONE_WEEK_AGO_S="$(($(date +%s) - 7 * 86400))"

# Run a curl in a throwaway container attached to the monitoring docker
# network so we can reach Tempo / Alertmanager by their internal hostnames.
mon_curl() {
  docker run --rm --network monitoringmelinakrzemowapl_default \
    curlimages/curl:8.10.1 -s --max-time 10 "$@"
}

report=()
warn=()
info() { report+=("$*"); }
flag() { warn+=("$*"); report+=("⚠ $*"); }

# ---------------------------------------------------------------------------
info "== somsiad weekly health — $DATE_NOW =="
info ""

# Container status (monitoring stack + apps)
info "[Containers]"
container_lines="$(docker ps --format '{{.Names}}|{{.Status}}|{{.Image}}' 2>/dev/null || true)"
for prefix in monitoringmelinakrzemowapl- instamelinakrzemowapl- sribiamelinakrzemowapl- mooncraft; do
  while IFS='|' read -r name status image; do
    [[ -z "$name" ]] && continue
    if [[ "$status" == Up* ]]; then
      info "  ✓ $name — $status"
    else
      flag "$name not Up: $status"
    fi
  done < <(grep -E "^$prefix" <<<"$container_lines" || true)
done

# Restart counts in past 7d (from `docker inspect`)
info ""
info "[Restarts in past 7d]"
restart_found=0
for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
  rc="$(docker inspect "$c" --format '{{.RestartCount}}' 2>/dev/null || echo 0)"
  if [[ "$rc" -gt 0 ]]; then
    info "  ⚠ $c: $rc total restarts"
    restart_found=1
  fi
done
[[ "$restart_found" -eq 0 ]] && info "  ✓ no container has restarted"

# Public probes (loopback through cloudflared)
info ""
info "[Public HTTPS probes]"
for url in \
  "https://insta.melinakrzemowa.pl" \
  "https://sribia.melinakrzemowa.pl/auth" \
  "https://mooncraft.melinakrzemowa.pl" \
  "https://monitoring.melinakrzemowa.pl"
do
  code="$(curl -sLo /dev/null -w '%{http_code}' --max-time 8 "$url" 2>/dev/null || echo 000)"
  if [[ "$code" =~ ^2|^3 ]]; then
    info "  ✓ $url → $code"
  else
    flag "$url returned $code"
  fi
done

# Tempo — confirm each service has at least one trace in the last 24h.
# An explicit start/end (unix seconds) is required for Tempo to scan all blocks
# rather than just the in-memory window.
info ""
info "[Traces (Tempo, last 24h)]"
tempo_start="$(($(date +%s) - 24 * 3600))"
tempo_end="$(date +%s)"
for svc in instagrain sribia; do
  body="$(mon_curl "http://tempo:3200/api/search?tags=service.name%3D${svc}&limit=1&start=${tempo_start}&end=${tempo_end}" 2>/dev/null || echo '{}')"
  count="$(echo "$body" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("traces",[])))' 2>/dev/null || echo 0)"
  if [[ "$count" -gt 0 ]]; then
    last_ns="$(echo "$body" | python3 -c 'import json,sys;t=json.load(sys.stdin)["traces"][0];print(t.get("startTimeUnixNano",0))' 2>/dev/null || echo 0)"
    age_s=$(( $(date +%s) - ${last_ns:0:10} ))
    info "  ✓ $svc — last trace ${age_s}s ago"
  else
    flag "$svc — no recent traces"
  fi
done
info "  · mooncraft — static site, no traces expected"

# Alertmanager — alerts that have fired in the last 7d (via Prometheus)
info ""
info "[Alerts fired (past 7d, via Prometheus ALERTS_FOR_STATE)]"
end_t="$(date +%s)"
start_t="$ONE_WEEK_AGO_S"
alerts_seen="$(mon_curl "http://prometheus:9090/api/v1/query_range?query=ALERTS%7Balertstate%3D%22firing%22%7D&start=${start_t}&end=${end_t}&step=300" 2>/dev/null \
  | python3 -c 'import json,sys
d=json.load(sys.stdin)
seen={}
for r in d.get("data",{}).get("result",[]):
  m=r["metric"]
  k=(m.get("alertname","?"), m.get("target",m.get("job","?")))
  vals=r.get("values",[])
  seen.setdefault(k,[]).append((vals[0][0] if vals else 0, vals[-1][0] if vals else 0, len(vals)))
for (a,t),items in sorted(seen.items()):
  total_eps=sum(i[2] for i in items)
  print(f"  ⚠ {a} on {t} — fired across {total_eps} eval points")
' 2>/dev/null || echo "  (could not query alerts)")"
if [[ -z "$alerts_seen" ]]; then
  info "  ✓ no alerts fired in the past 7 days"
else
  while IFS= read -r line; do info "$line"; done <<<"$alerts_seen"
fi

# Active silences
info ""
info "[Active silences in Alertmanager]"
silences_active="$(mon_curl 'http://alertmanager:9093/api/v2/silences?silenced=false' 2>/dev/null \
  | python3 -c 'import json,sys
for s in json.load(sys.stdin):
  if s.get("status",{}).get("state") == "active":
    m=", ".join(f"{x[\"name\"]}{\"=~\" if x[\"isRegex\"] else \"=\"}{x[\"value\"]}" for x in s.get("matchers",[]))
    print(f"  · until {s.get(\"endsAt\",\"?\")} — {m} ({s.get(\"comment\",\"\")})")
' 2>/dev/null)"
if [[ -z "$silences_active" ]]; then
  info "  ✓ none"
else
  while IFS= read -r line; do info "$line"; done <<<"$silences_active"
fi

# Host resources
info ""
info "[Air host]"
mem_total_pages="$(sysctl -n hw.memsize | awk '{print $1/16384}')"
mem_used_pct="$(vm_stat | awk '
  /Pages free/                                {free=$3+0}
  /Pages active/                              {active=$3+0}
  /Pages inactive/                            {inactive=$3+0}
  /Pages wired/                               {wired=$4+0}
  /Pages occupied by compressor/              {comp=$5+0}
  END {
    used=active+wired+comp
    total=free+active+inactive+wired+comp
    if(total>0) printf "%d", used*100/total
  }')"
disk="$(df -h / | awk 'NR==2 {printf "%s used / %s total (%s)", $3, $2, $5}')"
info "  Memory: ${mem_used_pct:-?}% used"
info "  Disk: $disk"
docker_stats="$(docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' 2>/dev/null \
  | awk '{cpu[$1]=$2; mem[$1]=$3" "$4" "$5}
         END {
           for (n in cpu) printf "    %-44s cpu %s mem %s\n", n, cpu[n], mem[n]
         }' | sort)"
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  info "$line"
done <<<"$docker_stats"

# node_exporter health
info ""
info "[node_exporter]"
ne_code="$(mon_curl -o /dev/null -w '%{http_code}' http://host.docker.internal:9100/metrics 2>/dev/null || echo 000)"
if [[ "$ne_code" == "200" ]]; then
  info "  ✓ responding (200)"
else
  flag "node_exporter not responding (got $ne_code)"
fi

# Summary
info ""
if [[ ${#warn[@]} -eq 0 ]]; then
  info "[Summary] ✓ everything green"
else
  info "[Summary] ${#warn[@]} item(s) to look at:"
  for w in "${warn[@]}"; do info "  - $w"; done
fi

REPORT="$(printf '%s\n' "${report[@]}")"

# Persist a copy locally for tail-after-the-fact debugging
echo "$REPORT" > "$DEPLOY_DIR/scripts/last_run.log"

# Email it
if [[ ${#warn[@]} -eq 0 ]]; then
  subject="[somsiad] Weekly OK — $DATE_TAG"
else
  subject="[somsiad] Weekly — ${#warn[@]} item(s) to check ($DATE_TAG)"
fi

# Build RFC 5322 message and pipe to curl --upload-file -. Uses SMTPS on 465.
{
  echo "From: somsiad <$MAIL_FROM>"
  echo "To: $MAIL_TO"
  echo "Subject: $subject"
  echo "Content-Type: text/plain; charset=utf-8"
  echo "Date: $(date -R)"
  echo
  echo "$REPORT"
} | curl --silent --show-error --ssl-reqd \
    --url "smtps://${SMTP_HOST}:${SMTP_PORT}" \
    --user "${SMTP_USER}:$(cat "$SMTP_PASSWORD_FILE")" \
    --mail-from "$MAIL_FROM" \
    --mail-rcpt "$MAIL_TO" \
    --upload-file - 2>&1 | tee -a "$DEPLOY_DIR/scripts/last_run.log"

exit 0
