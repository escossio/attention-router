# Docker inventory in canonical ROC Zabbix

The canonical destination is the containerized ROC Zabbix. The host-installed
Zabbix is legacy/auxiliary. Discovery reads Docker's control plane through a
local allowlisted Unix socket; it does not ping container IPs or route Docker
CIDRs through the LAN. IP, subnet, gateway and MAC are observed attributes.
Compose `project/service/replica` is the entity key when labels exist; otherwise
the container name is used. Container ID is an instance attribute.

## Communication and authority

| Initiator | Destination | Protocol | Purpose | Authority |
| --- | --- | --- | --- | --- |
| ROC Zabbix server | host Agent2 | TCP/10050, existing host-reachable path | Inventory and stats polling | Agent2 `Server` allowlist |
| host Agent2 | local Docker view | Unix socket | Read inventory and stats | `zabbix` socket group |
| local Docker view | Docker daemon | root-owned Unix socket | Allowlisted GET/HEAD | Proxy route and method policy |
| Grafana ROC | ROC Zabbix web API | existing internal HTTP | Query collected items | Existing `roc-zabbix` datasource credentials |

No new container, network, static IP, L2 identity or LAN route is required.
The ROC server's current source address must be allowed by Agent2. Recheck that
allowlist if the ROC container is recreated: its source address is not reserved.
Network reachability grants no Docker mutation authority. The view runs as root
only to access the daemon socket, while Agent2 remains outside the `docker`
group. The view exposes only GET/HEAD for `_ping`, version, info, container list,
inspect, non-streaming stats, network list/inspect, image list and system df.
POST/PUT/PATCH/DELETE fail with 405; non-allowlisted GET fails with 403. Inspect
responses omit environment, mounts and sensitive-looking label keys.

## Deployment

Back up ROC hosts, templates, links, discovery rules, dashboards, Grafana
datasource provisioning and a scoped logical database dump privately before
import. Keep those backups and real inventory snapshots outside Git.

1. Install `zabbix_proxy.py` and `zabbix_inventory.py` under
   `/usr/local/lib/andy-docker-view/`, and the unit under
   `/etc/systemd/system/`. Install `docker_inventory.conf` and the effective
   `Plugins.Docker.Endpoint` line from `docker_plugin.conf` under Agent2's
   include directories. Back up existing files first. Do not add `zabbix` to
   the `docker` group.
2. Start `andy-zabbix-docker-view.service`, check its Unix socket permissions,
   and set Agent2's `Server` allowlist to the native loopback plus the verified
   ROC server source address. Validate Agent2 config and restart it. Confirm
   `agent.ping` from the ROC server network namespace before provisioning.
3. Copy `settings.example.json` to a mode-0600 private file outside Git.
   Fill the ROC API address, existing admin credentials or an `api_token`,
   existing host group ID and host-reachable Agent2 address. Then run:

   ```sh
   python3 provision_roc.py --settings /private/path/docker-inventory.json
   ```

   The script creates the template only if absent. It creates the named engine
   host only if absent and refuses to silently relink an existing host.
   This package does not create container hosts or interfaces at Docker IPs.
4. Wait for the four dependent LLD rules and the Agent2 Docker stats rule.
   Check container, network, attachment, port and running stats counts in the
   ROC API before disabling native Docker polling. All values stay associated
   with the logical engine host. A container with two networks has two
   attachment items. Published and merely exposed ports have separate fields.
5. Query CPU, memory, RX and TX through Grafana datasource UID `roc-zabbix`.
   Text items for state, attachment/IP and ports are accessible through the
   datasource's Zabbix API resource route. Verify rendering in the installed
   Grafana plugin before building dashboards; the measured plugin 6.8.0
   returned `Unknown error` in Explore Text even though its resource route
   returned the correct Zabbix item/history data. This is an open display
   limitation, not a missing ROC inventory item.

The native provisioner at `/root/agt-zabbix-docker-inventory/provision.py`
targets the local host Zabbix API/database. It is legacy and must not be used
for future Docker inventory changes. Use this ROC API provisioner instead.

## Rollback

Restore the backed-up Agent2 `Server` setting if ROC polling must stop. Disable
the ROC Docker template's six master items and five LLD rules via the ROC API,
or disable only the new engine host. Preserve the host, template and history.
Re-enable the same six master items and five discovery rules on the native
template if legacy collection is temporarily needed; their prior status/IDs
must come from the private pre-disable backup. Do not run `unlink and clear`,
drop either database, remove the ROC's other templates, or alter Docker networks.
The read-only proxy may remain for other Agent2 consumers; stop it only after
confirming none depend on it.

## Verification scope

Inspect one bridge, one macvlan, one stopped container, one published port,
every multi-network container and a running container's CPU/memory/RX/TX.
Verify Docker user/group/socket permissions, 405 mutation rejection, 403
unknown-route rejection, inspect projection and absence of Docker TCP listeners.
Use disposable synthetic fixtures for any future identity-change regression;
do not commit real container names, subnets, addresses or inventory snapshots.
