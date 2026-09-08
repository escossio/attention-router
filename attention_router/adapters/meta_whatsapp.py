from datetime import datetime, timezone
from typing import Any

from attention_router.adapters.inbound import InboundAdapter, NormalizedInboundEvent


class MetaWhatsAppInboundAdapter(InboundAdapter):
    source = "meta_whatsapp"
    channel = "whatsapp"
    supported_message_types = {
        "text",
        "image",
        "audio",
        "video",
        "document",
        "sticker",
        "location",
        "contacts",
        "interactive",
        "button",
        "reaction",
    }

    def normalize(self, raw_event: dict[str, Any]) -> NormalizedInboundEvent:
        events = self.normalize_many(raw_event)
        if len(events) != 1:
            raise ValueError("Meta webhook contains zero or multiple message events")
        return events[0]

    def normalize_many(self, raw_event: dict[str, Any]) -> list[NormalizedInboundEvent]:
        normalized: list[NormalizedInboundEvent] = []
        for message, value, contact in self.iter_messages(raw_event):
            event = self._normalize_message(message, value, contact)
            if event is not None:
                normalized.append(event)
        return normalized

    def iter_statuses(self, raw_event: dict[str, Any]) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        for value in self._iter_values(raw_event):
            for status in value.get("statuses", []) or []:
                if isinstance(status, dict):
                    errors = self._normalize_status_errors(status.get("errors"))
                    error_item = errors[0] if errors else {}
                    error_data = error_item.get("error_data") or {}
                    statuses.append(
                        {
                            "id": status.get("id"),
                            "status": status.get("status"),
                            "timestamp": status.get("timestamp"),
                            "recipient_id": status.get("recipient_id"),
                            "errors": errors,
                            "error_code": error_item.get("code"),
                            "error_subcode": error_item.get("error_subcode"),
                            "error_title": error_item.get("title"),
                            "error_message": error_item.get("message"),
                            "error_details": error_data.get("details"),
                            "error_fbtrace_id": error_item.get("fbtrace_id"),
                        }
                    )
        return statuses

    def _normalize_status_errors(self, raw_errors: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_errors, list):
            return []
        errors: list[dict[str, Any]] = []
        for raw_error in raw_errors:
            if not isinstance(raw_error, dict):
                continue
            error: dict[str, Any] = {}
            for field in ("code", "error_subcode", "title", "message", "fbtrace_id"):
                value = raw_error.get(field)
                if isinstance(value, (str, int, float, bool)):
                    error[field] = value
            error_data = raw_error.get("error_data")
            details = error_data.get("details") if isinstance(error_data, dict) else None
            if isinstance(details, (str, int, float, bool)):
                error["error_data"] = {"details": details}
            errors.append(error)
        return errors

    def iter_messages(self, raw_event: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        messages: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for value in self._iter_values(raw_event):
            contacts = {
                contact.get("wa_id"): contact
                for contact in value.get("contacts", []) or []
                if isinstance(contact, dict)
            }
            for message in value.get("messages", []) or []:
                if isinstance(message, dict):
                    messages.append((message, value, contacts.get(message.get("from"), {})))
        return messages

    def unsupported_messages(self, raw_event: dict[str, Any]) -> list[dict[str, Any]]:
        ignored: list[dict[str, Any]] = []
        for message, _, _ in self.iter_messages(raw_event):
            message_type = str(message.get("type") or "unknown")
            if message_type not in self.supported_message_types:
                ignored.append({"id": message.get("id"), "type": message_type})
        return ignored

    def _iter_values(self, raw_event: dict[str, Any]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for entry in raw_event.get("entry", []) or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes", []) or []:
                if not isinstance(change, dict):
                    continue
                value = change.get("value")
                if isinstance(value, dict) and value.get("messaging_product") == "whatsapp":
                    enriched = dict(value)
                    enriched["_meta_object"] = raw_event.get("object")
                    enriched["_meta_entry_id"] = entry.get("id")
                    enriched["_meta_field"] = change.get("field")
                    values.append(enriched)
        return values

    def _normalize_message(
        self,
        message: dict[str, Any],
        value: dict[str, Any],
        contact: dict[str, Any],
    ) -> NormalizedInboundEvent | None:
        message_id = message.get("id")
        sender = message.get("from")
        message_type = str(message.get("type") or "unknown")
        if not message_id or not sender or message_type not in self.supported_message_types:
            return None
        text = self._content_for_message(message, message_type)
        interactive = message.get("interactive") if message_type == "interactive" else None
        button_reply = interactive.get("button_reply") if isinstance(interactive, dict) else None
        return NormalizedInboundEvent(
            schema_version="1",
            source=self.source,
            external_event_id=str(message_id),
            event_type="message",
            occurred_at=self._timestamp(message.get("timestamp")),
            actor_id=str(sender),
            actor_display_name=self._profile_name(contact) or "WhatsApp actor",
            actor_category="unknown",
            channel=self.channel,
            content=text,
            metadata={
                "provider": "meta",
                "messaging_product": "whatsapp",
                "object": value.get("_meta_object"),
                "field": value.get("_meta_field"),
                "waba_id": value.get("_meta_entry_id"),
                "message_type": message_type,
                "wamid": str(message_id),
                "sender": str(sender),
                "phone_number_id": (value.get("metadata") or {}).get("phone_number_id"),
                "display_phone_number": (value.get("metadata") or {}).get("display_phone_number"),
                "contacts_wa_id": contact.get("wa_id"),
                "contact_profile_name": self._profile_name(contact),
                "contact_user_id": contact.get("user_id"),
                "context_id": (message.get("context") or {}).get("id"),
                "interactive_type": interactive.get("type") if isinstance(interactive, dict) else None,
                "button_reply_id": button_reply.get("id") if isinstance(button_reply, dict) else None,
                "button_reply_title": button_reply.get("title") if isinstance(button_reply, dict) else None,
                "canonical_action_intent": self._button_intent(button_reply),
            },
            raw_reference=f"meta_whatsapp:{message_id}",
        )

    def _content_for_message(self, message: dict[str, Any], message_type: str) -> str:
        if message_type == "text":
            return str((message.get("text") or {}).get("body") or "")
        if message_type == "interactive":
            reply = ((message.get("interactive") or {}).get("button_reply") or {})
            if ((message.get("interactive") or {}).get("type") == "button_reply" and reply.get("title")):
                return str(reply["title"])
        return f"[unsupported WhatsApp message type: {message_type}]"

    def _button_intent(self, reply: Any) -> str | None:
        if not isinstance(reply, dict) or not reply.get("id"):
            return None
        if str(reply["id"]).startswith("interactive_canary_"):
            suffix = str(reply["id"]).split("_")[-1]
            return {"approve": "TEST_APPROVE", "deny": "TEST_DENY"}.get(suffix)
        return None

    def _profile_name(self, contact: dict[str, Any]) -> str | None:
        profile = contact.get("profile") if isinstance(contact, dict) else None
        name = profile.get("name") if isinstance(profile, dict) else None
        return str(name) if name else None

    def _timestamp(self, value: Any) -> datetime | None:
        if value is None:
            return None
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
