#!/usr/bin/env python3
"""Register new albums/photos in content/albums.csv and content/tags.csv.

Run this after dropping a new folder (or new photos) into content/:

    python scripts/sync_content.py            # update both CSVs, tag new photos with Claude
    python scripts/sync_content.py --no-ai    # register them but leave tags empty
    python scripts/sync_content.py --check    # report only; exit 1 if anything is unregistered

albums.csv: each new folder is inserted at the top (Sr No 1) and every
existing row moves down. If several folders are new at once, the most recently
shot one goes first. Month-Year is taken from the earliest photo's EXIF date.

tags.csv: each photo with no row gets one. Tags come from Claude, looking
at a downscaled copy of the photo and choosing from TAG_VOCABULARY, the same
fixed list the existing tags use. Without the `anthropic` package or API
credentials, the row is written with empty tags and a note. The build still
auto-detects B&W, and you can fill the tags in by hand.

Existing rows are never edited, apart from shifting Sr No. Only missing ones
are added, so your manual corrections stay as they are.
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_assets import (  # noqa: E402 - sibling script, shares the CSV rules with the build
    HEIC_EXTENSIONS,
    IMAGE_EXTENSIONS,
    _normalize,
    get_capture_date,
    load_album_overrides,
    load_image,
    load_photo_tags,
    match_override,
    natural_sort_key,
)

MODEL = "claude-opus-5-5"
TAG_IMAGE_MAX_EDGE = 1024

# The tag list Claude may choose from. Add a word here to make it available
# for new photos. Existing rows in tags.csv aren't affected.
TAG_VOCABULARY = [
    "people", "portrait", "sky", "clouds", "sun", "sunset", "moon", "night",
    "sea", "lake", "water", "trees", "mountains", "landscape", "architecture",
    "city", "street", "cars", "lights", "silhouette", "reflections", "window",
    "animals", "cat", "food", "flowers", "art", "still life", "motion", "b&w",
]

TAGGING_PROMPT = f"""You are tagging a photo for a personal photography portfolio so visitors can \
browse by subject or look. Choose every tag from this list that clearly applies, and no others:

{", ".join(TAG_VOCABULARY)}

Guidance:
- "people": any person is a notable part of the frame. Add "portrait" only when one or two people are the clear subject, close up.
- "sunset": golden-hour / sunset / sunrise light or sky. "sun": the sun itself is visible.
- "sea" for ocean/beach, "lake" for lakes and calm inland water, "water" only for other water (fountains, splashes).
- "silhouette": subjects rendered as dark shapes against a bright background.
- "b&w": the photo is monochrome, including warm/cool toned monochrome.
- "still life": arranged or found objects as the subject (desk items, books, products).
- "animals" for any animal; also add "cat" for cats.
- "motion": deliberate motion blur, panning or light trails.
Prefer 2-5 tags. Return an empty list only if nothing fits."""

TAGS_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {"type": "string", "enum": TAG_VOCABULARY},
        }
    },
    "required": ["tags"],
    "additionalProperties": False,
}


def album_dirs(content_dir: Path) -> list[Path]:
    return sorted(
        (p for p in content_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: natural_sort_key(p.name),
    )


def photo_files(album_dir: Path) -> list[Path]:
    return sorted(
        (p for p in album_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda p: natural_sort_key(p.name),
    )


def find_new_albums(content_dir: Path, overrides: dict) -> list[Path]:
    return [d for d in album_dirs(content_dir) if photo_files(d) and match_override(d.name, overrides) is None]


def find_untagged_photos(content_dir: Path, tag_rows: dict) -> list[Path]:
    missing = []
    for d in album_dirs(content_dir):
        for p in photo_files(d):
            if (_normalize(d.name), _normalize(p.stem)) not in tag_rows:
                missing.append(p)
    return missing


def album_first_date(album_dir: Path) -> datetime:
    dates = [d for p in photo_files(album_dir) if (d := get_capture_date(p))]
    return min(dates) if dates else datetime.fromtimestamp(album_dir.stat().st_mtime)


# --- albums.csv ---------------------------------------------------------------

def prepend_albums(csv_path: Path, new_albums: list[Path]) -> None:
    """Insert new folders at Sr No 1..N (newest shoot first) and push existing rows down."""
    new_albums = sorted(new_albums, key=album_first_date, reverse=True)
    shift = len(new_albums)

    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    else:
        rows = []
    header = rows[0] if rows else ["Sr No", "Folder Name", "Month-Year", "Description"]
    body = rows[1:]

    # Find columns by the same forgiving header rules the build uses.
    def col(*aliases: str) -> int | None:
        for i, h in enumerate(header):
            if _normalize(h) in aliases:
                return i
        return None

    order_i = col("srno", "sno", "order", "slno", "serialno")
    folder_i = col("foldername", "folder", "album", "albumname", "name")
    date_i = col("monthyear", "month", "date", "monthyr")
    if order_i is None or folder_i is None:
        sys.exit(f"error: {csv_path} needs 'Sr No' and 'Folder Name' columns -- not touching it")

    for row in body:
        if len(row) > order_i and row[order_i].strip().isdigit():
            row[order_i] = str(int(row[order_i]) + shift)

    new_rows = []
    for i, album in enumerate(new_albums, start=1):
        row = [""] * len(header)
        row[order_i] = str(i)
        row[folder_i] = album.name
        if date_i is not None:
            row[date_i] = album_first_date(album).strftime("%b %Y")
        new_rows.append(row)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([header, *new_rows, *body])
    for row in new_rows:
        label = f", {row[date_i]}" if date_i is not None else ""
        print(f"albums.csv: added '{row[folder_i]}' as #{row[order_i]}{label}")


# --- tags.csv -----------------------------------------------------------------

def make_tagger():
    """Return a function photo_path -> list[str], or None if Claude isn't available."""
    try:
        import anthropic
    except ImportError:
        print("note: `anthropic` isn't installed (pip install anthropic) -- new photos get empty tags", file=sys.stderr)
        return None

    try:
        client = anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001 - missing credentials surface here in some SDK versions
        print(f"note: no Anthropic credentials ({exc}) -- new photos get empty tags", file=sys.stderr)
        return None

    def tag(photo_path: Path) -> list[str] | None:
        img = load_image(photo_path).convert("RGB")
        img.thumbnail((TAG_IMAGE_MAX_EDGE, TAG_IMAGE_MAX_EDGE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        data = base64.standard_b64encode(buf.getvalue()).decode("ascii")

        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=2000,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": TAGS_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                        {"type": "text", "text": TAGGING_PROMPT},
                    ],
                }],
            )
        except anthropic.AuthenticationError:
            raise
        except anthropic.APIStatusError as exc:
            print(f"warning: tagging {photo_path} failed ({exc.status_code}): {exc.message}", file=sys.stderr)
            return None
        except anthropic.APIConnectionError as exc:
            print(f"warning: tagging {photo_path} failed (connection): {exc}", file=sys.stderr)
            return None

        if response.stop_reason == "refusal":
            print(f"warning: tagging {photo_path} was declined -- leaving tags empty", file=sys.stderr)
            return None
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            tags = json.loads(text)["tags"]
        except (json.JSONDecodeError, KeyError, TypeError):
            print(f"warning: unexpected tagging output for {photo_path}: {text[:200]!r}", file=sys.stderr)
            return None
        return [t for t in TAG_VOCABULARY if t in tags]  # vocabulary order, deduped

    return tag


def append_tag_rows(csv_path: Path, photos: list[Path], use_ai: bool) -> None:
    tagger = make_tagger() if use_ai else None
    write_header = not csv_path.exists()

    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["Folder Name", "File", "Tags"])
        for i, photo in enumerate(photos, start=1):
            tags: list[str] | None = None
            # HEIC files are converted to .jpg by the next build; register them
            # under the name the .jpg will have so the row keeps matching.
            name = photo.with_suffix(".jpg").name if photo.suffix.lower() in HEIC_EXTENSIONS else photo.name
            if tagger is not None:
                try:
                    tags = tagger(photo)
                except Exception as exc:  # noqa: BLE001 - auth errors etc.: stop calling, keep registering
                    print(f"note: stopping AI tagging ({exc}) -- remaining photos get empty tags", file=sys.stderr)
                    tagger = None
            writer.writerow([photo.parent.name, name, ";".join(tags or [])])
            f.flush()  # keep progress if interrupted mid-way
            shown = ", ".join(tags) if tags else "(no tags -- fill in by hand)"
            print(f"tags.csv [{i}/{len(photos)}]: {photo.parent.name}/{name}: {shown}")


# --- main ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--content-dir", default="content", type=Path)
    parser.add_argument("--check", action="store_true", help="Only report unregistered albums/photos; exit 1 if any.")
    parser.add_argument("--no-ai", action="store_true", help="Don't call Claude; add new photos with empty tags.")
    args = parser.parse_args()

    content = args.content_dir
    albums_csv, tags_csv = content / "albums.csv", content / "tags.csv"
    new_albums = find_new_albums(content, load_album_overrides(albums_csv))
    untagged = find_untagged_photos(content, load_photo_tags(tags_csv))

    if not new_albums and not untagged:
        print("content/ is in sync: every album is in albums.csv and every photo is in tags.csv")
        return

    if args.check:
        for album in new_albums:
            print(f"  not in albums.csv: {album.name}/")
        for photo in untagged:
            print(f"  not in tags.csv:   {photo.parent.name}/{photo.name}")
        print(
            f"\n{len(new_albums)} album(s) / {len(untagged)} photo(s) unregistered. "
            "Run:  python scripts/sync_content.py",
        )
        sys.exit(1)

    if new_albums:
        prepend_albums(albums_csv, new_albums)
    if untagged:
        append_tag_rows(tags_csv, untagged, use_ai=not args.no_ai)
    print("\ndone -- review the new rows in content/albums.csv and content/tags.csv before committing")


if __name__ == "__main__":
    main()
