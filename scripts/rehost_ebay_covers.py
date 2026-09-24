#!/usr/bin/env python3
"""
Re-host cover photos that live on eBay, so listings can be published.

eBay refuses a listing whose pictures mix its own hosted copies with
self-hosted ones -- *"A mixture of Self Hosted and EPS pictures are not
allowed"*. Every card picture this application sends is self-hosted, so a
cover on ``i.ebayimg.com`` fails the whole listing, and the obvious way
to end up with one is to copy the image address out of eBay, which is
exactly what somebody does when choosing a cover.

The picture itself is usually fine -- it is only *where it is* that eBay
objects to. So rather than making you find each image again, this
downloads each one and serves it from this deployment, then repoints the
record at the local copy. Nothing about which picture is used changes.

It covers all three places a cover can be recorded:

* the **account-wide default** in Listing Rules, which applies to every
  new listing without one of its own;
* a **live listing's** own, in ``ebay_listing_overrides``;
* and one **staged on a draft**, which is where a cover chosen on the
  drafts page sits until the listing exists.

Reports by default and writes nothing. Pass ``--yes`` to apply.

    python scripts/rehost_ebay_covers.py
    python scripts/rehost_ebay_covers.py --yes

On the NAS, which is where the data volume and the public hostname are:

    cd /volume1/docker/tcg-middleware
    docker compose exec tcg-middleware \\
        python scripts/rehost_ebay_covers.py --yes

Needs ``PUBLIC_BASE_URL``, because the point is to give eBay an address it
can fetch that is not its own -- and behind the reverse proxy this
process cannot work out what that address is.
"""

import argparse
import hashlib
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tcg_engine")):
    if path not in sys.path:
        sys.path.insert(0, path)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _imaging import EBAY_MIN_LONGEST_SIDE  # noqa: E402

FETCH_TIMEOUT_SECONDS = 30
USER_AGENT = (
    "TCG-Inventory-Middleware/1.0 "
    "(+https://github.com/mrhappyasthma/TCG-Inventory-Middleware)"
)

SERVABLE = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".webp": ".webp"}

# Where a re-hosted cover is stored, inside the directory the public
# card-image route already serves. Prefixed so it cannot collide with a
# card's own replacement picture, which is named for its manifest id.
COVER_PREFIX = "cover-"


def local_name(url: str) -> str:
    """
    A stable filename for one source URL.

    Named from a digest of the URL rather than from the listing, so that
    re-running is idempotent and two listings sharing a cover share one
    file instead of racing over a name.
    """
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    extension = SERVABLE.get(
        os.path.splitext(url.split("?")[0])[1].lower(), ".jpg"
    )
    return f"{COVER_PREFIX}{digest}{extension}"


class NotTheCover(RuntimeError):
    """What came back is not the picture that was asked for."""


def rehost(url, directory, base_url):
    """
    ``(public_url, note)`` for a re-hosted copy of this picture.

    **An undersized download is refused, never enlarged.** That rule
    exists because of how eBay answers for an image id it does not know:
    not a 404, but **HTTP 200 with an 80x80 "no image" placeholder**. A
    cover that really is on a live eBay listing passed eBay's own
    500-pixel minimum to get there, so anything smaller coming back from
    ``i.ebayimg.com`` is the placeholder rather than the cover -- and
    enlarging it would replace a real cover photo with a grey icon,
    silently, while reporting success.

    Found the hard way: the first version of this did enlarge, and
    cheerfully repointed eight covers at a 520x520 blow-up of that icon.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as r:
        raw = r.read()

    # Measured from the bytes we actually received, not from the URL.
    # ``measure()`` falls back to reading dimensions encoded in an eBay
    # URL when the header cannot be had, and that fallback would report
    # the size the *original* picture had -- which is exactly the lie
    # this check exists to catch.
    from ebay_client.pictures import dimensions_from_header  # noqa: PLC0415

    size = dimensions_from_header(raw)
    if size is None:
        raise NotTheCover(
            f"what came back from that URL is not a readable image "
            f"({len(raw)} bytes)"
        )
    width, height = size
    if max(width, height) < EBAY_MIN_LONGEST_SIDE:
        raise NotTheCover(
            f"what came back is {width}x{height}, under eBay's "
            f"{EBAY_MIN_LONGEST_SIDE}px minimum. A cover that is really on "
            f"a live listing cleared that minimum to get there, so this is "
            f"almost certainly eBay's 'no image' placeholder -- it answers "
            f"200 with an 80x80 icon for an id it does not know. The "
            f"original picture is gone; choose a new cover instead"
        )

    name = local_name(url)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "wb") as handle:
        handle.write(raw)
    return (
        f"{base_url}/card-images/{name}",
        f"{width}x{height}, {len(raw) // 1024} KB",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Re-host eBay-hosted cover photos so listings can publish.",
    )
    parser.add_argument(
        "--base-url", default="",
        help="Public origin to serve from, overriding PUBLIC_BASE_URL.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Actually re-host. Without this the script only reports.",
    )
    args = parser.parse_args(argv)

    from app import deps  # noqa: PLC0415 - see replace_card_image for why
    from ebay_client.pictures import is_ebay_hosted  # noqa: PLC0415

    db = deps.owner_inventory()
    owner = deps._owner_scope()
    directory = deps.CARD_IMAGE_DIR
    base_url = (args.base_url or deps.PUBLIC_BASE_URL).strip().rstrip("/")

    # (label, current url, apply(new_url))
    found = []

    settings = db.get_listing_settings(user_id=owner)
    account = str(settings.get("cover_image_url") or "").strip()
    if account and is_ebay_hosted(account):
        found.append((
            "the account-wide default (Listing Rules)", account,
            lambda new: db.set_listing_settings(
                {"cover_image_url": new}, user_id=owner
            ),
        ))

    for parent, url in db.get_listing_cover_images().items():
        if is_ebay_hosted(url):
            found.append((
                f"live listing #{parent}", url,
                lambda new, p=parent: db.set_listing_cover_image(p, new),
            ))

    for plan in db.get_plans(user_id=owner):
        if plan["status"] not in ("draft", "approved"):
            continue
        for group_key, url in db.get_plan_group_covers(plan["id"]).items():
            if url and is_ebay_hosted(url):
                found.append((
                    f"plan {plan['id']}, listing {group_key}", url,
                    lambda new, pid=plan["id"], g=group_key:
                        db.set_plan_group_cover(pid, g, new),
                ))

    if not found:
        print(
            "No cover photo is hosted by eBay. Nothing here needs "
            "re-hosting."
        )
        return 0

    print(f"{len(found)} cover photo(s) are hosted by eBay and would be "
          f"refused at publish:\n")
    for label, url, _ in found:
        print(f"  {label}")
        print(f"     {url}")

    if not base_url:
        print(
            "\nPUBLIC_BASE_URL is not set, so there is nowhere to re-host\n"
            "them to. Set it in .env to this deployment's public HTTPS\n"
            "origin and run `docker compose up -d`, or pass --base-url."
        )
        return 2
    if not base_url.lower().startswith("https://"):
        print(f"\nPUBLIC_BASE_URL is {base_url!r}, which is not https. eBay "
              f"will not fetch listing pictures over plain HTTP.")
        return 2

    print(f"\nThey would be downloaded and served from "
          f"{base_url}/card-images/ instead. The pictures themselves do not "
          f"change.")
    if not args.yes:
        print("\nNothing was written. Pass --yes to re-host them.")
        return 0

    done = failed = 0
    for label, url, apply in found:
        try:
            public, note = rehost(url, directory, base_url)
        except NotTheCover as exc:
            failed += 1
            print(f"  {label}: LEFT ALONE -- {exc}.")
            continue
        except (urllib.error.URLError, OSError) as exc:
            failed += 1
            print(f"  {label}: could not re-host -- {exc}")
            continue
        # Recorded only after the file exists, so a cover can never point
        # at something that was never written.
        apply(public)
        done += 1
        print(f"  {label}: {public}  ({note})")

    print(f"\n{done} re-hosted, {failed} left alone.")
    if failed:
        # Said instead of the encouraging line, not after it. A run that
        # left most of them alone has not unblocked the push, and "push
        # again" printed under a list of failures is the kind of hollow
        # success this project goes out of its way not to report.
        print(
            f"{failed} cover(s) are still eBay-hosted, so the listings they "
            f"belong to will still be refused. Set a different cover for "
            f"each on the drafts page, or clear it to use the first card's "
            f"picture."
        )
        return 1
    if done:
        print("Push the draft again -- the covers are unchanged, they are "
              "just no longer on eBay's own servers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
