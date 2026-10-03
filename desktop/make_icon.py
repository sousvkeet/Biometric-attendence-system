"""Draws the BioAttend icon (green rounded square with a white fingerprint) -> desktop/bioattend.ico, .png and .icns"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SIZE = 256


def draw():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, SIZE - 8, SIZE - 8), radius=56, fill=(79, 154, 44, 255))
    cx, cy = SIZE // 2, SIZE // 2 + 10
    # concentric fingerprint ridges (open at the bottom)
    for i, r in enumerate(range(20, 100, 16)):
        start, end = (200, 340) if i % 2 else (190, 350)
        d.arc((cx - r, cy - r - 10, cx + r, cy + r + 10), start=start, end=end, fill="white", width=9)
        d.arc((cx - r, cy - r - 10, cx + r, cy + r + 10), start=0, end=150 - i * 12, fill="white", width=9)
    return img


if __name__ == "__main__":
    image = draw()
    image.save(HERE / "bioattend.png")
    image.save(HERE / "bioattend.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    try:  # macOS app icon (desktop/build_macos.sh falls back to iconutil when this is not possible)
        image.resize((512, 512), Image.LANCZOS).save(HERE / "bioattend.icns")
    except Exception as exc:
        print(f"no .icns written: {exc}")
    print("icon written")
