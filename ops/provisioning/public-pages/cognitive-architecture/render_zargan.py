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

MAX_CHARS = 860
DISPLAY_VOICE = "Zargan"
PROVIDER_VOICE_ID = "zagan"


def sentences(text: str) -> list[str]:
    import re
    return [item.strip() for item in re.split(r"(?<=[.!?])\\s+", text.strip()) if item.strip()]


def chunk(text: str) -> list[str]:
    chunks: list[str] = []
    current = ""
    for sentence in sentences(text):
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
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
           "default=noprint_wrappers=1:nokey=1", str(path)]
    return float(subprocess.check_output(cmd, text=True).strip())


def post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode())


def tts_container_ip(container: str) -> str:
    cmd = ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container]
    address = subprocess.check_output(cmd, text=True).strip()
    if not address:
        raise RuntimeError("tts container has no address")
    return address


def source_segments(content: dict[str, object]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    thesis = content.get("thesis")
    if isinstance(thesis, str) and thesis.strip():
        out.append({"kind": "thesis", "text": thesis.strip()})
    passage = content.get("passage")
    if isinstance(passage, list):
        for item in passage:
            value = str(item).strip()
            if value:
                out.append({"kind": "passage", "text": value})
    sections = content.get("sections")
    if not isinstance(sections, list):
        raise ValueError("content.sections must be a list")
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("content section must be an object")
        title = str(section.get("title") or "").strip()
        if title:
            out.append({"kind": "heading", "text": title})
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list):
            raise ValueError("section.paragraphs must be a list")
        for item in paragraphs:
            value = str(item).strip()
            if value:
                out.append({"kind": "body", "text": value})
    return out


def voice_profile(kind: str) -> tuple[float, dict[str, object]]:
    if kind == "passage":
        speed = 0.94
    elif kind == "heading":
        speed = 0.95
    else:
        speed = 0.97
    return speed, {
        "enabled": True,
        "preset": "podcast" if kind != "heading" else "narrador",
        "level": 2 if kind != "heading" else 1,
        "tone": "friendly" if kind != "heading" else "professional",
        "pause_style": "expressive",
        "elongation": "none",
    }


def synthesize(url: str, text: str, kind: str, generated_dir: Path, destination: Path) -> None:
    speed, humanization = voice_profile(kind)
    payload: dict[str, object] = {
        "text": text,
        "provider": "xai",
        "language": "pt-BR",
        "voice": PROVIDER_VOICE_ID,
        "speed": speed,
        "humanization": humanization,
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
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
         "-safe", "0", "-i", str(concat_file), "-c", "copy", str(target)],
        check=True,
    )


def render(args: argparse.Namespace) -> dict[str, object]:
    source = Path(args.source)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    audio_dir = output / "audio"
    audio_dir.mkdir(exist_ok=True)

    raw = source.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    final_audio = audio_dir / "zargan-cognitive-architecture.mp3"
    segments_path = output / "segments.json"
    manifest_path = audio_dir / "manifest.json"

    content = json.loads(raw)
    source_items = source_segments(content)
    address = tts_container_ip(args.tts_container)
    url = f"http://{address}:8090/api/generate-audio"
    generated_dir = Path(args.generated_dir)

    with tempfile.TemporaryDirectory(prefix="andy-cognitive-zargan-") as temp:
        temp_dir = Path(temp)
        files: list[Path] = []
        timeline: list[dict[str, object]] = []
        cursor = 0.0
        chunk_index = 0

        for item in source_items:
            duration_total = 0.0
            for part in chunk(item["text"]):
                destination = temp_dir / f"{chunk_index:04d}.mp3"
                synthesize(url, part, item["kind"], generated_dir, destination)
                duration = probe(destination)
                files.append(destination)
                duration_total += duration
                chunk_index += 1
            timeline.append({
                "start": round(cursor, 6),
                "duration": round(duration_total, 6),
                "kind": item["kind"],
                "text": item["text"],
            })
            cursor += duration_total

        concatenate(files, final_audio, temp_dir)

    segments_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n")
    manifest: dict[str, object] = {
        "source_sha256": source_hash,
        "display_voice": DISPLAY_VOICE,
        "provider": "xai",
        "provider_voice_id": PROVIDER_VOICE_ID,
        "style": "warm_reflective_enthusiasm",
        "segments": len(timeline),
        "chunks": chunk_index,
        "duration_seconds": round(cursor, 3),
        "audio_sha256": hashlib.sha256(final_audio.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False))
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tts-container", default="tts-api-switcher")
    parser.add_argument("--generated-dir", default="/srv/projetos/tts-api-switcher/app/generated")
    return parser.parse_args()


def main() -> int:
    render(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
