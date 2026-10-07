# RETIRED_HISTORICAL — host-native runtime network namespaces

This package is retained solely for private rollback and forensic history.
It is not the canonical WhatsApp runtime target. The container candidate is
under `ops/whatsapp-container/`; do not install this package as a new
production deployment. AGT live remains host-native until approved cutover.

This package captures the source-controlled wiring used to isolate the Browser
and Transport runtimes in dedicated Linux network namespaces.

The public repository contains behavior and contracts only. Physical interface
names, VLAN IDs, addresses, gateways, resolver addresses and internal service
endpoints are host configuration and belong in the private host environment.

## Browser namespace

andy-netns-vlan creates a VLAN interface on an explicitly supplied parent,
moves it into andy-browser as eth0, assigns the configured address and default
route, and leaves the host topology outside the script.

andy-browser-cdp-edge.service exposes Chrome DevTools inside the Browser
namespace using host-private bind parameters from the environment file.

## Transport namespace

andy-transport-netns creates the Transport path as:

parent -> macvlan base -> VLAN eth0 -> andy-transport namespace

Parent interface, macvlan name, VLAN, address and gateway are supplied by the
private host environment.

### Optional TX offload policy

A previously validated host path showed invalid TCP checksums on the physical
wire while TX checksum, TSO and GSO offloads were enabled. Disabling those
offloads restored correct wire behavior.

That workaround is represented as explicit host policy through
ANDY_TRANSPORT_DISABLE_TX_OFFLOADS. Its public default is false; a host may set
it to true only when physical evidence requires the workaround.

## Systemd integration

The versioned units and service drop-ins establish ordering and namespace
attachment. Host-specific values are read from
/etc/attention-router/agt-network-namespace.env.

Resolver files remain host-owned:

- /etc/attention-router/resolv-browser.conf
- /etc/attention-router/resolv-transport.conf

The source examples intentionally contain no deployable private addresses.

## Historical deployment rule — do not execute for new deployments

Repository files are source of truth for logic. Host topology remains private.
Deployment must validate the environment, install scripts and units, run
systemd-analyze verify, reload systemd and then prove Browser and Transport
readiness without requiring a WhatsApp re-pair.
