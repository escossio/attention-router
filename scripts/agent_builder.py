import argparse
import json

from attention_router.application import agent_builder
from attention_router.infrastructure.db import SessionLocal


def _print_session(row) -> None:
    print(json.dumps({"session_id": row.id, "status": row.status, "current_stage": row.current_stage}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Builder V0 local CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    start_cmd = sub.add_parser("start")
    start_cmd.add_argument("--interviewer", choices=["deterministic", "openai"], default=None)
    next_cmd = sub.add_parser("next")
    next_cmd.add_argument("--session-id", required=True)
    answer_cmd = sub.add_parser("answer")
    answer_cmd.add_argument("--session-id", required=True)
    answer_cmd.add_argument("--text", required=True)
    chat_cmd = sub.add_parser("chat")
    chat_cmd.add_argument("--session-id", required=True)
    chat_cmd.add_argument("--text", required=True)
    chat_cmd.add_argument("--interviewer", choices=["deterministic", "openai"], default=None)
    correct_cmd = sub.add_parser("correct")
    correct_cmd.add_argument("--session-id", required=True)
    correct_cmd.add_argument("--stage", required=True)
    correct_cmd.add_argument("--text", required=True)
    review_cmd = sub.add_parser("review")
    review_cmd.add_argument("--session-id", required=True)
    build_cmd = sub.add_parser("build")
    build_cmd.add_argument("--session-id", required=True)
    args = parser.parse_args()

    with SessionLocal() as session:
        if args.command == "start":
            row = agent_builder.create_configuration_session(session)
            if args.interviewer:
                row.answers = {"_interviewer_provider": args.interviewer}
            session.commit()
            _print_session(row)
            question = agent_builder.get_next_question(session, row.id)
            if question:
                print(question.text)
            return 0
        if args.command == "next":
            question = agent_builder.get_next_question(session, args.session_id)
            print(question.text if question else "Sem proxima pergunta.")
            return 0
        if args.command == "answer":
            row = agent_builder.record_configuration_answer(session, args.session_id, args.text)
            session.commit()
            _print_session(row)
            question = agent_builder.get_next_question(session, args.session_id)
            if question:
                print(question.text)
            return 0
        if args.command == "chat":
            row = agent_builder.get_configuration_session(session, args.session_id)
            provider = args.interviewer or row.answers.get("_interviewer_provider")
            result = agent_builder.chat_configuration_session(session, args.session_id, args.text, provider=provider)
            session.commit()
            print(result["assistant_message"])
            return 0
        if args.command == "correct":
            row = agent_builder.correct_configuration_answer(session, args.session_id, args.stage, args.text)
            session.commit()
            _print_session(row)
            return 0
        if args.command == "review":
            row = agent_builder.get_configuration_session(session, args.session_id)
            print(agent_builder.render_review(row))
            return 0
        if args.command == "build":
            version = agent_builder.build_blueprint_from_session(session, args.session_id)
            session.commit()
            print(json.dumps({"blueprint_id": version.blueprint_id, "version": version.version, "spec": version.spec}, indent=2))
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
