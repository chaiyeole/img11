#!/usr/bin/env python3
"""Build thumbs/, full/, and manifest.json for the photo portfolio site.

Full rebuild every run (content/ -> dist/): simplest option that avoids
stale/orphaned output files when photos are renamed or deleted. Intended
for personal-scale photo libraries (dozens to low hundreds of photos);
revisit with incremental/mtime-based skipping only if that stops being fast.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

HEIC_EXTENSIONS = {".heic", ".heif"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"} | HEIC_EXTENSIONS
THUMB_MAX_EDGE = 800
THUMB_QUALITY = 75
THUMB_AVIF_QUALITY = 60
FULL_MAX_EDGE = 2200
FULL_QUALITY = 85
FULL_AVIF_QUALITY = 70
WEBP_METHOD = 6


def slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "untitled"


def natural_sort_key(name: str) -> list:
    """Split a filename into text/number chunks so "2.jpg" sorts before
    "10.jpg" -- a plain string sort would otherwise put "10" before "2"."""
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", name)]


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


ALBUM_CSV_COLUMN_ALIASES = {
    "order": {"srno", "sno", "order", "slno", "serialno"},
    "folder": {"foldername", "folder", "album", "albumname", "name"},
    "dateLabel": {"monthyear", "month", "date", "monthyr"},
    "description": {"description", "desc", "notes"},
}


def _normalize(text: str) -> str:
    """Lowercase and strip everything but letters/digits, for forgiving matching.

    Used both for CSV column headers (so "Sr. No." matches "Sr No") and for
    matching a CSV row's Folder Name against an actual content/ folder name
    when they differ only by spaces, underscores, hyphens, or casing
    (e.g. "NGP_1" / "NGP 1" / "ngp-1" all normalize the same).
    """
    return re.sub(r"[^a-z0-9]", "", (text or "").strip().lower())


def load_album_overrides(csv_path: Path) -> dict[str, dict]:
    """Read content/albums.csv for manual order / date-label / description overrides.

    Column headers are matched case-insensitively, ignoring spacing/punctuation
    (e.g. "Sr. No." and "Sr No" both match), so the user doesn't need to get the
    exact header text right. Returns {folder name: {"order", "dateLabel",
    "description"}}. A missing file, unreadable row, or unmatched column is
    warned about, never fatal -- albums just fall back to auto-detected date
    and no description, same as before this feature existed.
    """
    if not csv_path.exists():
        return {}

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return {}

        field_for_col: dict[int, str] = {}
        for i, h in enumerate(header):
            key = _normalize(h)
            for field, aliases in ALBUM_CSV_COLUMN_ALIASES.items():
                if key in aliases:
                    field_for_col[i] = field
                    break

        overrides: dict[str, dict] = {}
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            values: dict[str, str] = {}
            for i, cell in enumerate(row):
                field = field_for_col.get(i)
                if field:
                    values[field] = cell.strip()

            folder = values.get("folder")
            if not folder:
                continue

            order = None
            order_raw = values.get("order")
            if order_raw:
                try:
                    order = int(order_raw)
                except ValueError:
                    print(
                        f"warning: albums.csv row for '{folder}' has non-numeric Sr No '{order_raw}', ignoring order",
                        file=sys.stderr,
                    )

            overrides[folder] = {
                "order": order,
                "dateLabel": values.get("dateLabel") or None,
                "description": values.get("description") or None,
            }

        return overrides


def match_override(album_name: str, overrides: dict[str, dict]) -> tuple[str, dict] | None:
    """Find album_name's row in albums.csv: exact match first, then a
    normalized match (ignoring case/spaces/underscores/hyphens/symbols) so
    e.g. "NGP_1" in the CSV still matches a "NGP 1" folder. Returns the
    matched CSV folder-name key alongside its override, so the caller can
    track which CSV rows were actually used."""
    if album_name in overrides:
        return album_name, overrides[album_name]
    target = _normalize(album_name)
    for folder_key, val in overrides.items():
        if _normalize(folder_key) == target:
            return folder_key, val
    return None


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


def encode_avif(img: Image.Image, out_path: Path, quality: int) -> bool:
    """Encode an already-resized image to AVIF via the `avifenc` CLI.

    Pillow has no AVIF encoder on this system, so this mirrors the HEIC
    (heif-convert) pattern: shell out to a CLI tool rather than a Python lib.
    Explicitly uses 4:2:0 chroma subsampling -- avifenc's "auto" default is
    4:4:4 for non-JPEG input, which bloats file size with no visual benefit
    for photographic content (verified: 4:4:4 produced AVIF files *larger*
    than the existing WebP; 4:2:0 at these quality levels is smaller, with
    no visible difference in side-by-side crops). Returns False (and leaves
    no file behind) if avifenc is missing or fails, so callers degrade to
    WebP-only rather than failing the whole build.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        img.save(tmp_path, "PNG")
        subprocess.run(
            ["avifenc", "-q", str(quality), "-y", "420", "-s", "6", str(tmp_path), str(out_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"warning: failed to encode AVIF for {out_path}: {detail}", file=sys.stderr)
        out_path.unlink(missing_ok=True)
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def save_resized(
    img: Image.Image, out_path: Path, max_edge: int, quality: int, avif_quality: int
) -> tuple[int, int, bool]:
    resized = img.copy()
    resized.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    resized.save(out_path, "WEBP", quality=quality, method=WEBP_METHOD)
    avif_ok = encode_avif(resized, out_path.with_suffix(".avif"), avif_quality)
    return resized.width, resized.height, avif_ok


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

    album_overrides = load_album_overrides(content_dir / "albums.csv")
    matched_csv_folders: set[str] = set()

    albums = []
    taken_album_slugs: set[str] = set()

    album_dirs = sorted(
        (p for p in content_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: natural_sort_key(p.name),
    )

    for album_path in album_dirs:
        album_name = album_path.name
        album_slug = unique_slug(slugify(album_name), taken_album_slugs)

        photo_paths = sorted(
            (p for p in album_path.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
            key=lambda p: natural_sort_key(p.name),
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

            thumb_w, thumb_h, thumb_avif_ok = save_resized(
                img, dist_dir / thumb_rel, THUMB_MAX_EDGE, THUMB_QUALITY, THUMB_AVIF_QUALITY
            )
            full_w, full_h, full_avif_ok = save_resized(
                img, dist_dir / full_rel, FULL_MAX_EDGE, FULL_QUALITY, FULL_AVIF_QUALITY
            )

            photos.append(
                {
                    "slug": photo_slug,
                    "thumb": thumb_rel,
                    "thumbAvif": thumb_rel[: -len(".webp")] + ".avif" if thumb_avif_ok else None,
                    "full": full_rel,
                    "fullAvif": full_rel[: -len(".webp")] + ".avif" if full_avif_ok else None,
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

        match = match_override(album_name, album_overrides)
        override = None
        if match is not None:
            matched_key, override = match
            matched_csv_folders.add(matched_key)
        else:
            print(
                f"note: album '{album_name}' is not listed in albums.csv -- showing at the end, ordered by date",
                file=sys.stderr,
            )

        albums.append(
            {
                "name": album_name,
                "slug": album_slug,
                "date": album_date.strftime("%Y-%m-%d"),
                "order": override["order"] if override else None,
                "dateLabel": override["dateLabel"] if override else None,
                "description": override["description"] if override else None,
                "photoCount": len(photos),
                "photos": photos,
            }
        )
        print(f"album '{album_name}' ({album_slug}): {len(photos)} photo(s), date {album_date:%Y-%m-%d}")

    unmatched_csv_rows = set(album_overrides) - matched_csv_folders
    for folder_key in sorted(unmatched_csv_rows):
        print(
            f"note: albums.csv lists '{folder_key}' but no folder in content/ matches it "
            "(even ignoring case/spaces/underscores/symbols) -- check for a typo or renamed folder",
            file=sys.stderr,
        )

    ordered = [a for a in albums if a["order"] is not None]
    unordered = [a for a in albums if a["order"] is None]
    ordered.sort(key=lambda a: a["order"])
    unordered.sort(key=lambda a: a["date"], reverse=True)
    albums = ordered + unordered

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
