#!/usr/bin/env bash
# Non-mutating candidate smoke; does not alter Zabbix host interface or secrets.
set -euo pipefail
proxy=andy-roc-docker-view
agent=andy-roc-agent2
server=andy-roc-zabbix-server

for svc in "$proxy" "$agent" "$server"; do
  docker inspect "$svc" >/dev/null
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$proxy")" == healthy ]]
[[ "$(docker inspect -f '{{.State.Status}}' "$agent")" == running ]]
[[ "$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$agent")" == "{}" ]]
[[ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$agent")" == true ]]
[[ "$(docker exec "$server" zabbix_get -s roc-agent2 -p 10050 -k agent.ping -t 10)" == 1 ]]
for key in   andy.docker.inventory   andy.docker.inventory.containers   andy.docker.inventory.container_labels   andy.docker.inventory.networks   andy.docker.inventory.attachments   andy.docker.inventory.ports
do
  value="$(docker exec "$server" zabbix_get -s roc-agent2 -p 10050 -k "$key" -t 20)"
  [[ -n "$value" ]]
  printf '%s' "$value" | python3 -c 'import json,sys;assert isinstance(json.load(sys.stdin),(list,dict))'
  printf 'ROC_ENGINE_ITEM=%s JSON=PASS\n' "$key"
done
docker exec "$server" zabbix_get -s roc-agent2 -p 10050 -k 'docker.containers.discovery[false]' -t 12 | python3 -c 'import json,sys;assert isinstance(json.load(sys.stdin),(dict,list))'
printf 'ZABBIX_PLUGIN_DOCKER_LLD=PASS\n'
printf 'ROC_AGENT2_ISOLATED_PASS\n'
