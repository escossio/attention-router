# V0.4A foreground current-location snapshot authority

V0.4A introduces the first contextual observation accepted from an authenticated Android client.

## Authority boundary

The client contributes only location observation data: latitude, longitude, accuracy, capture time and optional PRECISE/APPROXIMATE permission metadata.

Human Identity, device and tenant authority come exclusively from the current ClientSession and server-side revalidation.

## Surface

- `PUT /api/v1/client/location/current`
- `GET /api/v1/client/location/current`

Both require `ClientSession` Bearer authentication.

The write operation transactionally stores the latest accepted snapshot for the authenticated device + active tenant. A later accepted observation replaces the previous current snapshot for that scope.

## Deliberate limits

This frontier does not create a location event history. It does not enable background collection, tracking, geofencing, mapping, reverse geocoding, movement inference or Context Engine rules.

Android location collection is explicit and foreground-only. Approximate location is an acceptable user choice.

## Validation

The contract bounds coordinates, accuracy and capture time. The runtime must reject stale/future/invalid observations and re-check the existing V0.3C session/device/membership/tenant authority on every read and write.

## Persistence

The runtime frontier may add `client_location_snapshots` with one current row per device + tenant. Coordinates are sensitive tenant data and must never be logged as generic request telemetry.

## Stop boundary

Stop after the physical phone proves one explicit foreground location write and authenticated readback for the existing V0.3D session.
