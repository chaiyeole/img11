#!/usr/bin/env python3
"""DEV-ONLY scaffolding: generate a few sample albums under content/ for
testing the build pipeline and front-end before real photos exist.

Not used by CI / the GitHub Actions workflow. Safe to re-run: refuses to
touch an album folder that already exists so it never clobbers real photos.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ALBUMS = {
    "Landscapes": [
        {"size": (1600, 1067), "color": (76, 130, 173), "label": "Landscapes 1"},
        {"size": (1600, 1067), "color": (94, 158, 110), "label": "Landscapes 2"},
        {"size": (1600, 1067), "color": (210, 160, 70), "label": "Landscapes 3"},
    ],
    "Portraits": [
        {"size": (1067, 1600), "color": (160, 90, 120), "label": "Portraits 1"},
        {"size": (1067, 1600), "color": (120, 100, 160), "label": "Portraits 2"},
    ],
    "Street": [
        {"size": (1600, 1067), "color": (90, 90, 90), "label": "Street 1"},
        {"size": (1600, 1067), "color": (60, 60, 70), "label": "Street 2"},
    ],
}


def make_image(size: tuple[int, int], color: tuple[int, int, int], label: str) -> Image.Image:
    img = Image.new("RGB", size, color=color)
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.rectangle([w * 0.1, h * 0.1, w * 0.9, h * 0.9], outline=(255, 255, 255), width=8)
    draw.ellipse([w * 0.35, h * 0.3, w * 0.65, h * 0.6], fill=(255, 255, 255, 128))
    draw.text((w * 0.1, h * 0.85), label, fill=(255, 255, 255))
    return img


def main() -> None:
    content_dir = Path("content")
    content_dir.mkdir(exist_ok=True)

    for album_name, photos in ALBUMS.items():
        album_dir = content_dir / album_name
        if album_dir.exists():
            print(f"skip: {album_dir} already exists")
            continue
        album_dir.mkdir(parents=True)
        for i, spec in enumerate(photos, start=1):
            img = make_image(spec["size"], spec["color"], spec["label"])
            out_path = album_dir / f"photo{i:02d}.jpg"
            img.save(out_path, "JPEG", quality=90)
        print(f"created {album_dir} with {len(photos)} placeholder photo(s)")


if __name__ == "__main__":
    main()
