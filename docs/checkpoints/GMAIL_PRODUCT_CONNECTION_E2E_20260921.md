# Gmail Product Connection — E2E checkpoint

Date: 2026-09-21

## Outcome

The real subscriber Gmail connection completed end to end on a physical Android device.

Flow proven:

`Andy Android -> Client Session -> Google authorization -> server authorization code -> Attention Router -> OAuth token exchange -> Gmail profile -> encrypted ProviderAuthorization -> channel.email binding -> CONNECTED`

Cold reopen also passed: the Android app restored its Client Session and `GET /api/v1/integrations/gmail` returned the persisted `CONNECTED` state without repeating Google authorization.

## Root cause of the failed live attempts

Safe provider diagnostics captured the Gmail profile rejection as:

`HTTP 403 / PERMISSION_DENIED / SERVICE_DISABLED,accessNotConfigured`

The authorization-code exchange had already succeeded. The failure occurred on the Gmail profile call because the Gmail API service was not enabled/configured for the Google Cloud project. After the provider service configuration was corrected, the same product flow completed successfully.

## Live evidence

- Authenticated Client Session establishment: challenge `201`, completion `200`, bootstrap `200`.
- Gmail status before connection: `GET /api/v1/integrations/gmail -> 200 DISCONNECTED`.
- Successful connection: `POST /api/v1/integrations/gmail -> 200`.
- Cold reopen: `GET /api/v1/integrations/gmail -> 200 CONNECTED`.
- Database evidence after success:
  - exactly one active `ProviderAuthorization` for the proved flow;
  - exactly one active `channel.email` `IntegrationBinding`;
  - an active digest-only `IntegrationCredential`;
  - provider secret material stored in the encrypted provider authorization envelope.
- Runtime readiness remained healthy on schema `0045_provider_authorization_v1`.

## Security properties preserved

- Android does not receive or persist provider refresh tokens.
- OAuth client secret is server-side only.
- Provider authorization encryption key is server-side only.
- No authorization code, access token, refresh token, Client Session bearer, OAuth client secret, or provider encryption key was written to the checkpoint or diagnostic logs.
- Gmail scope remains `https://www.googleapis.com/auth/gmail.metadata`.
- No Gmail send/modify permission was added.
- Client Session remains the authority for Human / Tenant / Device context.

## Infrastructure changes required for the proof

- Public Client API allowlist now includes exact `GET`, `POST`, and `DELETE` routes for `/api/v1/integrations/gmail`.
- Backend Gmail connection feature is enabled with its server-side OAuth and encryption prerequisites.
- Google OAuth test-user access was configured for the physical test account while the consent screen remains in Testing.
- Gmail API provider service must be enabled for the Google Cloud project.

## Repository state at this checkpoint

- Backend baseline product implementation: `a6930d1c01c7f4b381a558e04e95acf216c2650e` (PR #140).
- Android product implementation: `5fcf1a10f734829d8f10c95ff03e78dc6ed39e01` (PR #30).
- Safe Gmail provider-diagnostic change under review: PR #142, originally `cf812582a6fbf68d483b74d194fd635e506873ca`.

PR #142 adds bounded allowlisted provider error diagnostics only. It does not change OAuth scope, Android contract, database schema, or provider-secret handling.

## Follow-up boundary

The connection product slice is proven. Automatic Gmail polling/ingestion is a separate subsequent slice and was not part of this checkpoint.
