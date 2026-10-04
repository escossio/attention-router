# Container Communication Graph & Network Placement Invariant V1

## Purpose

A container is an execution boundary, not an automatic network identity.

Before any new container/service is attached to a runtime network, the architecture
must first describe **who communicates with whom**, in which direction, through
which protocol/trust boundary, and why that reachability is required.

Network placement and addressing are consequences of that communication graph.

This document does not assign private deployment addresses. Host-private inventory,
IP plans and secrets remain outside the public repository.

## Required sequence

Every new container/service must be designed in this order:

1. define the communication graph;
2. identify trust boundaries and authority checks;
3. determine required reachability;
4. choose network placement;
5. decide whether L2/L3 identity is required;
6. only then decide whether an address is needed, and whether it must be static.

Do not start with "which IP should this container receive?"

## Communication graph

A PR that introduces or changes a container MUST identify every required flow.

For each flow record:

- source;
- destination;
- connection initiator;
- protocol/port when applicable;
- whether it is required or optional;
- authentication/authority mechanism;
- data/capability class crossing the boundary;
- network/trust zone.

Use a table equivalent to:

| Source | Destination | Initiator | Protocol/Port | Required | Auth/Authority | Network/Zone |
| --- | --- | --- | --- | --- | --- | --- |
| component-a | component-b | component-a | HTTPS/443 | yes | scoped credential | provider egress |

No implied bidirectional reachability is allowed. If only A must initiate to B,
that does not justify B initiating to A.

## Network placement

After the communication graph exists, justify the minimum placement needed.

Evaluate in order:

1. Can an existing Docker/Compose network satisfy the flow?
2. Is internal service discovery sufficient?
3. Does the component need host reachability?
4. Does it need routed reachability outside the host?
5. Does it need to be visible as a separate L2 host?
6. Is more than one network attachment actually required?

A new network, VLAN, bridge, macvlan or routed subnet requires an architectural
reason. Convenience alone is insufficient.

## Addressing decision

Every new container PR must state:

```text
L2 identity: required / not required
L3 identity: required / not required
Dedicated IP: required / not required
Static IP: required / not required
Networks attached: [...]
Reason: ...
```

### Dedicated IP is not the default

A dedicated IP may be justified by, for example:

- external routed reachability;
- firewall/policy boundary;
- protocol requirement;
- explicit L2 identity requirement;
- operational monitoring that cannot use service identity;
- integration with a non-Docker network that cannot use internal service discovery.

A dedicated IP is **not** justified merely because a new container exists.

### Static IP is an additional decision

Even when a dedicated IP is required, a static address still requires justification.

Prefer stable service identity/DNS when routing and policy do not require a fixed
address.


## IPAM allocation authority

IPAM is the authoritative system for operational VLAN, subnet, gateway and IP
reservations.

Architecture may propose candidate ranges for discussion, but a candidate is not
an allocation until it has been checked and reserved in IPAM.

Required order after the communication/network-placement decision:

1. query IPAM for current allocation/capacity;
2. reserve the VLAN/subnet/gateway/IP objects in IPAM;
3. record the logical reservation/reference for the deployment;
4. only then configure Compose, host VLAN interfaces, routing/firewall or network
   appliances.

Do not allocate by arithmetic sequence alone (for example, "the previous VLAN is
217, therefore use 218") without checking IPAM first.

The public repository should not become a duplicate private address inventory.
Keep private allocation details in IPAM; Git may carry the role, topology
contract, and a non-secret reservation/reference needed to prove that allocation
was governed.

If runtime state and IPAM disagree, stop deployment and reconcile the source of
truth before creating another allocation.

## Least-reachability principle

Attach the container only to networks required by the communication graph.

Multi-network/dual-homed containers require explicit justification because they
can become unintended transit points or widen lateral reachability.

The design must prevent the container from becoming a bridge/router unless that
is an explicit, reviewed responsibility.

## Trust boundaries

Network reachability never grants application authority by itself.

For every flow, distinguish:

- **can reach** — network routing/attachment permits packets;
- **may act** — application credential/policy permits the operation.

Both controls should be minimized independently.

## Inbound vs outbound-only workloads

Some workloads may need no inbound application traffic.

A polling/synchronization worker may need only:

- database access;
- internal ingress/API access;
- provider/internet egress;
- local health/observability.

Such a workload may not require a dedicated routable IP at all.

The architecture must prove the need before assigning one.

## Observability

Monitoring reachability does not automatically justify an extra production
network attachment.

Prefer observability mechanisms already available on required networks. If an
additional management attachment is necessary, document it as a separate flow
with its own authority and blast-radius analysis.

## Rollback

A deployment change must explain how to remove the new service and its network
attachments without renumbering or disrupting unrelated components.

Where possible, rollback should be:

1. stop/disable the service;
2. detach its networks;
3. remove only its optional network artifacts;
4. preserve unrelated addressing and routes.

## PR review rule

Any PR that creates a container, adds a network attachment, changes container
placement, introduces a new subnet/VLAN/bridge, or assigns a static container IP
MUST include:

1. communication matrix;
2. network placement decision;
3. L2/L3/addressing decision;
4. trust/authority boundaries;
5. rollback impact.

Missing communication topology is an architecture-blocking omission, not an
implementation detail to be decided during deployment.

## Immediate application: Channel Sync Deployment V1

Issue #265 must define the Channel Sync communication graph before any Compose
network or address is chosen.

Only after that graph is reviewed may the implementation decide whether Channel
Sync:

- reuses an existing internal network;
- requires access to more than one existing network;
- needs only outbound/provider egress plus internal service discovery;
- needs a dedicated address;
- or needs no new address at all.

## Non-goals

- no private AGT IP plan in Git;
- no automatic one-IP-per-container policy;
- no retroactive redesign of all existing services;
- no substitution of network isolation for application authorization;
- no requirement that every container be externally routable.
