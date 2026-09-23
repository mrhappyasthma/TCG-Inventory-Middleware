#!/usr/bin/env python3
"""
Enlarge the card pictures eBay would refuse, and serve them ourselves.

eBay requires **500 pixels on an image's longest side**, and re-validates
every picture on a listing whenever anything about that listing changes. So
one undersized photo does not merely look poor: it blocks price changes,
quantity changes and every other revision to that listing, and the refusal
(`21919137`) names eBay's own copy of the image, which cannot be traced back
to ours.

When the export's picture is too small and the upstream CDN has no larger
copy, enlarging it is the only remedy left. This downloads each offending
image, scales it up with Lanczos resampling, writes it beside the databases,
and points the card at the local copy through ``manifest.image_override``.

Three things about that are worth knowing before running it.

**It adds pixels, not detail.** A 300x419 scan becomes 358x500 -- about a 19%
enlargement, so it stays close to the original -- but nothing is recovered
that was not there. It satisfies eBay's rule honestly, because the rule is
about dimensions, and a soft picture beats a listing that cannot be revised.

**eBay has to be able to reach the URL**, so ``PUBLIC_BASE_URL`` must be the
public HTTPS origin of this deployment, exactly as the outside world sees it.
It is configuration rather than anything derived from a request for the same
reason ``EBAY_NOTIFICATION_ENDPOINT`` is: behind the DSM reverse proxy the URL
this process sees is not the URL eBay would call.

**The files are not inside either database**, so the backup in the Database
dialog does not cover them. That matters more than it sounds. eBay copies a
picture into its own store when it first fetches it, so a listing already
live survives the files being deleted -- but the *next* Refresh or push
re-sends every ``imageUrls`` entry, and an override pointing at a file that
is gone is a dead URL that gets the whole listing refused. ``--purge``
therefore clears the overrides as well as the files, which returns those
cards to their original undersized pictures rather than to a broken link.

Reports by default and writes nothing.

    python scripts/upscale_card_images.py
    python scripts/upscale_card_images.py --yes
    python scripts/upscale_card_images.py --set "Terastal Gathering" --yes
    python scripts/upscale_card_images.py --purge --yes

On the NAS, which is where the data volume and the public hostname are:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware \\
        python scripts/upscale_card_images.py --yes

Deliberately a script: it reaches out to a third-party CDN, it writes files
to the data volume, and it is a repair rather than routine work.
"""

import argparse
import io
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app import deps  # noqa: E402

# eBay's documented floor. A little headroom is added so that a picture
# landing exactly on the boundary cannot be rejected by a rounding
# disagreement between their measurement and ours.
EBAY_MIN_LONGEST_SIDE = 500
TARGET_LONGEST_SIDE = 520

# Generous, because these are photographs on a third-party CDN and the
# alternative to waiting is a card left unlistable.
FETCH_TIMEOUT_SECONDS = 30

USER_AGENT = (
    "TCG-Inventory-Middleware/1.0 "
    "(+https://github.com/mrhappyasthma/TCG-Inventory-Middleware)"
)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as r:
        return r.read()


def upscale(raw: bytes, target: int = TARGET_LONGEST_SIDE):
    """
    (bytes, (width, height)) for the enlarged JPEG.

    Lanczos because it is the least bad of the cheap resamplers on
    photographic material; anything better means a model and a GPU, which
    is a great deal of machinery for a 19% enlargement.

    Converted to RGB because a JPEG cannot carry an alpha channel, and a
    palette or RGBA source would otherwise fail at save time rather than
    here.
    """
    from PIL import Image  # noqa: PLC0415 - imported late so --purge needs no Pillow

    image = Image.open(io.BytesIO(raw))
    image.load()
    width, height = image.size
    scale = target / max(width, height)
    if scale <= 1.0:
        return None, (width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    enlarged = image.convert("RGB").resize(size, Image.LANCZOS)
    buffer = io.BytesIO()
    enlarged.save(buffer, "JPEG", quality=92, optimize=True, progressive=True)
    return buffer.getvalue(), size


def current_image(card) -> str:
    """The picture this card would be listed with today."""
    for key in ("image_override", "cdn_image", "stock_image"):
        value = str(card.get(key) or "").strip()
        if value:
            return value
    return ""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Enlarge card pictures eBay would refuse for being too small.",
    )
    parser.add_argument(
        "--set", dest="set_name", default="",
        help="Only cards in this expansion set.",
    )
    parser.add_argument(
        "--purge", action="store_true",
        help=(
            "Delete the replacement pictures and clear every override, "
            "returning the cards to their export images."
        ),
    )
    parser.add_argument(
        "--base-url", default="",
        help=(
            "Public origin eBay should fetch from, overriding "
            "PUBLIC_BASE_URL. e.g. https://cards.example.synology.me"
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually do it. Without this the script only reports.",
    )
    args = parser.parse_args(argv)

    db = deps.owner_inventory()
    directory = deps.CARD_IMAGE_DIR

    if args.purge:
        return purge(db, directory, apply=args.yes)

    base_url = (args.base_url or deps.PUBLIC_BASE_URL).strip().rstrip("/")
    if not base_url:
        print(
            "PUBLIC_BASE_URL is not set, so there is no address to give\n"
            "eBay for these pictures. Set it in .env to this deployment's\n"
            "public HTTPS origin -- the same hostname you reach the\n"
            "dashboard on, e.g. https://cards.yourname.synology.me -- and\n"
            "run `docker compose up -d` to pick it up. Or pass --base-url."
        )
        return 2
    if not base_url.lower().startswith("https://"):
        # Not pedantry: eBay fetches these itself and will not follow a
        # plain-HTTP URL for listing imagery.
        print(
            f"PUBLIC_BASE_URL is {base_url!r}, which is not https. eBay "
            f"will not fetch listing pictures over plain HTTP."
        )
        return 2

    from ebay_client import pictures  # noqa: PLC0415

    cards = db.get_inventory(
        search=None, sort_by="manifest_id", sort_dir="ASC",
        limit=1000000, offset=0,
        set_name=args.set_name or None, below_target=False,
        target_default=db.get_target_quantity_default(),
    )
    print(f"Measuring {len(cards)} card(s)...\n")

    too_small, unreadable, fine, no_image = [], [], 0, []
    for card in cards:
        url = current_image(card)
        if not url:
            no_image.append(card)
            continue
        verdict = deps.check_picture(url)
        if verdict["ok"] is True:
            fine += 1
        elif verdict["ok"] is False:
            too_small.append((card, url, verdict))
        else:
            # Never counted as a pass. Our fetch failing is not proof eBay's
            # will, but it is not evidence it will succeed either.
            unreadable.append((card, url, verdict))

    print(f"  {fine} already meet eBay's {EBAY_MIN_LONGEST_SIDE}px minimum")
    print(f"  {len(too_small)} are too small")
    print(f"  {len(unreadable)} could not be measured (reported, not assumed good)")
    print(f"  {len(no_image)} have no picture at all")

    for card, url, verdict in unreadable:
        print(f"    ? {card['manifest_id']} {card.get('product_name')}: {url}")
    for card in no_image:
        print(f"    - {card['manifest_id']} {card.get('product_name')}: no image")

    if not too_small:
        print("\nNothing to enlarge.")
        return 0

    print(f"\n{len(too_small)} picture(s) would be enlarged and served from "
          f"{base_url}/card-images/ :")
    for card, url, verdict in too_small:
        print(f"  {card['manifest_id']:<8} {str(card.get('product_name'))[:32]:<32} "
              f"{verdict.get('width')}x{verdict.get('height')}")

    if not args.yes:
        print("\nNothing was written. Pass --yes to do it.")
        return 0

    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        print(f"Could not create {directory}: {exc}")
        return 2

    done = failed = 0
    for card, url, _ in too_small:
        manifest_id = card["manifest_id"]
        try:
            raw = fetch(url)
        except (urllib.error.URLError, OSError) as exc:
            failed += 1
            print(f"  {manifest_id}: could not download {url} -- {exc}")
            continue
        try:
            data, size = upscale(raw)
        except Exception as exc:  # noqa: BLE001 - one card must not stop the rest
            failed += 1
            print(f"  {manifest_id}: could not enlarge -- {exc}")
            continue
        if data is None:
            # Our header check said too small and Pillow disagrees. Reported
            # rather than resolved: two measurements of the same picture
            # differing is worth a person looking, not a silent choice.
            failed += 1
            print(f"  {manifest_id}: already {size[0]}x{size[1]} when decoded, "
                  f"which disagrees with the header check. Left alone.")
            continue

        name = f"{manifest_id}.jpg"
        try:
            with open(os.path.join(directory, name), "wb") as handle:
                handle.write(data)
        except OSError as exc:
            failed += 1
            print(f"  {manifest_id}: could not write the file -- {exc}")
            continue

        # Written only after the file exists, so an override can never point
        # at something that was never created.
        public = f"{base_url}/card-images/{name}"
        db.set_manifest_image_override(manifest_id, public)
        done += 1
        print(f"  {manifest_id}: {size[0]}x{size[1]}  {public}")

    print(f"\n{done} enlarged, {failed} could not be.")
    if done:
        print(
            "Rebuild the draft and push, or press Refresh on an existing\n"
            "listing, for eBay to pick these up. Confirm afterwards with\n"
            "  python scripts/check_images.py --all"
        )
    return 0


def purge(db, directory, apply: bool) -> int:
    """Remove the replacement pictures and the overrides pointing at them."""
    overrides = db.get_image_overrides()
    files = []
    if os.path.isdir(directory):
        files = sorted(
            name for name in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, name))
        )

    print(f"{len(overrides)} card(s) point at a replacement picture, and "
          f"{len(files)} file(s) are in {directory}.")
    for card in overrides:
        print(f"  {card['manifest_id']:<8} {str(card.get('product_name'))[:32]:<32} "
              f"{card['image_override']}")

    if not overrides and not files:
        print("Nothing to purge.")
        return 0

    print(
        "\nPurging returns these cards to the pictures their export "
        "supplied,\nwhich for every card this was used on means a picture "
        "eBay refused\nfor being too small. A listing already live keeps "
        "working, because\neBay copied the image when it first fetched it "
        "-- but the next\nRefresh or push re-sends the export's small URL "
        "and eBay will refuse\nthe whole listing. Only purge once you are "
        "certain those listings\nare finished with."
    )

    if not apply:
        print("\nNothing was removed. Pass --yes to purge.")
        return 0

    cleared = db.clear_image_overrides()
    removed = 0
    for name in files:
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError as exc:
            print(f"  could not remove {name}: {exc}")
    print(f"\n{cleared} override(s) cleared, {removed} file(s) removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
