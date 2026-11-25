"""Candidate for the existing standalone artifact worker's mounted runner.

The observed runner uses the same application service and worker identity.
Admit each full cycle through the optional host maintenance gate. Deployment
of this entrypoint, its image and shared state mount requires a separate
operational decision; importing this module starts no worker.
"""
import signal
import time

from attention_router.application.artifact_understanding import process_artifact_understandings
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.maintenance_gate import observe_runtime, runtime_admission

WORKER_ID = "artifact-understanding-live"


def run():
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    observe_runtime("artifact-worker")
    print("ARTIFACT_UNDERSTANDING_WORKER=STARTED", flush=True)
    while running:
        processed = 0
        try:
            with runtime_admission("artifact-worker") as admitted:
                if admitted:
                    with SessionLocal() as session:
                        try:
                            processed = process_artifact_understandings(session, WORKER_ID)
                        except Exception:
                            session.rollback()
                            raise
        except Exception as exc:
            print("ARTIFACT_UNDERSTANDING_CYCLE=FAILED TYPE=" + type(exc).__name__, flush=True)
        if processed:
            print("ARTIFACT_UNDERSTANDING_CYCLE=PASS PROCESSED=" + str(processed), flush=True)
        time.sleep(2 if processed else 5)
    print("ARTIFACT_UNDERSTANDING_WORKER=STOPPED", flush=True)


if __name__ == "__main__":
    run()
