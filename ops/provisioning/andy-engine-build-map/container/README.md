# Andy Engine Build Map — container first

Standalone static visual page, bundled with its static JSON and frontend assets
inside the OCI image. The source of truth must be Git; no runtime HTTP
dependency on the source tree on AGT.

For a candidate, bind to an alternative administrative address/port, verify
`/healthz`, `/`, `/app.js` and `/engine-map.json` against the live native
page, then obtain required GitHub checks and perform a controlled port cutover.
The old systemd unit and its data must be backed up before being disabled.
Nothing in the image needs network connectivity to other Attention Router
services or the Docker socket. To publish from other networks, route through
an explicitly authorized ingress.

Use `ENGINE_MAP_BIND_HOST` and `ENGINE_MAP_BIND_PORT` from a deployment
profile. Do not bake the old AGT `10.45.0.2` into the image or require a
second physical interface. Any later changed stages/statuses must be made
through a versioned update to `engine-map.json`; don't silently overwrite the
image content in an unversioned web directory.
