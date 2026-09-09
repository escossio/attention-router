# Public software supply chain

The v0.1.0 source commit and container artifact are distinct references:

| Reference | Meaning |
| --- | --- |
| `7e6faa89b21e1c0f21d80c34fb3e1cf02f26c805` | source commit used for the release |
| `v0.1.0` | immutable Git tag and public prerelease |
| `ghcr.io/escossio/attention-router:v0.1.0` | human-readable container tag |
| `ghcr.io/escossio/attention-router@sha256:...` | immutable container digest |

Pull by tag for convenience, or pin the digest for reproducible consumption:

```bash
docker pull ghcr.io/escossio/attention-router:v0.1.0
docker pull ghcr.io/escossio/attention-router@sha256:<published-digest>
```

The release assets include an SPDX JSON SBOM, digest metadata, and checksums.
The SBOM describes the published image digest; its checksum verifies the
downloaded asset, while artifact/build provenance attestations establish how
the image was built. A checksum is not a substitute for provenance.

Verify artifact/build provenance with the official CLI:

```bash
gh attestation verify \
  oci://ghcr.io/escossio/attention-router:v0.1.0 \
  -R escossio/attention-router
```

Compare `attention-router-v0.1.0-image-digest.txt` with the registry digest,
then verify `attention-router-v0.1.0-checksums.txt` with `sha256sum -c`.
