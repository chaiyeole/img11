#!/usr/bin/env python3
"""Build thumbs/, full/, and manifest.json for the photo portfolio site.

Full rebuild every run (content/ -> dist/): simplest option that avoids
stale/orphaned output files when photos are renamed or deleted. Intended
for personal-scale photo libraries (dozens to low hundreds of photos);
revisit with incremental/mtime-based skipping only if that stops being fast.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

HEIC_EXTENSIONS = {".heic", ".heif"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"} | HEIC_EXTENSIONS
THUMB_MAX_EDGE = 800
THUMB_QUALITY = 75
FULL_MAX_EDGE = 2200
FULL_QUALITY = 85
WEBP_METHOD = 6


def slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "untitled"


def unique_slug(base: str, taken: set[str]) -> str:
    if base not in taken:
        taken.add(base)
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    candidate = f"{base}-{i}"
    taken.add(candidate)
    return candidate


def get_capture_date(path: Path) -> datetime | None:
    try:
        exif = Image.open(path).getexif()
        if not exif:
            return None
        exif_ifd = exif.get_ifd(0x8769)
        raw = exif_ifd.get(36867) or exif_ifd.get(36868) or exif.get(306)
        if raw:
            return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - a missing/malformed date just means fall back
        pass
    return None


def convert_heic_in_place(path: Path) -> Path | None:
    """Convert a HEIC/HEIF source photo to JPEG in place, once, via heif-convert.

    heif-convert embeds the source's EXIF (DateTimeOriginal, Orientation) into
    the output JPEG by default, so the album-date feature and exif_transpose
    keep working unchanged. Deletes the original .heic/.heif on success, so
    content/ settles into a single format and later builds never reconvert it.
    Returns the new .jpg path, or None if conversion failed (caller skips the
    photo with a warning rather than failing the whole build).
    """
    jpg_path = path.with_suffix(".jpg")
    if jpg_path.exists():
        print(f"warning: {jpg_path} already exists, skipping HEIC conversion of {path}", file=sys.stderr)
        return None
    try:
        subprocess.run(
            ["heif-convert", str(path), str(jpg_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"warning: failed to convert HEIC {path}: {detail}", file=sys.stderr)
        jpg_path.unlink(missing_ok=True)
        return None
    path.unlink()
    return jpg_path


def strip_gps_in_place(path: Path) -> None:
    """Remove GPS EXIF (if present) from a source JPEG in content/, in place.

    Only touches the EXIF APP1 segment's GPS IFD; DateTimeOriginal/Orientation
    are left untouched since the album-date feature and exif_transpose depend
    on them. Only re-saves when GPS data is actually present, so files that
    have already been scrubbed aren't re-encoded on every subsequent build.
    """
    if path.suffix.lower() not in (".jpg", ".jpeg"):
        return
    try:
        img = Image.open(path)
        exif = img.getexif()
        if 0x8825 not in exif:
            return
        exif.pop(0x8825, None)
        img.save(path, exif=exif.tobytes(), quality="keep")
    except Exception as exc:  # noqa: BLE001 - never block the build over metadata scrubbing
        print(f"warning: failed to strip GPS data from {path}: {exc}", file=sys.stderr)


def load_image(path: Path) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    return img


def save_resized(img: Image.Image, out_path: Path, max_edge: int, quality: int) -> tuple[int, int]:
    resized = img.copy()
    resized.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    resized.save(out_path, "WEBP", quality=quality, method=WEBP_METHOD)
    return resized.width, resized.height


def copy_web_files(web_dir: Path, dist_dir: Path) -> None:
    if not web_dir.is_dir():
        print(f"warning: web dir not found: {web_dir}, skipping front-end copy", file=sys.stderr)
        return
    for item in web_dir.iterdir():
        dest = dist_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            dist_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)


def build(content_dir: Path, dist_dir: Path, web_dir: Path) -> None:
    if not content_dir.is_dir():
        print(f"error: content dir not found: {content_dir}", file=sys.stderr)
        sys.exit(1)

    shutil.rmtree(dist_dir, ignore_errors=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    copy_web_files(web_dir, dist_dir)

    thumbs_dir = dist_dir / "thumbs"
    full_dir = dist_dir / "full"

    albums = []
    taken_album_slugs: set[str] = set()

    album_dirs = sorted(
        (p for p in content_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )

    for album_path in album_dirs:
        album_name = album_path.name
        album_slug = unique_slug(slugify(album_name), taken_album_slugs)

        photo_paths = sorted(
            (p for p in album_path.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
            key=lambda p: p.name.lower(),
        )

        photos = []
        taken_photo_slugs: set[str] = set()
        capture_dates: list[datetime] = []

        for photo_path in photo_paths:
            if photo_path.suffix.lower() in HEIC_EXTENSIONS:
                converted = convert_heic_in_place(photo_path)
                if converted is None:
                    continue
                photo_path = converted

            strip_gps_in_place(photo_path)

            try:
                img = load_image(photo_path)
            except Exception as exc:  # noqa: BLE001 - one bad photo shouldn't fail the build
                print(f"warning: skipping unreadable image {photo_path}: {exc}", file=sys.stderr)
                continue

            capture_date = get_capture_date(photo_path)
            if capture_date:
                capture_dates.append(capture_date)

            photo_slug = unique_slug(slugify(photo_path.stem), taken_photo_slugs)

            thumb_rel = f"thumbs/{album_slug}/{photo_slug}.webp"
            full_rel = f"full/{album_slug}/{photo_slug}.webp"

            thumb_w, thumb_h = save_resized(img, dist_dir / thumb_rel, THUMB_MAX_EDGE, THUMB_QUALITY)
            full_w, full_h = save_resized(img, dist_dir / full_rel, FULL_MAX_EDGE, FULL_QUALITY)

            photos.append(
                {
                    "slug": photo_slug,
                    "thumb": thumb_rel,
                    "full": full_rel,
                    "width": full_w,
                    "height": full_h,
                    "thumbWidth": thumb_w,
                    "thumbHeight": thumb_h,
                }
            )

        if not photos:
            print(f"warning: album '{album_name}' has no valid images, skipping", file=sys.stderr)
            continue

        album_date = min(capture_dates) if capture_dates else datetime.fromtimestamp(album_path.stat().st_mtime)

        albums.append(
            {
                "name": album_name,
                "slug": album_slug,
                "date": album_date.strftime("%Y-%m-%d"),
                "photoCount": len(photos),
                "photos": photos,
            }
        )
        print(f"album '{album_name}' ({album_slug}): {len(photos)} photo(s), date {album_date:%Y-%m-%d}")

    albums.sort(key=lambda a: a["date"], reverse=True)

    manifest = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "albums": albums,
    }
    (dist_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total_photos = sum(a["photoCount"] for a in albums)
    print(f"done: {len(albums)} album(s), {total_photos} photo(s) -> {dist_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content-dir", default="content", type=Path)
    parser.add_argument("--dist-dir", default="dist", type=Path)
    parser.add_argument("--web-dir", default="web", type=Path)
    args = parser.parse_args()
    build(args.content_dir, args.dist_dir, args.web_dir)


if __name__ == "__main__":
    main()
