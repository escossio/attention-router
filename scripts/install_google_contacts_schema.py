"""Idempotent additive installation; touches only the two Contacts tables."""

from attention_router.infrastructure.db import engine
from attention_router.infrastructure.google_contacts_models import (
    GoogleContactsConnectionRow,
    GoogleContactRow,
)


def install(bind):
    with bind.begin() as connection:
        for table in [GoogleContactsConnectionRow.__table__, GoogleContactRow.__table__]:
            table.create(connection, checkfirst=True)


if __name__ == "__main__":
    install(engine)
