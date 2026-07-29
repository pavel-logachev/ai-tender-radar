from __future__ import annotations

from pathlib import Path
from random import Random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets"
OUT.mkdir(parents=True, exist_ok=True)

INK = "#111735"
INK_2 = "#171F43"
GRID = "#374168"
WHITE = "#F7F9FF"
MUTED = "#AAB4D1"
CYAN = "#BFF5EA"
GREEN = "#49D6B2"
AMBER = "#F3B95F"
VIOLET = "#6D66E8"
PAPER = "#F1F4FA"
PAPER_LINE = "#D9DFEB"
PAPER_INK = "#1A2340"
FONT_DIR = Path("C:/Windows/Fonts")
SANS = FONT_DIR / "bahnschrift.ttf"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(SANS), size=size)


def tracking(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, face: ImageFont.FreeTypeFont, fill: str, spacing: int) -> None:
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=face, fill=fill)
        x += int(draw.textlength(char, font=face)) + spacing


def grid(draw: ImageDraw.ImageDraw, size: tuple[int, int], margin: int, step: int = 64) -> None:
    for x in range(margin, size[0] - margin + 1, step):
        draw.line((x, margin, x, size[1] - margin), fill=GRID, width=1)
    for y in range(margin, size[1] - margin + 1, step):
        draw.line((margin, y, size[0] - margin, y), fill=GRID, width=1)


def noise(size: tuple[int, int], opacity: int = 7) -> Image.Image:
    rng = Random(240729)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    pixels = layer.load()
    for y in range(size[1]):
        for x in range(size[0]):
            value = rng.randrange(opacity + 1)
            pixels[x, y] = (191, 245, 234, value)
    return layer.filter(ImageFilter.GaussianBlur(0.35))


def radar(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int) -> None:
    cx, cy = center
    for factor in (1.0, 0.72, 0.44):
        r = int(radius * factor)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=GRID, width=2)
    draw.line((cx - radius, cy, cx + radius, cy), fill=GRID, width=1)
    draw.line((cx, cy - radius, cx, cy + radius), fill=GRID, width=1)
    draw.pieslice((cx - radius, cy - radius, cx + radius, cy + radius), start=300, end=360, fill="#232D57", outline=None)
    for x, y, color, r in [
        (cx + 96, cy - 52, GREEN, 12),
        (cx - 70, cy + 72, AMBER, 10),
        (cx + 38, cy + 42, CYAN, 8),
    ]:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
        draw.ellipse((x - r - 9, y - r - 9, x + r + 9, y + r + 9), outline=color, width=2)


def render_banner() -> None:
    size = (1600, 440)
    image = Image.new("RGBA", size, INK)
    draw = ImageDraw.Draw(image)
    grid(draw, size, 48)
    draw.rectangle((0, 0, 12, size[1]), fill=VIOLET)
    draw.rectangle((12, 0, 16, size[1]), fill=GREEN)
    tracking(draw, (84, 44), "PROCUREMENT INTELLIGENCE / PYTHON", font(18), MUTED, 2)
    draw.text((80, 104), "AI Tender Radar", font=font(65), fill=WHITE)
    draw.text((84, 194), "FROM PROCUREMENT NOISE TO AN AUDITABLE LEAD QUEUE.", font=font(23), fill=CYAN)
    draw.text((84, 248), "Collect / validate evidence / triage / report / learn", font=font(21), fill=MUTED)
    for index, (label, color) in enumerate([("SOURCE", VIOLET), ("DOCS", AMBER), ("AI", GREEN), ("ACTION", CYAN)]):
        x = 84 + index * 180
        draw.rounded_rectangle((x, 328, x + 150, 370), radius=19, fill=INK_2, outline=color, width=2)
        tracking(draw, (x + 18, 339), label, font(14), color, 1)
    draw.line((1002, 64, 1002, 376), fill=GRID, width=1)
    radar(draw, (1282, 220), 154)
    image = Image.alpha_composite(image, noise(size)).convert("RGB")
    image.save(OUT / "ai-tender-radar-banner.png", optimize=True, quality=94)


def render_social() -> None:
    size = (1280, 640)
    image = Image.new("RGBA", size, INK)
    draw = ImageDraw.Draw(image)
    grid(draw, size, 54, 72)
    draw.rectangle((0, 0, 12, size[1]), fill=VIOLET)
    draw.rectangle((12, 0, 16, size[1]), fill=GREEN)
    tracking(draw, (84, 62), "PRODUCTION ENGINEERING / PROCUREMENT", font(18), MUTED, 2)
    draw.text((80, 148), "AI Tender", font=font(76), fill=WHITE)
    draw.text((80, 232), "Radar", font=font(76), fill=WHITE)
    draw.text((84, 360), "Bounded acquisition. Evidence-first AI.", font=font(26), fill=CYAN)
    draw.text((84, 404), "Auditable lead workflow.", font=font(26), fill=CYAN)
    tracking(draw, (84, 548), "COLLECT / TRIAGE / REPORT / FEEDBACK", font(16), MUTED, 2)
    radar(draw, (1004, 322), 198)
    image = Image.alpha_composite(image, noise(size)).convert("RGB")
    image.save(OUT / "ai-tender-radar-social-preview.png", optimize=True, quality=94)


def badge(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, fill: str, face_size: int = 14) -> None:
    face = font(face_size)
    height = face_size + 18
    padding = face_size + 16
    width = int(draw.textlength(text, font=face)) + padding
    draw.rounded_rectangle((x, y, x + width, y + height), radius=height // 2, fill=fill)
    draw.text((x + padding // 2, y + 7), text, font=face, fill=WHITE)


def render_product() -> None:
    size = (1440, 920)
    image = Image.new("RGB", size, PAPER)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0], 176), fill=INK)
    draw.rectangle((0, 0, 12, size[1]), fill=VIOLET)
    draw.rectangle((12, 0, 16, size[1]), fill=GREEN)
    tracking(draw, (72, 36), "SYNTHETIC PRODUCT EVIDENCE", font(17), MUTED, 2)
    draw.text((68, 78), "A lead queue with traceable evidence", font=font(43), fill=WHITE)

    columns = [(76, "SIGNAL"), (330, "DOCUMENTS"), (664, "AI TRIAGE"), (1038, "NEXT ACTION")]
    for x, title in columns:
        tracking(draw, (x, 222), title, font(16), "#66708B", 2)
    draw.line((68, 258, 1372, 258), fill=PAPER_LINE, width=2)

    rows = [
        ("TR-2401", "Storage platform", "Technical spec + contract", "GO / high evidence", "Prepare discovery call", GREEN),
        ("TR-2402", "Server refresh", "Specification selected", "MAYBE / clarify scope", "Ask 3 technical questions", AMBER),
        ("TR-2403", "License renewal", "No hardware evidence", "NO-GO / service noise", "Keep out of lead queue", VIOLET),
    ]
    for index, (record, subject, docs, triage, action, color) in enumerate(rows):
        top = 286 + index * 168
        draw.rounded_rectangle((68, top, 1372, top + 132), radius=18, fill="#FFFFFF", outline=PAPER_LINE, width=2)
        draw.text((82, top + 22), record, font=font(18), fill=PAPER_INK)
        draw.text((82, top + 56), subject, font=font(19), fill="#4D5874")
        draw.text((330, top + 34), docs, font=font(18), fill=PAPER_INK)
        badge(draw, 664, top + 26, triage, color)
        draw.text((1038, top + 34), action, font=font(18), fill=PAPER_INK)
        draw.text((330, top + 72), "validated • bounded download", font=font(15), fill="#7A849D")
        draw.text((664, top + 76), "contract + reason + evidence refs", font=font(15), fill="#7A849D")
        draw.text((1038, top + 72), "human feedback retained", font=font(15), fill="#7A849D")

    draw.line((68, 828, 1372, 828), fill=PAPER_LINE, width=2)
    tracking(draw, (68, 850), "BOUNDED ACQUISITION / STRICT CONTRACTS / HUMAN FEEDBACK", font(15), PAPER_INK, 2)
    image.save(OUT / "ai-tender-radar-product.png", optimize=True, quality=94)


def render_product_mobile() -> None:
    size = (800, 1280)
    image = Image.new("RGB", size, PAPER)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0], 210), fill=INK)
    draw.rectangle((0, 0, 10, size[1]), fill=VIOLET)
    draw.rectangle((10, 0, 14, size[1]), fill=GREEN)
    tracking(draw, (48, 36), "SYNTHETIC PRODUCT EVIDENCE", font(16), MUTED, 2)
    draw.text((46, 84), "Traceable evidence", font=font(42), fill=WHITE)
    draw.text((48, 144), "from signal to action", font=font(30), fill=CYAN)

    rows = [
        ("TR-2401", "Storage platform", "Technical spec + contract", "GO / high evidence", "Prepare discovery call", GREEN),
        ("TR-2402", "Server refresh", "Specification selected", "MAYBE / clarify scope", "Ask 3 technical questions", AMBER),
        ("TR-2403", "License renewal", "No hardware evidence", "NO-GO / service noise", "Keep out of lead queue", VIOLET),
    ]
    for index, (record, subject, docs, triage, action, color) in enumerate(rows):
        top = 246 + index * 294
        draw.rounded_rectangle((40, top, 760, top + 254), radius=20, fill="#FFFFFF", outline=PAPER_LINE, width=2)
        draw.text((64, top + 24), record, font=font(22), fill=PAPER_INK)
        draw.text((64, top + 58), subject, font=font(26), fill="#4D5874")
        badge(draw, 64, top + 106, triage, color, face_size=20)
        draw.text((64, top + 158), docs, font=font(20), fill=PAPER_INK)
        draw.text((64, top + 194), action, font=font(20), fill=PAPER_INK)
        draw.text((540, top + 208), "feedback retained", font=font(14), fill="#7A849D")

    draw.line((40, 1152, 760, 1152), fill=PAPER_LINE, width=2)
    tracking(draw, (40, 1182), "BOUNDED / AUDITABLE / HUMAN-REVIEWED", font(15), PAPER_INK, 1)
    image.save(OUT / "ai-tender-radar-product-mobile.png", optimize=True, quality=94)


if __name__ == "__main__":
    render_banner()
    render_social()
    render_product()
    render_product_mobile()
    print("Rendered AI Tender Radar GitHub assets")
