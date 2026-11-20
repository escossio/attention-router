#!/usr/bin/env bash
# AGT01 - Provisionamento controlado das VLANs de runtime / loopbacks /32.
# NAO reinicia contêineres, nao altera IP primario, NAT, firewall ou default route.
set -Eeuo pipefail
umask 022

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo 'ERRO: execute como root no AGT01.' >&2
  exit 1
fi
for binary in docker ip nsenter ssh systemctl awk grep mktemp; do
  command -v "$binary" >/dev/null || { echo "ERRO: falta $binary" >&2; exit 1; }
done

STAGE='andy-investigator-v0-net-stage'
NETWORK='andy-vlan219-investigator-stage'
MAP='/etc/andy/runtime-loopbacks-v1.map'
SYNC='/usr/local/sbin/andy-runtime-loopbacks-v1.sh'
UNIT='/etc/systemd/system/andy-runtime-loopbacks-v1.service'
TIMER='/etc/systemd/system/andy-runtime-loopbacks-v1.timer'
ROS_SSH=(ssh -n -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes -o HostName=10.45.0.1 -o HostKeyAlias=192.168.88.1 mikrotik-core)

# Nome do container | Docker network | IP de transporte | loopback exclusivo /32.
# O segundo ingress da VLAN 212 usa 200.0.0.19/32, pois .212 ja tem dono.
MAP_CONTENT=$(cat <<'MAPEOF'
andy-whatsapp-browser|andy-whatsapp-runtime_browser_wire|10.77.10.2|200.0.0.210
andy-whatsapp-transport|andy-whatsapp-runtime_transport_wire|10.77.10.10|200.0.0.211
attention-router-understanding-internal-ingress-5aed|andy-vlan212-ingress-stage|10.77.10.18|200.0.0.212
attention-router-neutral-integration-ingress-34ad1fc|andy-vlan212-ingress-stage|10.77.10.19|200.0.0.19
attention-router-live-flow-stage4-worker-1|andy-vlan213-worker-stage|10.77.10.26|200.0.0.213
attention-router-v03b-live-api-api-1|andy-vlan214-api-stage|10.77.10.34|200.0.0.214
attention-router-live-flow-stage4-db-1|andy-vlan215-db-stage|10.77.10.42|200.0.0.215
attention-router-artifact-understanding-worker-5aed|andy-vlan216-artifact-stage|10.77.10.50|200.0.0.216
tts-api-switcher|andy-vlan217-voice-stage|10.77.10.58|200.0.0.217
andy-investigator-v0-net-stage|andy-vlan219-investigator-stage|10.77.10.74|200.0.0.219
MAPEOF
)

ros() { "${ROS_SSH[@]}" "$1" </dev/null; }
fail() { echo "ERRO: $*" >&2; exit 1; }

# Confere os nove runtimes ANTES de qualquer escrita.
while IFS='|' read -r name net addr loop; do
  [[ "$name" == "$STAGE" ]] && continue
  docker inspect "$name" >/dev/null 2>&1 || fail "contêiner nao encontrado: $name"
  state=$(docker inspect -f '{{.State.Running}}' "$name")
  got=$(docker inspect -f '{{with index .NetworkSettings.Networks "'"$net"'"}}{{.IPAddress}}{{end}}' "$name")
  [[ "$state" == true && "$got" == "$addr" ]] || fail "mapeamento divergente: $name; nao alterado"
done <<< "$MAP_CONTENT"

# Preflight de rede reservada: IPAM /29, parent correto, sem anexos inesperados.
network_line=$(docker network inspect -f '{{.Driver}}|{{index .Options "parent"}}|{{(index .IPAM.Config 0).Subnet}}|{{(index .IPAM.Config 0).Gateway}}' "$NETWORK") || fail 'network 219 ausente'
[[ "$network_line" == 'macvlan|enp1s0.219|10.77.10.72/29|10.77.10.73' ]] || fail "network 219 inesperada: $network_line"
if docker inspect "$STAGE" >/dev/null 2>&1; then
  [[ "$(docker inspect -f '{{.Config.Image}}' "$STAGE")" == 'python:3.13-slim' ]] || fail 'container 219 ja existe com imagem diferente'
  [[ "$(docker inspect -f '{{index .Config.Labels "io.github.escossio.role"}}' "$STAGE")" == 'investigator-v0-network-stage' ]] || fail 'container 219 ja existe sem label de propriedade'
  [[ "$(docker inspect -f '{{.State.Running}}' "$STAGE")" == true ]] || fail 'container 219 existe, mas nao esta rodando; nao reiniciamos automaticamente'
  got=$(docker inspect -f '{{with index .NetworkSettings.Networks "andy-vlan219-investigator-stage"}}{{.IPAddress}}{{end}}' "$STAGE")
  [[ "$got" == 10.77.10.74 ]] || fail 'container 219 possui IP inesperado'
else
  occupied=$(docker network inspect -f '{{range .Containers}}{{.IPv4Address}} {{end}}' "$NETWORK")
  [[ -z "${occupied// /}" ]] || fail 'rede 219 contem outro container'
  docker image inspect python:3.13-slim >/dev/null 2>&1 || fail 'imagem python:3.13-slim nao esta no cache'
fi

# Preflight no roteador: se a rota ja existe, precisa ser exata; nunca a substituimos.
[[ "$(ros ':put [/interface vlan get [find where vlan-id=219] name]' | tr -d '\r\n')" == 'ANDY219-GW' ]] || fail 'RouterOS 219 inesperada'
[[ "$(ros ':put [:len [/ip address find where address="10.77.10.73/29"]]' | tr -d '\r\n')" == 1 ]] || fail 'gateway /29 nao encontrado'
while IFS='|' read -r name net addr loop; do
  cidr="$loop/32"
  count=$(ros ":put [:len [/ip route find where dst-address=\"$cidr\"]]" | tr -d '\r\n') || fail "falha consultando $cidr"
  [[ "$count" == 0 || "$count" == 1 ]] || fail "rotas duplicadas para $cidr"
  if [[ "$count" == 1 ]]; then
    via=$(ros ":put [/ip route get [find where dst-address=\"$cidr\"] gateway]" | tr -d '\r\n') || fail "falha na rota $cidr"
    [[ "$via" == "$addr" ]] || fail "rota existente $cidr aponta para $via, esperado $addr"
  fi
done <<< "$MAP_CONTENT"

echo 'PREFLIGHT OK: 9 contêineres preservados, 219 validada e rotas sem conflitos.'

# Nao sobrescrever politicas preexistentes que pertencam a outro operador.
if [[ -f "$MAP" ]]; then
  [[ "$(cat "$MAP")" == "$MAP_CONTENT" ]] || fail "mapeamento existente divergente: $MAP"
fi
if [[ -f "$SYNC" ]] && ! grep -Fq 'ANDY_RUNTIME_LOOPBACKS_V1' "$SYNC"; then
  fail "script de reconciliacao preexistente sem marcador: $SYNC"
fi
if [[ -f "$UNIT" ]] && ! grep -Fq 'andy-runtime-loopbacks-v1.sh' "$UNIT"; then
  fail "service systemd preexistente nao corresponde: $UNIT"
fi
if [[ -f "$TIMER" ]] && ! grep -Fq 'andy-runtime-loopbacks-v1.service' "$TIMER"; then
  fail "timer systemd preexistente nao corresponde: $TIMER"
fi

# Cria somente o container de REDE, sem motor/Agents SDK e sem privilégios de rede.
if ! docker inspect "$STAGE" >/dev/null 2>&1; then
  docker run -d --name "$STAGE" \
    --label io.github.escossio.role=investigator-v0-network-stage \
    --network "$NETWORK" --ip 10.77.10.74 \
    --restart unless-stopped --init --cap-drop ALL \
    --security-opt no-new-privileges --read-only \
    --pids-limit 32 --memory 128m --cpus 0.2 \
    python:3.13-slim python -B -c 'import time; time.sleep(10**9)' >/dev/null
  echo 'CONTAINER 219: criado como staging inerte de rede.'
else
  echo 'CONTAINER 219: staging existente validado, sem restart.'
fi

# Script de reconciliacao persistente; somente allowlist; nao altera o IP principal.
install -d -m 0755 /etc/andy /usr/local/sbin
if [[ -f "$MAP" ]]; then
  [[ "$(cat "$MAP")" == "$MAP_CONTENT" ]] || fail "mapeamento preexistente divergente em $MAP"
else
  printf '%s\n' "$MAP_CONTENT" > "$MAP"
  chmod 0644 "$MAP"
fi

cat > "$SYNC" <<'SYNCEOF'
#!/usr/bin/env bash
# ANDY_RUNTIME_LOOPBACKS_V1 - allowlist de containers do runtime, sem restart.
set -Eeuo pipefail
while IFS='|' read -r name net addr loop; do
  [[ -n "$name" ]] || continue
  if ! docker inspect "$name" >/dev/null 2>&1; then
    echo "AGUARDANDO: $name ainda nao presente"; continue
  fi
  [[ "$(docker inspect -f '{{.State.Running}}' "$name")" == true ]] || {
    echo "AGUARDANDO: $name parado"; continue;
  }
  actual=$(docker inspect -f '{{with index .NetworkSettings.Networks "'"$net"'"}}{{.IPAddress}}{{end}}' "$name")
  [[ "$actual" == "$addr" ]] || { echo "CONFLITO: $name IP=$actual esperado=$addr" >&2; exit 1; }
  pid=$(docker inspect -f '{{.State.Pid}}' "$name")
  [[ "$pid" =~ ^[1-9][0-9]+$ ]] || { echo "CONFLITO: PID $name" >&2; exit 1; }
  local_addresses=$(nsenter -t "$pid" -n ip -4 -o addr show dev lo)
  if printf '%s\n' "$local_addresses" | grep -Fq "inet $loop/32 "; then
    continue
  fi
  if printf '%s\n' "$local_addresses" | grep -Eq 'inet 200\.0\.0\.'; then
    echo "CONFLITO: ja existe outro 200.0.0.x no loopback de $name" >&2
    exit 1
  fi
  nsenter -t "$pid" -n ip addr add "$loop/32" dev lo
  echo "LOOPBACK_OK $name $loop/32 via $addr"
done < /etc/andy/runtime-loopbacks-v1.map
SYNCEOF
chmod 0755 "$SYNC"

cat > "$UNIT" <<'UNITEOF'
[Unit]
Description=Andy runtime loopback /32 reconciliation (allowlisted Docker namespaces)
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/andy-runtime-loopbacks-v1.sh
UNITEOF
cat > "$TIMER" <<'TIMEREOF'
[Unit]
Description=Retry Andy runtime loopbacks after container recreation
[Timer]
OnBootSec=20s
OnUnitActiveSec=60s
Unit=andy-runtime-loopbacks-v1.service
[Install]
WantedBy=timers.target
TIMEREOF
systemctl daemon-reload
# Aplica os 10 loopbacks de uma vez e verifica a saida.
"$SYNC"
systemctl enable --now andy-runtime-loopbacks-v1.timer >/dev/null

# Rotas individuais e idempotentes; nenhuma rota /24, NAT ou alteração de default route.
while IFS='|' read -r name net addr loop; do
  cidr="$loop/32"
  count=$(ros ":put [:len [/ip route find where dst-address=\"$cidr\"]]" | tr -d '\r\n')
  if [[ "$count" == 0 ]]; then
    ros "/ip route add dst-address=$cidr gateway=$addr comment=ANDY-RUNTIME-LO-$name" >/dev/null
    echo "ROTA_OK $cidr via $addr"
  else
    echo "ROTA_EXISTENTE $cidr via $addr"
  fi
done <<< "$MAP_CONTENT"

# Verificacao final: mapeamento do loopback e rotas host exatas.
"$SYNC"
route_verified=0
while IFS='|' read -r name net addr loop; do
  cidr="$loop/32"
  [[ "$(ros ":put [:len [/ip route find where dst-address=\"$cidr\"]]" | tr -d '\r\n')" == 1 ]] || fail "rota nao confirmada $cidr"
  via=$(ros ":put [/ip route get [find where dst-address=\"$cidr\"] gateway]" | tr -d '\r\n')
  [[ "$via" == "$addr" ]] || fail "rota $cidr com next-hop divergente: $via"
  ((route_verified+=1))
done <<< "$MAP_CONTENT"
[[ "$route_verified" -eq 10 ]] || fail "foram verificadas somente $route_verified/10 rotas; aborto sem alegar sucesso"
echo 'SUCESSO: 10 loopbacks /32 e 10 rotas individuais conferidos; nenhum runtime reiniciado.'
echo 'NOTA: 219 e apenas staging de REDE, ainda nao e o motor Agents SDK.'
