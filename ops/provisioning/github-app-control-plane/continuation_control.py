#!/usr/bin/env python3
import argparse
import json
import uuid

from webhook_receiver import (
    TARGET_REPO,
    acknowledge_signal,
    get_stage,
    init_db,
    list_ready_signals,
    register_stage,
)


def parser():
    root = argparse.ArgumentParser(
        description="Register and inspect GitHub event-driven continuation stages."
    )
    sub = root.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register")
    register.add_argument("--stage-id")
    register.add_argument("--repository", default=TARGET_REPO)
    register.add_argument("--pr-number", type=int, required=True)
    register.add_argument("--head-sha", required=True)
    register.add_argument(
        "--event-kind",
        choices=("workflow_run", "check_run"),
        required=True,
    )
    register.add_argument("--target-name", required=True)

    show = sub.add_parser("show")
    show.add_argument("stage_id")

    sub.add_parser("list-ready")

    ack = sub.add_parser("ack")
    ack.add_argument("stage_id")
    return root


def main():
    args = parser().parse_args()
    init_db()

    if args.command == "register":
        stage_id = args.stage_id or f"stage_{uuid.uuid4().hex}"
        register_stage(
            stage_id,
            args.repository,
            args.pr_number,
            args.head_sha,
            args.event_kind,
            args.target_name,
        )
        print(json.dumps(get_stage(stage_id), sort_keys=True))
        return

    if args.command == "show":
        row = get_stage(args.stage_id)
        if row is None:
            raise SystemExit("stage not found")
        print(json.dumps(row, sort_keys=True))
        return

    if args.command == "list-ready":
        print(json.dumps(list_ready_signals(), sort_keys=True))
        return

    if args.command == "ack":
        if not acknowledge_signal(args.stage_id):
            raise SystemExit("ready signal not found")
        print(json.dumps({"stage_id": args.stage_id, "acknowledged": True}, sort_keys=True))
        return


if __name__ == "__main__":
    main()
