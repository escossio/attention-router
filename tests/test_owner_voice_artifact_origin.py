from types import SimpleNamespace
import pytest
from attention_router.application.whatsapp_artifacts import _validate_outbound_media_origin

@pytest.mark.parametrize('kind', ['ptt', 'audio'])
@pytest.mark.parametrize('origin', ['UNKNOWN_FROM_ME', 'OWNER_MANUAL_OUTBOUND_OBSERVED', 'ROUTER_AUTOMATED_OUTBOUND_OBSERVED'])
def test_account_voice_is_evidence_without_human_authorship(kind, origin):
    metadata = dict(from_me=True, conversation_state='READY', has_media=True,
                    message_type=kind, from_me_classification=origin,
                    final_from_me_classification=origin)
    event = SimpleNamespace(source='wwebjs', event_type='message', payload=dict(
        channel='whatsapp', owner_authenticated=False, event_origin=origin, metadata=metadata))
    _validate_outbound_media_origin(event)
    assert event.payload['owner_authenticated'] is False
    assert event.payload['event_origin'] == origin
    metadata['message_type'] = 'image'
    if origin != 'OWNER_MANUAL_OUTBOUND_OBSERVED':
        with pytest.raises(ValueError):
            _validate_outbound_media_origin(event)
    metadata['message_type'] = kind
    metadata['is_group'] = True
    _validate_outbound_media_origin(event)
