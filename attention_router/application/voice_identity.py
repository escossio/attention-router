"""Resolve only identities emitted by Transport; never infer LID/phone aliases."""
import re
SERIAL = re.compile(r'(true|false)_([^\s:_]+@(?:c\.us|lid|g\.us|newsletter))_([A-Za-z0-9_-]+)\Z')
FALLBACK = re.compile(r'wwebjs:(inbound|from-me):([^\s:_]+@(?:c\.us|lid|g\.us|newsletter)):([A-Za-z0-9_-]+)\Z')

def parse(value):
    if not isinstance(value, str):
        return None
    match = SERIAL.fullmatch(value)
    if match:
        direction, remote, stanza = match.groups()
        return value, direction == 'true', remote, 'SERIALIZED'
    match = FALLBACK.fullmatch(value)
    if match:
        direction, remote, stanza = match.groups()
        return f'{str(direction == "from-me").lower()}_{remote}_{stanza}', direction == 'from-me', remote, 'TRANSPORT_COMPONENTS'
    return None

def resolve(event):
    payload = event.payload or {}
    values = [event.external_event_id]
    raw = payload.get('raw_reference')
    if raw is not None:
        # Ingress prefixes the provider to its unmodified source message ID.
        if not isinstance(raw, str) or not raw.startswith('wwebjs:'):
            raise ValueError('REFERENCE_INCOMPATIBLE')
        values.append(raw[len('wwebjs:'):])
    candidates = [parse(value) for value in values]
    if any(c is None for c in candidates):
        raise ValueError('ORIGINAL_WHATSAPP_ID_UNAVAILABLE')
    if len({c[0] for c in candidates}) != 1:
        raise ValueError('IDENTITY_AMBIGUOUS')
    locator, from_me, remote, strategy = candidates[0]
    if payload.get('metadata', {}).get('from_me') is not from_me:
        raise ValueError('MEDIA_DIRECTION_MISMATCH')
    return locator, strategy
