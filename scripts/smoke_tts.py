import argparse
import hashlib
from pathlib import Path

from attention_router.adapters.tts import configured_tts_provider


TEXT = "Oi, eu sou a Andy. Esta é a voz escolhida para cuidar das mensagens do Alex com clareza, calma e honestidade."


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one explicit Andy TTS smoke synthesis.")
    parser.add_argument("--output", required=True, help="Destination MP3 path.")
    parser.add_argument("--text", default=TEXT)
    args = parser.parse_args()
    output = Path(args.output)
    result = configured_tts_provider().synthesize(args.text, output)
    if result is None:
        print("status=disabled")
        return
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print("status=ok")
    print(f"profile={result.profile}")
    print(f"provider={result.provider}")
    print(f"request_id={result.request_id}")
    print(f"path={output}")
    print(f"size_bytes={result.size_bytes}")
    print(f"sha256={digest}")


if __name__ == "__main__":
    main()
