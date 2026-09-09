"""Deterministic, provider-free architecture demonstration."""

from __future__ import annotations


def main() -> None:
    print("DETERMINISTIC OFFLINE DEMO")
    steps = [
        ("Synthetic inbound", "synthetic-event-001"),
        ("Context assembly", "synthetic-context-001"),
        ("Deterministic agent proposal", "request_information"),
        ("Policy/autonomy decision", "REQUIRES_APPROVAL"),
        ("Human approval boundary", "APPROVAL_REQUIRED"),
        ("Execution/outbox", "synthetic-outbox-001"),
        ("Synthetic delivery evidence", "synthetic-delivery-001"),
    ]
    for label, value in steps:
        print(f"{label}: {value}")
    for name in ("PHONE_NUMBERS", "NAMES", "MESSAGES", "CREDENTIALS"):
        print(f"DEMO_REAL_{name}=0")
    print("DEMO_NETWORK_PROVIDER_CALLS=0")


if __name__ == "__main__":
    main()
