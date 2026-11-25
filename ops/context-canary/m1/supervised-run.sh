#!/usr/bin/env bash
# One independent lab execution with durable identity and a read-only attachment.
set -euo pipefail
umask 077

command=${1:?launch, status or supervise}
root=${2:?private experimental root}
id=${3:?run UUID}
[[ $id =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
[[ $root = /srv/andy-ci/work/* && $(realpath -e "$root") = "$root" && -d "$root" && -O "$root" ]]
control="$root/.control/$id"
case "$command" in
    status)
        [[ $# = 3 ]]
        for file in requested started finished; do
            if [[ -f "$control/$file" ]]; then cat "$control/$file"; fi
        done
        ;;
    launch|supervise)
        [[ $# = 8 ]]
        stage=$4 sha=$5 waves=$6 port=$7 runner=$8
        [[ $stage =~ ^/srv/andy-ci/work/andy-m1-e5\.[A-Za-z0-9]+$ && $sha =~ ^[0-9a-f]{40}$ ]]
        [[ $waves =~ ^(1|6)$ && $port =~ ^[1-9][0-9]{3,4}$ && $port -le 65535 ]]
        [[ -f "$runner" && "$runner" = "$root/run-ci02.sh" && -O "$runner" ]]
        if [[ $command = launch ]]; then
            mkdir -p "$root/.control"
            if ! mkdir "$control" 2>/dev/null; then
                [[ -f "$control/requested" ]] || exit 75
                grep -Fxq "SHA=$sha WAVES=$waves OTLP_PORT=$port" "$control/requested" || exit 65
                exec bash "$0" status "$root" "$id"
            fi
            printf 'RUN_ID=%s\nSHA=%s WAVES=%s OTLP_PORT=%s\n' "$id" "$sha" "$waves" "$port" > "$control/requested.tmp"
            mv "$control/requested.tmp" "$control/requested"
            nohup setsid bash "$0" supervise "$root" "$id" "$stage" "$sha" "$waves" "$port" "$runner" \
                > "$control/worker.log" 2>&1 </dev/null &
        else
            exec 8>"$control/supervisor.lock"
            flock -n -E 75 8
            [[ ! -e "$control/started" ]]
            printf 'PID=%s\nSTARTED=%s\n' "$$" "$(date -u +%FT%T.%NZ)" > "$control/started.tmp"
            mv "$control/started.tmp" "$control/started"
            export M1_RUN_ROOT="$root" M1_RUN_ID="$id"
            export M1_OTLP_ENDPOINT="http://127.0.0.1:$port/v1/traces"
            result=0
            bash "$runner" "$stage" "$sha" "$waves" instrumented 8>&- || result=$?
            printf 'EXIT=%s\nFINISHED=%s\n' "$result" "$(date -u +%FT%T.%NZ)" > "$control/finished.tmp"
            mv "$control/finished.tmp" "$control/finished"
            exit "$result"
        fi
        ;;
    *) exit 64 ;;
esac
