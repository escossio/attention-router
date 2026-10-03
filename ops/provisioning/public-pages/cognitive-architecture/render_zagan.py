#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path


MAX_CHARS = 900


def sentences(text: str) -> list[str]:
    import re

    return [
        item.strip()
        for item in re.split(r"(?<=[.!?])\\s+", text.strip())
        if item.strip()
    ]


def chunk(text: str) -> list[str]:
    chunks: list[str] = []
    current = ""
    for sentence in sentences(text):
        if len(sentence) > MAX_CHARS:
            words = sentence.split()
            part = ""
            for word in words:
                candidate = (part + " " + word).strip()
                if len(candidate) > MAX_CHARS and part:
                    chunks.append(part)
                    part = word
                else:
                    part = candidate
            if part:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(part)
            continue

        candidate = (current + " " + sentence).strip()
        if len(candidate) > MAX_CHARS and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def probe(path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    return float(subprocess.check_output(command, text=True).strip())


def post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def current_manifest(
    manifest_path: Path,
    source_hash: str,
    final_audio: Path,
    segments_path: Path,
) -> bool:
    if not (
        manifest_path.exists()
        and final_audio.exists()
        and segments_path.exists()
    ):
        return False
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError, TypeError):
        return False
    return manifest.get("source_sha256") == source_hash


def tts_container_ip(container: str) -> str:
    command = [
        "docker",
        "inspect",
        "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        container,
    ]
    address = subprocess.check_output(command, text=True).strip()
    if not address:
        raise RuntimeError("tts container has no address")
    return address


def source_paragraphs(content: dict[str, object]) -> list[str]:
    paragraphs: list[str] = []
    sections = content.get("sections")
    if not isinstance(sections, list):
        raise ValueError("content.sections must be a list")
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("content section must be an object")
        values = section.get("paragraphs")
        if not isinstance(values, list):
            raise ValueError("section.paragraphs must be a list")
        paragraphs.extend(str(item).strip() for item in values if str(item).strip())
    return paragraphs


def synthesize_chunk(
    *,
    url: str,
    text: str,
    generated_dir: Path,
    destination: Path,
) -> None:
    payload: dict[str, object] = {
        "text": text,
        "provider": "xai",
        "language": "pt-BR",
        "voice": "zagan",
        "speed": 1.0,
        "humanization": {"enabled": False},
    }
    result = post_json(url, payload)
    if result.get("status") != "ok":
        raise RuntimeError(f"tts generation failed: {result}")
    filename = result.get("filename")
    if not isinstance(filename, str) or not filename:
        raise RuntimeError("tts response did not include a filename")
    source = generated_dir / filename
    if not source.is_file():
        raise FileNotFoundError(source)
    shutil.copy2(source, destination)


def concatenate(files: list[Path], target: Path, temp_dir: Path) -> None:
    concat_file = temp_dir / "concat.txt"
    lines = []
    for path in files:
        escaped = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped}'\n")
    concat_file.write_text("".join(lines))
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(target),
        ],
        check=True,
    )


def render(args: argparse.Namespace) -> dict[str, object]:
    source = Path(args.source)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_dir = output_dir / "audio"
    audio_dir.mkdir(exist_ok=True)

    raw = source.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    manifest_path = audio_dir / "manifest.json"
    final_audio = audio_dir / "zagan-cognitive-architecture.mp3"
    segments_path = output_dir / "segments.json"

    if (
        not args.force
        and current_manifest(
            manifest_path,
            source_hash,
            final_audio,
            segments_path,
        )
    ):
        return json.loads(manifest_path.read_text())

    content = json.loads(raw)
    paragraphs = source_paragraphs(content)
    address = tts_container_ip(args.tts_container)
    url = f"http://{address}:8090/api/generate-audio"
    generated_dir = Path(args.generated_dir)

    with tempfile.TemporaryDirectory(
        prefix="andy-cognitive-zagan-"
    ) as temporary:
        temp_dir = Path(temporary)
        audio_files: list[Path] = []
        segments: list[dict[str, object]] = []
        cursor = 0.0
        chunk_index = 0

        for paragraph in paragraphs:
            paragraph_duration = 0.0
            for part in chunk(paragraph):
                destination = temp_dir / f"{chunk_index:04d}.mp3"
                synthesize_chunk(
                    url=url,
                    text=part,
                    generated_dir=generated_dir,
                    destination=destination,
                )
                duration = probe(destination)
                audio_files.append(destination)
                paragraph_duration += duration
                chunk_index += 1

            segments.append(
                {
                    "start": round(cursor, 6),
                    "duration": round(paragraph_duration, 6),
                    "text": paragraph,
                }
            )
            cursor += paragraph_duration

        concatenate(audio_files, final_audio, temp_dir)

    segments_path.write_text(
        json.dumps(segments, ensure_ascii=False, indent=2) + "\n"
    )
    manifest: dict[str, object] = {
        "source_sha256": source_hash,
        "voice": "zagan",
        "provider": "xai",
        "segments": len(segments),
        "chunks": chunk_index,
        "duration_seconds": round(cursor, 3),
        "audio_sha256": hashlib.sha256(final_audio.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tts-container", default="tts-api-switcher")
    parser.add_argument(
        "--generated-dir",
        default="/srv/projetos/tts-api-switcher/app/generated",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    manifest = render(parse_args())
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
