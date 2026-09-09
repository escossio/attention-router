"""Render the silent, synthetic public portfolio demo."""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/assets/demo"
WIDTH, HEIGHT, FPS = 1920, 1080, 24
TEAL, WHITE, MUTED, PANEL, BG = (115, 230, 209), (244, 247, 251), (169, 189, 212), (18, 36, 58), (11, 18, 32)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)


def frame(progress: float) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.ellipse((1320, -220, 2050, 510), fill=(18, 52, 91))
    draw.ellipse((1250, 700, 2100, 1450), fill=(18, 79, 82))
    draw.text((120, 86), "SYNTHETIC DEMO  ·  PUBLIC PORTFOLIO", fill=TEAL, font=font(28, True))
    draw.text((120, 150), "Attention Router / Andy", fill=WHITE, font=font(68, True))
    draw.text((124, 240), "Policy-aware Agent Runtime", fill=MUTED, font=font(34))
    draw.text((124, 316), "The model proposes. Policy decides. Humans control authority.", fill=WHITE, font=font(25))
    stages = ["Inbound", "Context", "Agent", "Policy", "Human Control", "Execution", "Delivery"]
    values = ["synthetic message", "assembled snapshot", "reply suggested", "approval required", "APPROVED", "queued / outbox", "confirmed evidence"]
    active = min(6, int(progress * 7.0))
    widths, y, left = [190, 190, 190, 190, 250, 210, 220], 500, 120
    for index, (label, value, box_width) in enumerate(zip(stages, values, widths)):
        x = left + sum(widths[:index]) + index * 35
        color, fill = (TEAL if index <= active else (68, 94, 120)), ((20, 52, 72) if index == active else PANEL)
        draw.rounded_rectangle((x, y, x + box_width, y + 120), radius=26, fill=fill, outline=color, width=4)
        label_width = draw.textbbox((0, 0), label, font=font(23, True))[2]
        value_width = draw.textbbox((0, 0), value, font=font(17))[2]
        draw.text((x + (box_width - label_width) / 2, y + 25), label, fill=WHITE, font=font(23, True))
        draw.text((x + (box_width - value_width) / 2, y + 72), value, fill=MUTED, font=font(17))
        if index < len(stages) - 1:
            arrow = TEAL if index < active else (68, 94, 120)
            nx = x + box_width + 10
            draw.line((nx, y + 60, nx + 25, y + 60), fill=arrow, width=4)
            draw.polygon(((nx + 25, y + 60), (nx + 12, y + 52), (nx + 12, y + 68)), fill=arrow)
    cards = [("INBOUND", '"Avise que estou em reunião e retorno em 30 minutos."'), ("PROPOSAL", '"Estou em reunião. Retorno em 30 minutos."'), ("POLICY", "External delivery requires human approval."), ("EVIDENCE", "queued → delivered → confirmed")]
    label, message = cards[min(3, max(0, int(progress * 5) - 1))]
    draw.rounded_rectangle((120, 760, 1800, 910), radius=25, fill=(14, 27, 45), outline=(44, 68, 94), width=2)
    draw.text((158, 790), label, fill=TEAL, font=font(20, True))
    draw.text((158, 836), message, fill=WHITE, font=font(28))
    draw.text((120, 980), "Human-controlled  ·  Auditable  ·  Provider-agnostic", fill=MUTED, font=font(22))
    draw.text((1450, 980), "OFFLINE / NO PROVIDERS", fill=TEAL, font=font(20, True))
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    mp4, poster = OUT / "attention-router-demo.mp4", OUT / "attention-router-demo-poster.png"
    process = subprocess.Popen(["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert process.stdin is not None
    for number in range(72 * FPS):
        current = frame(number / (72 * FPS - 1))
        if number == 0:
            current.save(poster)
        process.stdin.write(current.tobytes())
    process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError(process.stderr.read().decode())
    print("DEMO_GENERATION=PASS")
    print("DEMO_DURATION_SECONDS=72")
    print(f"MP4={mp4}")
    print(f"POSTER={poster}")


if __name__ == "__main__":
    main()
