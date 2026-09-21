# Gmail Product Connection V1

Status: implementation candidate

Related:
- #83 Multi-channel expansion
- Client Session V0.3C
- Integration Contract V1
- Neutral Integration Ingress/Dispatch
- Gmail Connector/API Reader V1

## Product flow

The normal subscriber experience is:

`Andy Android -> Connect Gmail -> Google consent -> authorization code -> Attention Router`

The user never provisions an integration bearer or edits runtime flags.

## Authentication versus authorization

Google sign-in remains Human Identity authentication.

Gmail access is a separate provider authorization.

The backend derives:
- Human Identity;
- active tenant;
- device/session authority

from the existing authenticated Client Session.

The Gmail API request accepts only a one-time Google authorization code.
The Android client cannot select tenant, Human Identity, integration binding,
provider account identity or internal credential.

## Initial Gmail scope

V1 requests only:

`https://www.googleapis.com/auth/gmail.metadata`

This is enough for the current metadata-only integration boundary and avoids
requesting message-body access before the product has a governed body-ingestion
feature.

Future features may request additional scopes incrementally.

## Offline provider access

Android requests Google offline access for the server Web OAuth client.

Google returns a one-time server authorization code. The backend exchanges it
at `https://oauth2.googleapis.com/token`.

The access token is used only long enough to verify the Gmail mailbox via
`users/me/profile` and is never persisted.

The refresh token is persisted only inside an AES-256-GCM envelope.

## Secret storage

`provider_authorizations` contains:
- tenant/Human Identity scope;
- provider/product;
- SHA-256 provider account fingerprint;
- granted scopes;
- encrypted secret envelope;
- Integration binding/credential references;
- lifecycle state.

The encrypted envelope contains:
- Google refresh token;
- internal neutral-ingress bearer.

No plaintext columns exist for:
- authorization code;
- access token;
- refresh token;
- integration bearer;
- Gmail address;
- Google subject;
- OAuth client secret.

The encryption key is deployment configuration:

`PROVIDER_AUTHORIZATION_KEY_B64URL`

It is a 32-byte unpadded base64url key and is never stored in PostgreSQL.

## Automatic channel.email provisioning

A successful connection automatically creates or rotates:
- one `channel.email` IntegrationBinding;
- one digest-only IntegrationCredential.

The provider mailbox address is not stored as the integration account key.
The binding uses:

`sha256:<provider-account-hash>`

Reconnecting the same mailbox rotates the internal integration credential.
Changing the authorized mailbox disables the old binding and creates the new
installation namespace.

## API

Authenticated Client Session endpoints:

- `GET /api/v1/integrations/gmail`
- `POST /api/v1/integrations/gmail`
- `DELETE /api/v1/integrations/gmail`

POST accepts only:

`authorization_code`

Responses expose no provider mailbox identifier or secret.

## Disconnect

Disconnect:
1. decrypts the provider refresh token server-side;
2. revokes Google provider access;
3. disables the IntegrationBinding;
4. revokes the IntegrationCredential;
5. marks the provider authorization REVOKED.

The user can reconnect later through the normal Google consent flow.

## Runtime configuration

V1 is disabled by default:

`GMAIL_CONNECT_ENABLED=false`

When enabled it requires:
- Client Session enabled;
- Google Workspace Web OAuth client ID;
- Google Workspace Web OAuth client secret;
- provider authorization AES-GCM key.

## Non-goals

This backend slice does not:
- change Human Identity semantics;
- treat Gmail address as canonical user identity;
- start Gmail polling;
- ingest message bodies;
- request send/modify scopes;
- expose provider or integration secrets to Android;
- enable production flags.

## Android contract

The Android implementation should use Google Identity Services
`AuthorizationClient`, request offline access using the server client ID, and
request `gmail.metadata`.

If Google returns a PendingIntent resolution, the UI completes that consent
flow. The resulting server authorization code is POSTed over the existing
authenticated Client Session.

A missing refresh token on a new installation is returned as
`GMAIL_REFRESH_TOKEN_REQUIRED`; the Android flow may retry with explicit
Google consent.

