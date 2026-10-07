# RETIRED_HISTORICAL — AGT host-native WhatsApp runtime

These unit and namespace definitions explain the Stage 3.8 production
architecture and remain available for the private cutover rollback. They are
not a deployment source for new WhatsApp runtime releases. The current live
host remains on these units until an authorized cutover and soak succeed.

Canonical target: `ops/whatsapp-container/`.

Do not install or enable these files from automation. After successful soak,
the host copies are stopped, disabled and masked; the private rollback bundle
is retained. Historical documentation may cite the old service names but must
carry the `RETIRED_HISTORICAL` marker.
