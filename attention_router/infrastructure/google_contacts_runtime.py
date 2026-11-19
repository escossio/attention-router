"""Bounded Contacts scheduling inside the existing API, not the voice Worker."""

import os
import threading
from attention_router.application.google_contacts import (
    ContactsService,
    ContactsOAuth,
    PeopleConnections,
)
from attention_router.security.provider_secrets import ProviderSecretCipher
from attention_router.infrastructure.db import SessionLocal


def install_google_contacts(app, settings, client_sessions, get_session):
    install_conversation_context(app)
    if os.environ.get("GOOGLE_CONTACTS_ENABLED") != "true":
        return
    service = ContactsService(
        client_sessions=client_sessions,
        oauth=ContactsOAuth(
            client_id=settings.google_workspace_oauth_client_id or "",
            client_secret=settings.google_workspace_oauth_client_secret or "",
        ),
        people=PeopleConnections(),
        cipher=ProviderSecretCipher(settings.provider_authorization_key_b64url),
        enabled=True,
    )
    from attention_router.api.v1.google_contacts import build_google_contacts_router

    app.include_router(build_google_contacts_router(get_session=get_session, service=service))
    stop = threading.Event()
    app.state.google_contacts_service = service

    def run():
        while not stop.is_set():
            try:
                with SessionLocal() as session:
                    service.sync_one(session)
            except Exception:
                import logging

                logging.getLogger(__name__).warning("contacts_sync_cycle_failed")
            stop.wait(30)

    @app.on_event("startup")
    def start():
        threading.Thread(target=run, name="google-contacts-sync", daemon=True).start()

    @app.on_event("shutdown")
    def shutdown():
        stop.set()


def install_conversation_context(app):
    """Reuse API background lifecycle; independent of provider availability."""
    if os.environ.get("CONVERSATION_CONTEXT_ENABLED") != "true":
        return
    stop = threading.Event()

    def run():
        from attention_router.application.conversation_context import reconcile
        from sqlalchemy import text
        import logging
        while not stop.is_set():
            try:
                with SessionLocal() as session:
                    reconcile(session)
            except Exception:
                logging.getLogger(__name__).warning("conversation_context_cycle_failed")
                try:
                    with SessionLocal() as session:
                        session.execute(text("UPDATE conversation_context_progress SET failures=failures+1,updated_at=now() WHERE name='reconcile'"))
                        session.commit()
                except Exception:
                    pass
            stop.wait(30)

    @app.on_event("startup")
    def start_context():
        threading.Thread(target=run, name="conversation-context", daemon=True).start()

    @app.on_event("shutdown")
    def stop_context():
        stop.set()
