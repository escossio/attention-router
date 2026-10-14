#!/bin/sh
set -eu
: "${GOACCESS_WS_URL:?external WebSocket URL is required}"
: "${GOACCESS_LOG_FILE:=/logs/attention-router-client-api-access.log}"
: "${GOACCESS_REPORT_FILE:=/reports/index.html}"

test -r "$GOACCESS_LOG_FILE"
test -w /reports
# The current Apache log is metadata-only: ISO timestamp, route, method,
# status, duration micros, remote IP. Keep this conversion identical to
# attention-goaccess-feed.sh on the original host.
tail -n +1 -F "$GOACCESS_LOG_FILE" |
gawk '
NF >= 6 {
    timestamp=$1
    route=$2
    method=$3
    status=$4
    duration=$5
    ip=$6

    date=substr(timestamp,1,10)
    time=substr(timestamp,12,8)
    if (route !~ /^\//) route="/" route

    printf "%s %s %s \"%s %s HTTP/1.1\" %s %s\n",
        ip, date, time, method, route, status, duration
    fflush()
}' |
goaccess - \
    --log-format='%h %d %t "%r" %s %D' \
    --date-format='%Y-%m-%d' \
    --time-format='%H:%M:%S' \
    --real-time-html \
    --addr=0.0.0.0 \
    --port=7890 \
    --ws-url="$GOACCESS_WS_URL" \
    --no-progress \
    -o "$GOACCESS_REPORT_FILE"
