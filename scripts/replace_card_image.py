#!/usr/bin/env python3
"""
Point a card at a better picture than the one its export supplied.

For the case the upscaler cannot help with: the export's image is not
merely small but *wrong* -- a crop showing half the card, the wrong
printing, or nothing usable at all -- and you have found a better one.

It writes ``manifest.image_override``, the same column the upscaler uses,
which sits ahead of both of the export's image columns. That matters: the
export keeps supplying the original URL, so anything that merely edited
``cdn_image`` would be silently undone by the next batch upload.

**By default it downloads the picture and serves it from this deployment**,
rather than pointing eBay at wherever you found it. A URL you found on a
wiki or a forum can move, rot, or refuse a request that is not a browser,
and the moment that matters is a push or a Refresh weeks later -- at which
point eBay refuses the whole listing, not just the picture. Hosting it
ourselves costs a few kilobytes on the data volume and removes that. Pass
``--link`` to store the URL verbatim instead, which needs no
``PUBLIC_BASE_URL`` and is reasonable for a CDN you trust.

Everything is checked before anything is written:

* the image must be reachable and measurable -- eBay has to fetch it too;
* it must clear eBay's 500-pixel minimum, because an undersized picture
  blocks every future revision of the listing, not just this card;
* and it is reported, but not refused, when the proportions are not those
  of a whole card. That is a judgement about the subject rather than a
  rule, and you may have good reason.

Reports by default and writes nothing. Pass ``--yes`` to apply.

    python scripts/replace_card_image.py ID2503 https://example.com/card.jpg
    python scripts/replace_card_image.py ID2503 https://example.com/card.jpg --yes
    python scripts/replace_card_image.py "Ceruledge" https://example.com/card.jpg
    python scripts/replace_card_image.py --from-file replacements.csv --yes
    python scripts/replace_card_image.py ID2503 --clear --yes

``--from-file`` takes ``manifest_id,url`` per line, so a handful of
corrections is one run. Blank lines and lines starting ``#`` are ignored,
and a header row naming the columns is skipped if present.

On the NAS, which is where the databases and the data volume are:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware python scripts/replace_card_image.py \\
        ID2503 https://example.com/card.jpg --yes
"""

import argparse
import csv
import os
import re
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

# `app.deps` is imported inside main() rather than here, deliberately.
#
# Importing it builds the databases and reads GOOGLE_CLIENT_ID at module
# level, so a test that merely wants to exercise the pure helpers below
# would configure the whole application as a side effect -- and, because
# Python caches modules, every later test in the same run would inherit
# that configuration instead of its own. That is not hypothetical: it
# pointed the web-app suite at the wrong databases and failed eight of its
# tests on a user that should not have existed.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ebay_client import pictures  # noqa: E402
from _imaging import TARGET_LONGEST_SIDE, upscale  # noqa: E402

MANIFEST_ID = re.compile(r"^ID\d+$", re.IGNORECASE)

# What the public card-image route will serve. Anything else is refused
# here rather than stored and then 404ing when eBay asks for it.
SERVABLE = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".webp": ".webp"}

FETCH_TIMEOUT_SECONDS = 30
USER_AGENT = (
    "TCG-Inventory-Middleware/1.0 "
    "(+https://github.com/mrhappyasthma/TCG-Inventory-Middleware)"
)

# A trading card is 2.5 x 3.5. Kept in step with scripts/check_images.py,
# and used the same way: to report, never to refuse.
CARD_ASPECT = 2.5 / 3.5
CARD_ASPECT_TOLERANCE = 0.12


def find_card(db, term):
    """
    The one card this names, or None after saying why not.

    A manifest id is taken as exact. Anything else is a search, and an
    ambiguous one is refused rather than guessed at -- pointing the wrong
    card at a picture is silent, and the next push publishes it.
    """
    term = str(term or "").strip()
    if MANIFEST_ID.match(term):
        card = db.get_manifest_by_id(term.upper())
        if card is None:
            print(f"  {term}: no such card in the catalogue.")
        return card

    matches = db.get_inventory(
        search=term, sort_by="manifest_id", sort_dir="ASC",
        limit=50, offset=0, set_name=None, below_target=False,
        target_default=db.get_target_quantity_default(),
    )
    if not matches:
        print(f"  {term!r}: no card matches.")
        return None
    if len(matches) > 1:
        print(f"  {term!r} matches {len(matches)} cards, so it is ambiguous. "
              f"Name one by its id:")
        for card in matches[:10]:
            print(f"     {card['manifest_id']:<8} {card.get('product_name')} "
                  f"({card.get('set_name')} · {card.get('condition')})")
        if len(matches) > 10:
            print(f"     ... and {len(matches) - 10} more")
        return None
    return matches[0]


def current_image(card):
    for key in ("image_override", "cdn_image", "stock_image"):
        value = str(card.get(key) or "").strip()
        if value:
            return value, key
    return "", "none"


def inspect(url, can_enlarge=True):
    """
    ``(status, verdict_lines)`` for a candidate picture.

    ``status`` is ``"ok"``, ``"small"`` or ``"unusable"``. The split
    matters because "too small" is only fatal when we cannot do anything
    about it: a picture we are downloading and hosting ourselves can be
    enlarged on the way in, and the replacements worth having are often
    small -- the two whole-card images found for the cropped Chinese
    cards were both 300x419, better pictures at a worse size. Refusing
    those would mean keeping a half-card because the whole card was
    under-sized, which is the wrong trade.

    With ``--link`` there is nothing to be done, so small stays fatal.
    """
    lines = []
    verdict = pictures.check(url)
    width, height = verdict.get("width"), verdict.get("height")

    if verdict["ok"] is None:
        lines.append(f"could not be read: {verdict.get('reason') or 'unknown'}")
        lines.append("eBay has to fetch it too, so this is not usable.")
        return "unusable", lines

    lines.append(f"{width}x{height}, longest side {max(width, height)}")
    if verdict["ok"] is False:
        if can_enlarge:
            lines.append(
                f"under eBay's {pictures.MIN_LONGEST_SIDE}px minimum, so it "
                f"will be enlarged to {TARGET_LONGEST_SIDE} on the longest "
                f"side as it is saved. That adds pixels, not detail."
            )
        else:
            lines.append(
                f"TOO SMALL -- eBay needs {pictures.MIN_LONGEST_SIDE} on the "
                f"longest side, and an undersized picture blocks every "
                f"future revision of the whole listing, not just this card. "
                f"Drop --link and it will be enlarged as it is saved."
            )
            return "unusable", lines

    if width and height:
        aspect = width / height
        if abs(aspect - CARD_ASPECT) / CARD_ASPECT > CARD_ASPECT_TOLERANCE:
            lines.append(
                f"NOT CARD-SHAPED: {aspect:.2f} wide per unit tall, where a "
                f"card is {CARD_ASPECT:.2f}. Usually a crop. Allowed, but "
                f"worth a look before it goes live."
            )
    else:
        aspect = None
    if not url.lower().startswith("https://"):
        lines.append(
            "not https -- eBay may refuse to fetch it. Prefer an https URL, "
            "or let this host it for you by dropping --link."
        )
    return ("ok" if verdict["ok"] else "small"), lines


def store_locally(url, manifest_id, directory, enlarge=False):
    """
    Download the picture and return ``(filename, bytes, note)``.

    Saved as it arrives unless it has to grow: the bytes already cleared
    the size check in that case, and a needless JPEG round trip would
    only lose quality. When it does have to grow it is re-encoded as
    JPEG, which is also how a WebP source ends up servable.

    Any previous file for this card under a different extension is
    removed, so a .png replacing a .jpg does not leave an orphan being
    served to nobody.
    """
    extension = SERVABLE.get(
        os.path.splitext(url.split("?")[0])[1].lower(), ".jpg"
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as r:
        raw = r.read()

    note = ""
    if enlarge:
        grown, size = upscale(raw)
        if grown is not None:
            raw, extension = grown, ".jpg"
            note = f"enlarged to {size[0]}x{size[1]}"

    os.makedirs(directory, exist_ok=True)
    for stale in set(SERVABLE.values()):
        if stale != extension:
            path = os.path.join(directory, f"{manifest_id}{stale}")
            if os.path.isfile(path):
                os.remove(path)

    name = f"{manifest_id}{extension}"
    with open(os.path.join(directory, name), "wb") as handle:
        handle.write(raw)
    return name, len(raw), note


def read_pairs(path):
    """``manifest_id,url`` per line, tolerating a header and comments."""
    pairs = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if not row:
                continue
            cells = [c.strip() for c in row if c.strip()]
            if len(cells) < 2 or cells[0].startswith("#"):
                continue
            # A header row names its columns rather than a card.
            if not MANIFEST_ID.match(cells[0]) and "://" not in cells[1]:
                continue
            pairs.append((cells[0], cells[1]))
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Point a card at a replacement picture.",
    )
    parser.add_argument("card", nargs="?", help="A manifest id, or a search term")
    parser.add_argument("url", nargs="?", help="The replacement picture's URL")
    parser.add_argument(
        "--from-file",
        help="A CSV of manifest_id,url pairs instead of one card",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help=(
            "Remove the override so the card falls back to its export "
            "image. For a card whose export image is the problem, that "
            "puts the problem back."
        ),
    )
    parser.add_argument(
        "--link", action="store_true",
        help=(
            "Store the URL as given instead of downloading it. Needs no "
            "PUBLIC_BASE_URL, but eBay must be able to fetch that URL at "
            "push time, possibly weeks from now."
        ),
    )
    parser.add_argument(
        "--base-url", default="",
        help="Public origin to serve from, overriding PUBLIC_BASE_URL.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually apply. Without this the script only reports.",
    )
    args = parser.parse_args(argv)

    from app import deps  # noqa: PLC0415 - see the note at the imports

    db = deps.owner_inventory()
    directory = deps.CARD_IMAGE_DIR

    if args.clear:
        if not args.card:
            print("Name the card to clear, by manifest id or search term.")
            return 2
        card = find_card(db, args.card)
        if card is None:
            return 2
        shown, source = current_image(card)
        print(f"{card['manifest_id']} {card.get('product_name')}")
        print(f"  override now : {card.get('image_override') or '(none)'}")
        print(f"  would fall back to: {card.get('cdn_image') or card.get('stock_image') or '(no picture at all)'}")
        if not args.yes:
            print("\nNothing was written. Pass --yes to clear it.")
            return 0
        db.set_manifest_image_override(card["manifest_id"], "")
        print("\nCleared. The card now uses whatever its export supplied.")
        return 0

    if args.from_file:
        if not os.path.isfile(args.from_file):
            print(f"No such file: {args.from_file}")
            return 2
        pairs = read_pairs(args.from_file)
        if not pairs:
            print(f"{args.from_file} has no manifest_id,url pairs in it.")
            return 2
    elif args.card and args.url:
        pairs = [(args.card, args.url)]
    else:
        parser.print_help()
        return 2

    base_url = (args.base_url or deps.PUBLIC_BASE_URL).strip().rstrip("/")
    if not args.link and not base_url:
        print(
            "PUBLIC_BASE_URL is not set, so there is nowhere to serve a\n"
            "downloaded picture from. Either set it in .env to this\n"
            "deployment's public HTTPS origin, or pass --link to store the\n"
            "URL you gave as-is."
        )
        return 2

    planned = []
    for term, url in pairs:
        print(f"\n{term} -> {url}")
        card = find_card(db, term)
        if card is None:
            continue
        shown, source = current_image(card)
        print(f"  {card['manifest_id']} {card.get('product_name')} "
              f"({card.get('set_name')} · {card.get('condition')})")
        print(f"  currently: {shown or '(no picture)'}  [{source}]")
        status, lines = inspect(url, can_enlarge=not args.link)
        for line in lines:
            print(f"  new: {line}")
        if status == "unusable":
            print("  REFUSED -- not applied.")
        else:
            planned.append((card, url, status == "small"))

    if not planned:
        print("\nNothing usable to apply.")
        return 1

    print(f"\n{len(planned)} card(s) would be repointed"
          + ("" if args.link else f", served from {base_url}/card-images/"))
    if not args.yes:
        print("Nothing was written. Pass --yes to apply.")
        return 0

    applied = failed = 0
    for card, url, needs_enlarging in planned:
        manifest_id = card["manifest_id"]
        if args.link:
            public = url
        else:
            try:
                name, size, note = store_locally(
                    url, manifest_id, directory, enlarge=needs_enlarging
                )
            except (urllib.error.URLError, OSError) as exc:
                failed += 1
                print(f"  {manifest_id}: could not save it -- {exc}")
                continue
            public = f"{base_url}/card-images/{name}"
            print(f"  {manifest_id}: saved {size / 1024:.0f} KB"
                  + (f", {note}" if note else ""))
        # Written only after the file exists, so an override can never
        # point at something that was never created.
        db.set_manifest_image_override(manifest_id, public)
        applied += 1
        print(f"  {manifest_id}: now uses {public}")

    print(f"\n{applied} applied, {failed} failed.")
    if applied:
        print(
            "Rebuild the draft and push, or press Refresh on a listing that\n"
            "already exists, for eBay to pick these up. An upload from\n"
            "SortSwift will not undo it -- the override sits ahead of the\n"
            "export's own image columns."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
