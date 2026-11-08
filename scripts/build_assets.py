#!/usr/bin/env python3
"""Build thumbs/, full/, and manifest.json for the photo portfolio site.

dist/ itself is always fully rebuilt from content/ -- no orphaned/stale
output files when photos are renamed or deleted. The expensive step (resize
+ WebP/AVIF encode) is cached separately in --cache-dir, keyed by each
source photo's content hash, so unchanged photos are copied from cache
instead of re-encoded. That cache dir is meant to be persisted across CI
runs (e.g. via actions/cache) -- without it (or on a cold cache), behavior
and output are identical, just slower.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

HEIC_EXTENSIONS = {".heic", ".heif"}
RAW_EXTENSIONS = {".raf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"} | HEIC_EXTENSIONS | RAW_EXTENSIONS
THUMB_MAX_EDGE = 800
THUMB_QUALITY = 75
THUMB_AVIF_QUALITY = 60
FULL_MAX_EDGE = 2200
FULL_QUALITY = 85
FULL_AVIF_QUALITY = 70
WEBP_METHOD = 6

# Bump this if THUMB_*/FULL_*/WEBP_METHOD or the encode logic changes, so old
# cached outputs (encoded with different settings) don't get reused silently.
CACHE_VERSION = "v1"


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


BW_TAG = "b&w"

# Mean HSV saturation (0-1) below which a photo counts as black & white.
# Measured on the current library: true grayscale exports sit at <= 0.026,
# the least-saturated colour photo at 0.057. Toned/tinted B&W (warm-tinted
# exports) overlaps colour photos on every cheap metric tried, so those are
# tagged by hand in tags.csv instead of loosening this.
MONO_SATURATION_MAX = 0.03

TAGS_CSV_COLUMN_ALIASES = {
    "folder": {"foldername", "folder", "album", "albumname"},
    "file": {"file", "filename", "photo", "image", "name"},
    "tags": {"tags", "tag", "keywords"},
}

# Spellings that should collapse into one tag, so the Tags index doesn't
# grow near-duplicates ("cloud" vs "clouds").
TAG_ALIASES = {
    "bw": BW_TAG,
    "b/w": BW_TAG,
    "black and white": BW_TAG,
    "black & white": BW_TAG,
    "monochrome": BW_TAG,
    "mono": BW_TAG,
    "cloud": "clouds",
    "person": "people",
    "tree": "trees",
    "mountain": "mountains",
    "hills": "mountains",
    "animal": "animals",
    "car": "cars",
    "reflection": "reflections",
    "light": "lights",
    "sunrise": "sunset",
    "cats": "cat",
}


def is_monochrome(img: Image.Image) -> bool:
    small = img.convert("RGB")
    small.thumbnail((64, 64))
    saturation = ImageStat.Stat(small.convert("HSV")).mean[1] / 255
    return saturation < MONO_SATURATION_MAX


def normalize_tag(raw: str) -> str:
    tag = re.sub(r"\s+", " ", raw.strip().lower())
    return TAG_ALIASES.get(tag, tag)


def tag_slug(tag: str) -> str:
    # slugify("b&w") would give "b-w"; "bw" reads better in a URL.
    return "bw" if tag == BW_TAG else slugify(tag)


def load_photo_tags(csv_path: Path) -> dict[tuple[str, str], dict]:
    """Read content/tags.csv: one row per photo, tags separated by ';'.

    Returns {(normalized folder, normalized file stem): {"add": [...],
    "remove": set(...), "label": "Folder/File"}}. A tag written with a
    leading '-' (e.g. "-b&w") removes an auto-detected tag. Like albums.csv,
    a missing file or bad row is warned about, never fatal.
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
            for field, aliases in TAGS_CSV_COLUMN_ALIASES.items():
                if key in aliases:
                    field_for_col[i] = field
                    break

        if set(field_for_col.values()) != set(TAGS_CSV_COLUMN_ALIASES):
            print(
                f"warning: {csv_path} needs Folder Name, File and Tags columns -- ignoring it",
                file=sys.stderr,
            )
            return {}

        result: dict[tuple[str, str], dict] = {}
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            values = {field_for_col[i]: cell.strip() for i, cell in enumerate(row) if i in field_for_col}
            folder, file = values.get("folder"), values.get("file")
            if not folder or not file:
                continue

            add: list[str] = []
            remove: set[str] = set()
            for raw in values.get("tags", "").split(";"):
                if not raw.strip():
                    continue
                if raw.strip().startswith("-"):
                    remove.add(normalize_tag(raw.strip()[1:]))
                else:
                    tag = normalize_tag(raw)
                    if tag not in add:
                        add.append(tag)

            key = (_normalize(folder), _normalize(Path(file).stem))
            result[key] = {"add": add, "remove": remove, "label": f"{folder}/{file}"}
        return result


def raf_embedded_jpeg(path: Path) -> bytes:
    """The camera-rendered JPEG inside a Fujifilm RAF file.

    The RAF header stores its offset and length as big-endian uint32s at bytes
    84 and 88. That JPEG already has the film simulation applied and carries
    the full EXIF + Fujifilm MakerNote, so the build uses it in place of
    decoding RAW data -- no extra dependency, and the .raf is never modified.
    """
    with path.open("rb") as f:
        header = f.read(92)
        if not header.startswith(b"FUJIFILMCCD-RAW"):
            raise ValueError(f"{path} is not a Fujifilm RAF file")
        offset, length = struct.unpack(">II", header[84:92])
        f.seek(offset)
        data = f.read(length)
    if not data.startswith(b"\xff\xd8"):
        raise ValueError(f"{path}: no embedded JPEG found at the expected offset")
    return data


def open_source(path: Path) -> Image.Image:
    """Image.open, except a .raf opens its embedded camera JPEG."""
    if path.suffix.lower() in RAW_EXTENSIONS:
        return Image.open(io.BytesIO(raf_embedded_jpeg(path)))
    return Image.open(path)


def get_capture_date(path: Path) -> datetime | None:
    try:
        exif = open_source(path).getexif()
        if not exif:
            return None
        exif_ifd = exif.get_ifd(0x8769)
        raw = exif_ifd.get(36867) or exif_ifd.get(36868) or exif.get(306)
        if raw:
            return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except Exception:  # noqa: BLE001 - a missing/malformed date just means fall back
        pass
    return None


def _fmt_number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


# Fujifilm MakerNote value tables, from exiftool's FujiFilm tag reference.
# Codes not listed here are skipped, never guessed.
FUJI_FILM_MODES = {
    0x000: "Provia", 0x100: "Studio Portrait", 0x110: "Studio Portrait Enhanced Saturation",
    0x120: "Astia", 0x130: "Studio Portrait Increased Sharpness", 0x200: "Velvia",
    0x300: "Studio Portrait Ex", 0x400: "Velvia", 0x500: "Pro Neg. Std", 0x501: "Pro Neg. Hi",
    0x600: "Classic Chrome", 0x700: "Eterna", 0x800: "Classic Negative", 0x900: "Eterna Bleach Bypass",
    0xA00: "Nostalgic Neg.", 0xB00: "Reala Ace",
}
# Monochrome simulations live in the Saturation tag, not FilmMode.
FUJI_MONO_MODES = {
    0x300: "Monochrome", 0x301: "Monochrome + R", 0x302: "Monochrome + Ye", 0x303: "Monochrome + G",
    0x310: "Sepia", 0x500: "Acros", 0x501: "Acros + R", 0x502: "Acros + Ye", 0x503: "Acros + G",
}
FUJI_GRAIN = {16: "Grain weak, small", 32: "Grain weak", 48: "Grain strong, small", 64: "Grain strong"}
FUJI_STRENGTH = {32: "weak", 64: "strong"}
FUJI_DR = {0x100: "DR100", 0x200: "DR200", 0x201: "DR400"}
FUJI_WB = {
    0x0: "Auto WB", 0x1: "Auto WB (white)", 0x2: "Auto WB (ambience)", 0x100: "Daylight WB",
    0x200: "Cloudy WB", 0x400: "Incandescent WB", 0x600: "Underwater WB", 0xFF0: "Kelvin WB",
}
FUJI_TONE_STEP = 16  # ShadowTone/HighlightTone: -16 per +1 step (e.g. -32 -> +2)

_TIFF_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 7: 1, 8: 2, 9: 4}


def read_fuji_makernote(note: bytes) -> dict[int, int]:
    """Parse a Fujifilm MakerNote: b"FUJIFILM", a little-endian uint32 offset to
    an IFD, then standard 12-byte IFD entries. Offsets are relative to the start
    of the note, so the note is self-contained. Returns {tag: value} for the
    single-value integer tags (all the recipe settings are)."""
    if not note.startswith(b"FUJIFILM") or len(note) < 14:
        return {}
    (ifd_offset,) = struct.unpack_from("<I", note, 8)
    (count,) = struct.unpack_from("<H", note, ifd_offset)
    values: dict[int, int] = {}
    for i in range(count):
        entry = ifd_offset + 2 + i * 12
        if entry + 12 > len(note):
            break
        tag, typ, n = struct.unpack_from("<HHI", note, entry)
        if n != 1 or typ not in _TIFF_SIZES or _TIFF_SIZES[typ] > 4:
            continue
        fmt = {1: "<B", 3: "<H", 4: "<I", 7: "<B", 8: "<h", 9: "<i"}.get(typ)
        if fmt:
            (values[tag],) = struct.unpack_from(fmt, note, entry + 8)
    return values


def fuji_recipe(exif_ifd) -> tuple[str | None, str | None]:
    """(film simulation, short recipe) from the MakerNote, or (None, None)."""
    note = exif_ifd.get(0x927C)
    if not isinstance(note, (bytes, bytearray)):
        return None, None
    try:
        tags = read_fuji_makernote(bytes(note))
    except struct.error:
        return None, None
    if not tags:
        return None, None

    film = FUJI_MONO_MODES.get(tags.get(0x1003, -1)) or FUJI_FILM_MODES.get(tags.get(0x1401, -1))

    def signed32(v: int) -> int:
        return v - (1 << 32) if v >= 1 << 31 else v

    parts = []
    if (grain := FUJI_GRAIN.get(signed32(tags.get(0x1047, 0)))):
        parts.append(grain)
    if (cc := FUJI_STRENGTH.get(signed32(tags.get(0x1048, 0)))):
        parts.append(f"Color chrome {cc}")
    if (ccb := FUJI_STRENGTH.get(signed32(tags.get(0x104E, 0)))):
        parts.append(f"Chrome blue {ccb}")
    for tag, name in ((0x1041, "Highlights"), (0x1040, "Shadows")):
        raw = tags.get(tag)
        if raw is not None and signed32(raw) % FUJI_TONE_STEP == 0 and signed32(raw) != 0:
            step = -signed32(raw) // FUJI_TONE_STEP
            parts.append(f"{name} {step:+d}")
    if (dr := FUJI_DR.get(tags.get(0x1402, -1))):
        parts.append(dr)
    if (wb := FUJI_WB.get(tags.get(0x1002, -1))):
        parts.append(wb)
    return film, (" · ".join(parts) or None)


def get_photo_meta(path: Path) -> dict | None:
    """Camera, settings and capture date from a source photo's EXIF, preformatted
    for the lightbox info panel. The web images are re-encoded without EXIF, so
    this is the only place it survives. Missing fields are simply omitted;
    returns None when there's nothing at all (e.g. exported/edited copies)."""
    try:
        exif = open_source(path).getexif()
    except Exception:  # noqa: BLE001 - metadata is optional
        return None
    if not exif:
        return None
    ifd = exif.get_ifd(0x8769)
    meta: dict[str, str] = {}

    make = str(exif.get(271) or "").strip()
    model = str(exif.get(272) or "").strip()
    if model:
        # "Canon" + "Canon EOS 1500D" -> "Canon EOS 1500D"; "FUJIFILM" + "X-S20" -> "Fujifilm X-S20"
        if make and not model.lower().startswith(make.lower().split()[0]):
            model = f"{make.title() if make.isupper() else make} {model}"
        meta["camera"] = model

    # Real focal length, as printed on the lens. Phones report a tiny real
    # value (~6mm) that means little to a viewer, so for those (< 10mm) use
    # the 35mm-equivalent when the camera records one.
    focal = ifd.get(0x920A)
    if (not focal or float(focal) < 10) and ifd.get(0xA405):
        focal = ifd.get(0xA405)
    if focal:
        meta["focal"] = f"{_fmt_number(float(focal))}mm"

    f_number = ifd.get(0x829D)
    if f_number:
        meta["aperture"] = f"f/{_fmt_number(float(f_number))}"

    exposure = ifd.get(0x829A)
    if exposure:
        exposure = float(exposure)
        meta["shutter"] = f"1/{round(1 / exposure)}s" if 0 < exposure < 1 else f"{_fmt_number(exposure)}s"

    iso = ifd.get(0x8827)
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    if iso:
        meta["iso"] = f"ISO {int(iso)}"

    bias = ifd.get(0x9204)
    if bias is not None:
        try:
            bias = float(bias)
        except (TypeError, ValueError, ZeroDivisionError):
            bias = 0.0
        if abs(bias) >= 0.05:
            meta["ev"] = f"{bias:+.1f} EV".replace(".0 EV", " EV")

    film, recipe = fuji_recipe(ifd)
    if film:
        meta["film"] = film
    if recipe:
        meta["recipe"] = recipe

    taken = get_capture_date(path)
    if taken:
        meta["taken"] = taken.strftime("%Y-%m-%dT%H:%M")

    return meta or None


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
    img = open_source(path)
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


def source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:20]


def cache_tier_paths(cache_dir: Path | None, src_hash: str, tier: str) -> tuple[Path, Path] | None:
    if cache_dir is None:
        return None
    d = cache_dir / CACHE_VERSION / src_hash / tier
    return d / "img.webp", d / "img.avif"


def save_resized(
    img: Image.Image,
    out_path: Path,
    max_edge: int,
    quality: int,
    avif_quality: int,
    cache_dir: Path | None,
    src_hash: str,
    tier: str,
) -> tuple[int, int, bool, bool]:
    """Resize+encode (WebP always, AVIF best-effort), or reuse a cached copy
    from an earlier build of the exact same source photo. Returns (width,
    height, avif_ok, cache_hit)."""
    cache_paths = cache_tier_paths(cache_dir, src_hash, tier)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_paths is not None:
        cached_webp, cached_avif = cache_paths
        if cached_webp.exists():
            shutil.copy2(cached_webp, out_path)
            with Image.open(out_path) as cached_img:
                width, height = cached_img.size
            avif_ok = cached_avif.exists()
            if avif_ok:
                shutil.copy2(cached_avif, out_path.with_suffix(".avif"))
            return width, height, avif_ok, True

    resized = img.copy()
    resized.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    resized.save(out_path, "WEBP", quality=quality, method=WEBP_METHOD)
    avif_ok = encode_avif(resized, out_path.with_suffix(".avif"), avif_quality)

    if cache_paths is not None:
        cached_webp, cached_avif = cache_paths
        cached_webp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_path, cached_webp)
        if avif_ok:
            shutil.copy2(out_path.with_suffix(".avif"), cached_avif)

    return resized.width, resized.height, avif_ok, False


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


def build(content_dir: Path, dist_dir: Path, web_dir: Path, cache_dir: Path | None) -> None:
    if not content_dir.is_dir():
        print(f"error: content dir not found: {content_dir}", file=sys.stderr)
        sys.exit(1)

    shutil.rmtree(dist_dir, ignore_errors=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    copy_web_files(web_dir, dist_dir)

    thumbs_dir = dist_dir / "thumbs"
    full_dir = dist_dir / "full"

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
    cache_hits = 0
    cache_total = 0

    album_overrides = load_album_overrides(content_dir / "albums.csv")
    matched_csv_folders: set[str] = set()
    photo_tags = load_photo_tags(content_dir / "tags.csv")
    matched_tag_rows: set[tuple[str, str]] = set()
    tag_counts: dict[str, int] = {}

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

            if photo_path.suffix.lower() in RAW_EXTENSIONS:
                if max(img.size) < FULL_MAX_EDGE:
                    print(
                        f"warning: {photo_path}'s embedded JPEG is only {img.width}x{img.height} "
                        f"(full view is {FULL_MAX_EDGE}px) -- export a full-size JPEG if it looks soft",
                        file=sys.stderr,
                    )
                if 0x8825 in open_source(photo_path).getexif():
                    print(
                        f"warning: {photo_path} contains GPS location. The website never shows it, "
                        "but the .raf file itself is readable in the repo -- remove GPS before committing",
                        file=sys.stderr,
                    )

            tags = [BW_TAG] if is_monochrome(img) else []
            tag_key = (_normalize(album_name), _normalize(photo_path.stem))
            tag_row = photo_tags.get(tag_key)
            if tag_row is not None:
                matched_tag_rows.add(tag_key)
                tags = [t for t in tags if t not in tag_row["remove"]]
                tags += [t for t in tag_row["add"] if t not in tags and t not in tag_row["remove"]]
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

            capture_date = get_capture_date(photo_path)
            if capture_date:
                capture_dates.append(capture_date)

            photo_slug = unique_slug(slugify(photo_path.stem), taken_photo_slugs)
            src_hash = source_hash(photo_path)

            thumb_rel = f"thumbs/{album_slug}/{photo_slug}.webp"
            full_rel = f"full/{album_slug}/{photo_slug}.webp"

            thumb_w, thumb_h, thumb_avif_ok, thumb_hit = save_resized(
                img, dist_dir / thumb_rel, THUMB_MAX_EDGE, THUMB_QUALITY, THUMB_AVIF_QUALITY, cache_dir, src_hash, "thumb"
            )
            full_w, full_h, full_avif_ok, full_hit = save_resized(
                img, dist_dir / full_rel, FULL_MAX_EDGE, FULL_QUALITY, FULL_AVIF_QUALITY, cache_dir, src_hash, "full"
            )
            if thumb_hit:
                cache_hits += 1
            if full_hit:
                cache_hits += 1
            cache_total += 2

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
                    "tags": tags,
                    "meta": get_photo_meta(photo_path),
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

    for key in sorted(set(photo_tags) - matched_tag_rows):
        print(
            f"note: tags.csv lists '{photo_tags[key]['label']}' but no such photo was found in content/",
            file=sys.stderr,
        )

    tag_index = [
        {"name": name, "slug": tag_slug(name), "count": count}
        for name, count in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    if tag_index:
        print("tags: " + ", ".join(f"{t['name']} ({t['count']})" for t in tag_index))

    ordered = [a for a in albums if a["order"] is not None]
    unordered = [a for a in albums if a["order"] is None]
    ordered.sort(key=lambda a: a["order"])
    unordered.sort(key=lambda a: a["date"], reverse=True)
    albums = ordered + unordered

    manifest = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "albums": albums,
        "tags": tag_index,
    }
    (dist_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total_photos = sum(a["photoCount"] for a in albums)
    cache_note = f", {cache_hits}/{cache_total} outputs reused from cache" if cache_total else ""
    print(f"done: {len(albums)} album(s), {total_photos} photo(s) -> {dist_dir}{cache_note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content-dir", default="content", type=Path)
    parser.add_argument("--dist-dir", default="dist", type=Path)
    parser.add_argument("--web-dir", default="web", type=Path)
    parser.add_argument(
        "--cache-dir",
        default=".build_cache",
        type=Path,
        help="Persist resize/encode outputs here, keyed by source content hash, so unchanged "
        "photos are copied instead of re-encoded on the next run. Pass --no-cache to disable.",
    )
    parser.add_argument("--no-cache", action="store_true", help="Disable the resize/encode cache entirely.")
    args = parser.parse_args()
    build(args.content_dir, args.dist_dir, args.web_dir, None if args.no_cache else args.cache_dir)


if __name__ == "__main__":
    main()
