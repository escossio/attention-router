# ROC-NETWORK / OSI

Operational path: MikroTik → SNMPv3 → Zabbix ROC → Grafana ROC → Andy Ops / MGMT.
This package instruments the existing ROC stack while the application remains
degraded. It does not repair application containers or change network topology.

## Deployment

Identify the actual ROC containers and Compose labels before using any endpoint.
Use the containerized Zabbix API; the host's legacy Zabbix is outside this package.
Back up the existing template, host, dashboard provisioning and panel files privately.

1. Determine the collector's actual source at the router after Docker NAT. Preserve
   existing SNMP communities. Configure a dedicated SNMPv3 identity, authPriv,
   strongest supported authentication, AES, read-access=yes, write-access=no,
   restricted to that collector address. Store independently generated passphrases
   in root-only files outside Git. The measured RouterOS release supports SHA1
   and AES for its SNMP agent; SHA2 was rejected by the agent's CLI.
2. Copy `settings.example.json` to a private operator-owned mode-0600 file, replace
   placeholders with the actual ROC API, host, SNMP target and empirically verified
   physical interface index. `token_file` can replace username/password when an
   appropriate private API token already exists. Reuse existing valid administrative
   access; never bootstrap passwords or access the Zabbix database.
3. Run `python3 provision.py --settings PRIVATE_SETTINGS --export-template template.json`.
   The API creates or updates only the named network template/host. SNMP credentials
   are host secret macros and are excluded from the template export. The exporter
   excludes superseded partial definitions and private template macros.
4. Copy `roc-network-osi.json` into the existing Grafana ROC dashboard provider's
   mounted directory. Reuse datasource UID `roc-zabbix`. Apply the one-property
   `grafana-datasource.patch` to its existing provisioning, increasing its version
   above the current API value if necessary, then reload datasource
   provisioning through Grafana's administrative API. A one-minute metadata cache
   keeps LLD/item changes visible; browser refresh still reads Zabbix history.
   Do not create a second datasource/provider/server or edit the E2E dashboard.
5. Install `topology.py`, `provision.py`, `snmp.js`, `discovery.js`, `neighbor.js` under
   `/opt/andy-network-osi/`. Configure `topology-settings.example.json` privately as
   `/etc/andy-ops-network/topology-settings.json` mode 0600. Install and enable the
   supplied systemd service/timer. It reads VLANs, bridge members and Ethernet link
   metadata over the existing dedicated SSH key and sends a trapper snapshot to
   an explicit ROC collector address every five minutes. It does not query through
   Andy Ops or add an HTTP service. Failure leaves the last snapshot to expire.
6. Deploy the small panel changes in `ops/provisioning/andy-ops-panel/`, preserving
   any extra live tabs. Set `ANDY_OPS_NETWORK_OSI_URL` in its private environment to
   the authenticated dashboard URL, then restart only the panel service. The iframe
   loads on tab selection and its origin alone is added to the panel's frame CSP.

`compose.network-osi.yaml` records the separately authorized attachment of the
ROC Zabbix server to the two existing networks. The first rollout uses only
`docker network connect andy-roc-edge andy-roc-zabbix-server`; keep its monitoring
network/IP and container identity. Do not run Compose up during this rollout.
The override records the configuration for a future planned ROC deployment.
Before using it, inspect both existing networks and abort if either is absent.

## Collection and states

Zabbix 7.4 native asynchronous master walks feed local dependent items and LLD:

| Master / operation | Interval | Scope |
| --- | --- | --- |
| `andy.discovery.raw` | 5m | Seven interface/type/name/alias/address columns |
| `andy.metrics.raw` | 30s | Ten status/speed/counter columns |
| `andy.neighbors.raw` | 30s | Two legacy ARP columns: MAC and type |
| Native ICMP simple checks | 10s | Three packets for each discovered endpoint; shared loss/RTT checks |
| Calculated health/freshness | 10s | Local Zabbix calculations |
| RouterOS topology snapshot | 5m | Three read-only SSH commands |
| Grafana refresh | 10s | Existing Zabbix datasource/history |

This is two logical SNMP walks per counter cycle, plus one slower discovery walk.
Each walk uses multiple bounded GETBULK requests; its logical item count is not its
packet count. Measure packets privately at the actual egress when reporting load.
The master walks never traverse the whole SNMP tree. Individual dependent items,
LLD, calculations and browsers cause no extra SNMP queries. Native multi-OID `get[]`
did not produce a usable OID map in the measured version, so it is not deployed.

Standard OID roots used:

- IF-MIB `.1.3.6.1.2.1.2.2.1`: ifIndex `.1`, ifDescr `.2`, type `.3`, admin `.7`, oper `.8`,
  RX discards `.13`, RX errors `.14`, TX discards `.19`, TX errors `.20`.
- ifXTable `.1.3.6.1.2.1.31.1.1.1`: name `.1`, HC RX `.6`, HC TX `.10`,
  high speed `.15`, alias `.18`.
- IP-MIB `.1.3.6.1.2.1.4.20.1`: address→interface `.2`, netmask `.3`.
- Legacy ARP `.1.3.6.1.2.1.4.22.1`: physical address `.2`, entry type `.4`.

Initial evidence confirmed that ifStack and ipNetToPhysical/state were not exposed.
BRIDGE-MIB was inspected but is not polled; enterprise OIDs are unnecessary here.
Duplex remains UNKNOWN when RouterOS's Ethernet monitor does not expose it.
An additional dependent LLD discovers type-6 Ethernet interfaces. Their counters
come from the same metrics master; the physical table shows the declared trunk
and real VLAN-parent members from the topology snapshot, with no extra SNMP poll.

LLD accepts `^ANDY21[0-7]-GW$`, joins the measured interface index and /29 gateway,
and derives the subnet and second usable endpoint under this runtime's addressing
convention. It verifies the real VLAN ID/parent against the RouterOS snapshot.
Unmapped gateways are excluded by the endpoint filter so an empty simple-check
target cannot accidentally ping the monitoring host. No functional names are invented.

Legacy neighbor classification: valid dynamic/static MAC → COMPLETE; empty/all-zero
MAC or invalid-entry type → INCOMPLETE; missing row in a successful walk → ABSENT;
partial or malformed data → UNKNOWN. Failure of a master does not become ABSENT
or PASS. Displayed OPER/ARP/ICMP states are calculated locally with freshness limits
of 90s/90s/40s. Health expires too; topology expires after one hour. RTT zero from
an unreachable endpoint is displayed as NO RESPONSE, not successful latency.

## Causality and embedding

Trigger dependencies: `L1_PARENT_DOWN` → `L2_DOMAIN_DOWN` →
`L3_NEIGHBOR_INCOMPLETE` → `L3_ENDPOINT_UNREACHABLE`. Telemetry loss is a separate
UNKNOWN problem. Physical/domain/neighbor/ICMP triggers require three samples;
downstream expressions also require three healthy parent samples. An existing
parent problem suppresses downstream alarms while the measurements remain visible.
`L2_TOPOLOGY_MISMATCH` records an unverified parent association. A declared trunk
must not be described as the proven cause of endpoint failures when the actual
VLAN parent belongs to another bridge; the dashboard exposes both facts.
Physical path health is UNKNOWN until that association is verified, even when
the declared trunk has carrier. A topology mismatch also suppresses downstream
alarms while all eight failing neighbor/ICMP measurements stay visible.

Dependencies can retain already open events from a previous partial implementation.
Back up and disable superseded definitions, then reconcile stale expressions before
reinstating dependencies. Preserve their history and the runtime's failing measurements.

Test the existing public HTTPS route before changing embedding. If its framing
headers block MGMT, the supplied Apache fragment removes contradictory upstream
X-Frame-Options at the existing HTTPS proxy and sets a restrictive `frame-ancestors`
list containing only approved MGMT origins. Replace the placeholder privately,
validate with `apache2ctl configtest`, then reload Apache. The measured public
MGMT/Grafana routes share a browser site, so existing SameSite=Lax sessions work
without changing cookies. Keep Apache Basic Auth and Grafana authentication. No anonymous access,
credential URL, token injection or new proxy is used. Grafana's private backend
continues its existing framing policy. A different-site deployment must diagnose
its own cookie behavior before making any additional change.

## Validation and rollback

Run only focused local checks:

```
node test_discovery.cjs
python3 -m unittest discover -s . -p 'test_*.py'
python3 -m py_compile *.py
```

Compare the live dashboard to read-only RouterOS interface/address/ARP output.
Prove all eight LLD rows, ICMP loss/RTT, dependencies, current problems and the
authenticated lazy MGMT iframe in a fresh headless browser. Keep screenshots,
inventory, secrets, API evidence, packet headers and backups private outside Git.
Compare E2E dashboard content/hash and all ROC/application container IDs/start times.

Rollback the edge attachment with only
`docker network disconnect andy-roc-edge andy-roc-zabbix-server`. Disable the
topology timer, remove only this dashboard file, restore backed-up panel/private
configuration and Apache headers, and disable this network host/template's polling.
Do not reset the database or repair/restart application containers during rollback.
