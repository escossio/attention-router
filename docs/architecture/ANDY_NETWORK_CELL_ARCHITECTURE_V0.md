# Andy Network Cell Architecture V0

**Status:** FUTURE ARCHITECTURE / NOT IMPLEMENTED  
**Purpose:** preserve a future network-and-compute scaling direction for Andy without changing the current runtime.

## 1. Motivation

Andy is moving toward a model where a container is not merely a software packaging unit. It can become a movable unit of capacity.

If one functional area develops excess latency, queue pressure or CPU saturation, the platform should be able to add another instance of only that service instead of scaling an entire machine or rebuilding the network around it.

The network must therefore support future growth in which workloads can be split across hosts, racks, clusters or sites without losing logical service identity.

This document records a proposed **Network Cell** architecture to support that direction.

## 2. Core idea

A **Network Cell** is an independently routable infrastructure domain.

Each cell contains:

- one or more workload networks/VLANs;
- containers, VMs and services belonging to that domain;
- a dedicated virtual routing boundary;
- observability and routing metadata;
- explicit import/export policy.

Conceptually:

```text
SITE / HOST / CLUSTER
        |
   +----+-----+
   |  VyOS    |
   | Cell GW  |
   +----+-----+
        |
   +----+------------------+
   |          |            |
 VLAN A     VLAN B        VLAN C
   |          |            |
  API       Workers        DB
```

The cell gateway is responsible for routing the networks inside its domain and for exchanging only the routes that policy allows with other cells.

## 3. Preferred routing boundary

The preferred future gateway is **VyOS in a dedicated VM**, not a normal application container.

Reasoning:

- VyOS needs predictable control of interfaces and routing tables;
- BGP/OSPF/IS-IS, VRFs, firewall and policy are kernel/network-plane responsibilities;
- a VM creates a cleaner failure and privilege boundary than a regular workload container;
- upgrades and rollback are easier to isolate from the application runtime.

Containerized routing can be evaluated in the future, but it is not the preferred baseline for a cell gateway.

## 4. Separation of underlay and service routing

The design should distinguish two planes.

### 4.1 Underlay

The underlay answers:

> How does one cell router reach another cell router?

Candidates:

- OSPF;
- IS-IS;
- bounded static routing during an early stage.

### 4.2 Cell/service routing

The service routing plane answers:

> Which workload prefixes exist behind each cell?

Preferred candidate:

- BGP between cell gateways.

Example:

```text
CELL-A announces:
10.77.10.16/29  ingress
10.77.10.24/29  workers
10.77.10.32/29  api
10.77.10.40/29  database

CELL-B announces:
10.78.10.16/29
10.78.10.24/29
10.78.10.32/29
...
```

A new cell joins the architecture by establishing its routing relationships and advertising only its authorized prefixes.

## 5. Routing is not authorization

A critical invariant:

> Route knowledge does not imply communication authority.

BGP/OSPF/IS-IS may make a destination reachable, but security policy still decides whether traffic is allowed.

Every cell must have explicit:

- route export policy;
- route import policy;
- firewall/security policy;
- ownership of advertised prefixes;
- observability of accepted and rejected communication.

No cell should be allowed to advertise arbitrary prefixes without policy.

## 6. Logical identity versus network identity

Service identity must not be bound to an IP address.

Example:

```text
Andy Worker
├── cell-a / worker-1
├── cell-a / worker-2
└── cell-b / worker-1
```

The logical service remains **Andy Worker**.

Attributes that may change per instance include:

- container ID;
- VM ID;
- IP;
- MAC;
- VLAN;
- cell;
- host;
- rack/site.

The observability stack should preserve both:

1. logical service identity;
2. concrete network identity at the time of a flow.

## 7. Scaling model

A future scheduler may use infrastructure and network telemetry when deciding where to place or execute work.

Possible inputs:

- CPU;
- memory;
- queue depth;
- service latency;
- network RTT;
- dependency locality;
- cell health;
- route availability;
- cost;
- failure-domain preference.

This document does **not** define the scheduler algorithm.

It only preserves the architecture needed to make such scheduling possible.

## 8. Cell growth model

A new sector should not require redesigning the global network.

Preferred process:

1. create a new cell;
2. provision its VyOS VM;
3. provision workload VLANs/subnets;
4. establish underlay reachability;
5. establish BGP relationships;
6. apply import/export policy;
7. register the cell in observability;
8. deploy workload instances.

The core network should not need to know every individual container route.

It should learn summarized/policy-approved cell prefixes.

## 9. Relationship with the current MikroTik

The current MikroTik may remain part of the physical/core underlay.

The proposed architecture reduces the need for the MikroTik to know every internal workload VLAN and container subnet individually.

A future model may look like:

```text
Physical/Core Network
        |
     MikroTik
        |
   +----+----------------+
   |                     |
 VyOS Cell-A          VyOS Cell-B
   |                     |
 workloads              workloads
```

The exact long-term role of MikroTik is intentionally left open.

## 10. Observability requirements

The ROC observability stack should eventually model:

```text
site
  -> cell
    -> router
      -> VLAN/network
        -> logical service
          -> instance
```

For a real flow, observability should be able to show:

- source logical entity;
- destination logical entity;
- source cell;
- destination cell;
- source IP/port;
- destination IP/port;
- protocol;
- route path when available;
- trace/span relationship;
- latency;
- policy decision;
- container/VM instance.

This connects directly to the existing Docker inventory + OpenTelemetry + future network evidence design.

## 11. Relationship with Docker inventory and OTel

Three truths should remain distinct:

### Inventory truth
Docker/VM control plane says **what exists**.

### Trace truth
OpenTelemetry says **which application operation called which operation**.

### Network truth
Kernel/network evidence says **which connection actually happened**.

The future Flow Map should reconcile these sources instead of treating any one source as complete truth.

## 12. Failure domains

A cell should be treated as a bounded failure domain.

Desired properties:

- failure of one cell does not invalidate the routing state of every other cell;
- a cell can be drained independently;
- workloads can be replicated into another cell;
- routes can be withdrawn when a cell is unhealthy;
- observability can distinguish service failure from cell/network failure.

## 13. Security principles

- default-deny between cells unless explicitly allowed;
- route advertisement is policy-controlled;
- routing protocol authentication should be considered;
- management plane separated from workload plane;
- no secrets in route metadata;
- no service identity derived only from IP;
- observability cannot itself become an authorization mechanism;
- route leaks must be detectable;
- unexpected advertised prefixes must trigger alarms.

## 14. Potential protocol direction

This proposal intentionally avoids final protocol selection.

Current candidates:

- **BGP** for inter-cell prefix exchange;
- **OSPF or IS-IS** for underlay reachability;
- VRFs where tenant/domain isolation requires separate routing tables.

A later design must choose protocol details from measured requirements rather than convention alone.

## 15. Future observability examples

The ROC could eventually show:

```text
SITE: AGT01
|
+-- CELL: RUNTIME-A
|   +-- VyOS
|   +-- API
|   +-- Worker-1
|   +-- Worker-2
|   +-- DB
|
+-- CELL: OBSERVABILITY
    +-- VyOS
    +-- Zabbix
    +-- Grafana
    +-- Tempo
    +-- OTel Collector
```

And a trace could include topology context such as:

```text
CELL-EDGE
  -> CELL-RUNTIME
  -> VLAN 213
  -> worker
  -> TCP/5432
  -> database
```

## 16. Design invariants

1. Each cell is an independent routing/failure domain.
2. Each cell has an explicit gateway/router boundary.
3. VyOS is preferred as a dedicated VM for the cell gateway.
4. Internal workload prefixes should not require manual core-route growth.
5. Inter-cell route exchange is dynamic and policy-controlled.
6. Underlay reachability and workload-prefix exchange are separate concerns.
7. IP is an instance attribute, not service identity.
8. Containers/services may scale within or across cells.
9. Every cell must be visible to ROC observability.
10. Every advertised prefix must have known origin/owner.
11. Route visibility does not grant application authority.
12. Cells must support independent drain, rollback and isolation.
13. The architecture must support multiple physical hosts/sites without redesign.
14. Observability must preserve logical and concrete network identity simultaneously.
15. Future scheduling may consume network cost/latency but may not infer authority from routing.

## 17. Non-goals of V0

This document does not authorize implementation.

It does not:

- replace the current MikroTik;
- deploy VyOS;
- create BGP sessions;
- create OSPF/IS-IS;
- create VRFs;
- change current VLANs;
- change Docker networks;
- move production workloads;
- change security policy;
- change current container placement.

## 18. Preconditions before implementation

Before implementing a Network Cell design, complete:

1. current runtime cleanup/containerization;
2. canonical ROC inventory;
3. OTel coverage audit and remediation;
4. network-edge evidence/correlation;
5. Golden Trace;
6. real latency and dependency measurements;
7. cell boundary proposal based on actual traffic.

The first real cell design should be informed by observed traffic, not guessed.

## 19. Open questions

A later ADR/design must decide:

- exact VyOS deployment model;
- HA pair versus single router per cell;
- underlay protocol;
- BGP ASN strategy;
- iBGP versus eBGP between cells;
- route reflectors if scale requires them;
- summarization policy;
- VRF model;
- service discovery interaction;
- health-driven route withdrawal;
- scheduler integration;
- multi-site/WAN behavior;
- observability of route changes;
- failure testing and convergence targets.

## 20. Status

**Preserved as a future architecture proposal.**

Do not treat this document as current production architecture.

When the project is ready, this proposal should be promoted into a dedicated implementation ADR only after the current observability work provides enough evidence to define the first real cell boundary.
