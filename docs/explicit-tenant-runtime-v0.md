# Explicit Tenant Runtime V0

This boundary hardens the authenticated local WhatsApp ingress so production traffic cannot silently fall back to the historical default tenant.

## Contract

- Forwarded WhatsApp events must carry an explicit `tenant_id`.
- The local transport reads that identity from `LOCAL_TENANT_ID`.
- If inbound forwarding is enabled and `LOCAL_TENANT_ID` is absent, the transport fails closed before creating a forwarded event.
- The authenticated internal HTTP ingress rejects event and media payloads that omit `tenant_id`.
- The ingress accepts only tenants that exist and are `ACTIVE`.
- Replay/conflict lookup is scoped by `(tenant_id, source, external_event_id)`.
- Voice-media notifications carry the same tenant identity and resolve their inbound event inside that tenant scope.
- Tenant identity is included in the signed request body, so the existing HMAC authenticates the exact tenant assertion together with the rest of the payload.

## Compatibility seam

`InternalInboundPayload` and `MediaReadyNotification` keep the historical default tenant only for direct/internal callers and existing isolated tests. That compatibility default is not accepted at the authenticated HTTP boundary: production ingress requires the field to be present explicitly before model normalization.

This seam is temporary and lets tenant hardening proceed boundary-by-boundary rather than performing a risky repository-wide removal of `DEFAULT_TENANT_ID` in one change.

## Trust model

V0 trusts the authenticated internal-ingress principal to assert a tenant after the database confirms that tenant is active. A later hardening step can bind distinct credentials/principals to one or more allowed tenant IDs so possession of a generic internal HMAC secret is not sufficient to choose arbitrary tenants.

[ADR 0020](adr/0020-neutral-ingress-and-authenticated-tenant-binding.md) now
proposes one credential-to-tenant/integration binding for a distinct neutral
ingress surface. That proposal does not change this implemented HMAC trust model
or migrate the WhatsApp transport; those remain separate implementation work.

Public provider ingress is deliberately outside this boundary. In particular, a future multi-tenant Meta ingress should resolve provider account/phone-resource identity to a tenant through a trusted server-side binding rather than trusting a `tenant_id` supplied by the public webhook body.

## Non-goals

This change does not remove every legacy `DEFAULT_TENANT_ID` reference, add PostgreSQL RLS, redesign tenant provisioning, or change Personal Context. Those are subsequent boundaries.
