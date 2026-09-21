"""Bounded, caller-transaction-owned Gmail metadata history execution."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.gmail_connection import gmail_authorization_scope
from attention_router.application.gmail_product_runner import (
    GmailProductAuthorizationUnavailable,
    GmailProductIngressFailed,
    GmailProductProviderUnavailable,
    GmailProductRefreshFailed,
    GmailProductRunnerDisabled,
    GmailProductRunnerError,
    _bearer_token,
    _sanitized,
)
from attention_router.infrastructure.gmail_slot_lock import acquire_gmail_slot
from attention_router.infrastructure.provider_authorization_models import ProviderAuthorizationRow
from attention_router.integrations.gmail_api_reader import history_id
from attention_router.integrations.gmail_connector import GmailConnectorError


if TYPE_CHECKING:
    from attention_router.application.gmail_product_runner import GmailProductRunner


class GmailProductHistoryStale(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_HISTORY_STALE"


class GmailProductHistoryRecordTooLarge(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_HISTORY_RECORD_TOO_LARGE"


class GmailProductHistoryBusy(GmailProductRunnerError):
    code = "GMAIL_PRODUCT_HISTORY_BUSY"


@dataclass(frozen=True, slots=True)
class GmailHistoryResult:
    installation_id: str
    initialized: bool = False
    history_records_examined: int = 0
    selected: int = 0
    accepted: int = 0
    duplicates: int = 0
    cursor_advanced: bool = False


def _additions(record: dict) -> list[str]:
    additions = record.get("messagesAdded", [])
    if not isinstance(additions, list) or len(additions) > 1000:
        raise GmailProductHistoryRecordTooLarge()
    ids = []
    for addition in additions:
        message = addition["message"]
        labels = message["labelIds"]
        message_id = message["id"]
        if (
            not isinstance(labels, list) or not all(isinstance(v, str) for v in labels)
            or not isinstance(message_id, str) or not 1 <= len(message_id) <= 256
        ):
            raise GmailProductProviderUnavailable()
        if "INBOX" in labels and message_id not in ids:
            ids.append(message_id)
    return ids


def _cycle(reader, connector, start, limit, max_pages):
    cursor = history_id(start)
    seen = set()
    tokens = set()
    token = None
    examined = accepted = duplicates = 0
    for _ in range(max_pages):
        page = reader.history_page(start, token)
        records = page.get("history", [])
        if not isinstance(records, list) or len(records) > 100:
            raise GmailProductProviderUnavailable()
        end = history_id(page.get("historyId"))
        if int(end) < int(cursor):
            raise GmailProductProviderUnavailable()
        for record in records:
            record_id = history_id(record["id"])
            if not int(cursor) < int(record_id) <= int(end):
                raise GmailProductProviderUnavailable()
            ids = _additions(record)
            # Check the complete record, independently of cycle deduplication:
            # the next execution must be able to consume it by itself.
            if len(ids) > limit:
                raise GmailProductHistoryRecordTooLarge()
            pending = [value for value in ids if value not in seen]
            if len(seen) + len(pending) > limit:
                return cursor, examined, len(seen), accepted, duplicates
            for message_id in pending:
                message, staged = connector.prepare_message(message_id)
                if (
                    message.message_id != message_id
                    or message.body_observed
                    or message.body
                ):
                    raise GmailProductProviderUnavailable()
                if connector.attachment_mode:
                    if (
                        not message.attachments_observed
                        or len(staged) != len(message.attachments)
                    ):
                        raise GmailProductProviderUnavailable()
                elif (
                    message.attachments_observed
                    or message.attachments
                    or staged
                ):
                    raise GmailProductProviderUnavailable()
                # Immutable provider timestamp makes retries byte-identical;
                # neutral ingress stores the actual admission wall clock.
                response = connector.ingest_message(
                    message,
                    staged_attachments=staged,
                    received_at=datetime.fromisoformat(
                        message.email_ts.replace("Z", "+00:00")
                    ),
                )
                if response.status_code not in {200, 202}:
                    raise GmailProductIngressFailed()
                if response.body.get("status") == "accepted":
                    accepted += 1
                elif response.body.get("status") == "duplicate":
                    duplicates += 1
                else:
                    raise GmailProductIngressFailed()
                seen.add(message_id)
            examined += 1
            cursor = record_id
        token = page.get("nextPageToken")
        if token is None:
            return end, examined, len(seen), accepted, duplicates
        if not isinstance(token, str) or not 1 <= len(token) <= 2048 or token in tokens:
            raise GmailProductProviderUnavailable()
        tokens.add(token)
    # Never use the mailbox-wide historyId while pages remain unexamined.
    return cursor, examined, len(seen), accepted, duplicates


def run_incremental(
    runner: GmailProductRunner, session: Session, *, installation_id: str,
    max_results: int | None = None, max_pages: int = 10, now: datetime | None = None,
) -> GmailHistoryResult:
    if not runner.settings.gmail_product_runner_enabled:
        raise GmailProductRunnerDisabled()
    if not isinstance(installation_id, str) or not installation_id:
        raise GmailProductAuthorizationUnavailable()
    limit = runner.settings.gmail_product_runner_max_results if max_results is None else max_results
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("GMAIL_POLL_LIMIT_OUT_OF_RANGE")
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise ValueError("GMAIL_HISTORY_PAGE_LIMIT_OUT_OF_RANGE")
    # Discover the immutable slot without a row lock, then take the shared
    # gate before any row locks. Reload/validate authority after acquisition.
    # Caller must commit/rollback promptly; no ingress authority locks span I/O.
    with session.no_autoflush:
        slot = _sanitized(GmailProductAuthorizationUnavailable, lambda: session.scalar(
            select(ProviderAuthorizationRow.slot_key)
            .where(ProviderAuthorizationRow.id == installation_id)
        ))
        if slot is None:
            raise GmailProductAuthorizationUnavailable()
        acquired = _sanitized(GmailProductHistoryBusy, lambda: acquire_gmail_slot(
            session, slot, wait=False
        ))
        if not acquired:
            raise GmailProductHistoryBusy()
        _sanitized(GmailProductHistoryBusy, lambda: session.scalar(
            select(ProviderAuthorizationRow)
            .where(ProviderAuthorizationRow.id == installation_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        ))
    row, binding, refresh, bearer = runner._load_governed_authorization(
        session, installation_id, now=runner._utc(now or datetime.now(UTC))
    )
    scope = gmail_authorization_scope(row.granted_scopes)
    if scope is None:
        raise GmailProductProviderUnavailable()
    token = _sanitized(
        GmailProductRefreshFailed,
        lambda: runner._refresh_access_token(refresh, scope),
    )
    if not _bearer_token(token):
        raise GmailProductRefreshFailed()
    reader = _sanitized(GmailProductProviderUnavailable, lambda: runner._reader_factory(token))
    start = row.gmail_history_id
    if start is None:
        baseline = _sanitized(GmailProductProviderUnavailable, lambda: history_id(
            reader.current_history_id()
        ))
        row.gmail_history_id = baseline
        session.flush([row])
        return GmailHistoryResult(row.id, initialized=True, cursor_advanced=True)
    connector = runner._build_connector(
        row=row,
        binding=binding,
        reader=reader,
        ingress_bearer=bearer,
    )
    failure = GmailProductProviderUnavailable
    try:
        end, examined, selected, accepted, duplicates = _cycle(
            reader, connector, start, limit, max_pages
        )
    except GmailProductHistoryRecordTooLarge:
        failure = GmailProductHistoryRecordTooLarge
    except GmailProductIngressFailed:
        failure = GmailProductIngressFailed
    except GmailConnectorError as exc:
        if str(exc) == GmailProductHistoryStale.code:
            failure = GmailProductHistoryStale
    except Exception:
        pass
    else:
        row.gmail_history_id = end
        session.flush([row])
        return GmailHistoryResult(
            row.id, False, examined, selected, accepted, duplicates, end != start
        )
    raise failure()
