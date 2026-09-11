"""
Refresh market prices from TCGCSV, and regenerate an eBay Revise file from
the stored prices.

TCGCSV is a free daily mirror of TCGplayer's own catalogue and price data. We
use it because TCGplayer's official API has been closed to new applicants for
years, and because the numbers are the same: the Market/Low/Mid/High columns a
SortSwift export carries were verified, field for field, against TCGCSV's
product-level prices for the same cards.

Their documented limits are treated as hard constraints, not suggestions:

* Updated once per day. ``last-updated.txt`` is checked first and a refresh is
  skipped entirely if nothing has changed, so a button press costs one request
  on a day that is already current.
* At most 10,000 requests per 24 hours, or an application may be banned. We
  issue one request per set held -- single digits in practice.
* 100 ms between requests.
* A custom User-Agent is required; generic or missing ones may be blocked.
* Restrictive CORS, so this must run server-side. It does.

Prices are product-level. Neither TCGplayer's public data nor the SortSwift
export breaks a price down by condition, which is why the grade discount lives
in ``condition_multipliers`` as policy rather than being fetched.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .db import (
    Database,
    SHARED_SCOPE,
    apply_condition_multiplier,
    apply_pricing_rules,
)

# The catalogue and price endpoints live under /tcgplayer; the daily
# snapshot timestamp lives at the site root. Both are needed, so both are
# named rather than one being derived from the other with "..".
TCGCSV_ROOT = "https://tcgcsv.com"
TCGCSV_BASE = f"{TCGCSV_ROOT}/tcgplayer"
POKEMON_CATEGORY_ID = 3

# Identifies this application, as TCGCSV requires. A generic urllib header
# may be blocked outright.
USER_AGENT = (
    "TCG-Inventory-Middleware/1.0 "
    "(+https://github.com/mrhappyasthma/TCG-Inventory-Middleware)"
)

REQUEST_SPACING_SECONDS = 0.1
REQUEST_TIMEOUT_SECONDS = 30

# A reprice touches price only. Quantity is deliberately absent rather than
# blank: sending the column at all risks changing stock on a file whose whole
# purpose is not to.
REPRICE_HEADERS = ["Action", "ItemID", "CustomLabel", "Price"]


class PriceFeedError(RuntimeError):
    """A refresh could not be completed. The stored prices are untouched."""


def _http_get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise PriceFeedError(
                "TCGCSV is rate limiting us (HTTP 429). Their documented "
                "limit is one sync per 24 hours; wait and try again."
            ) from exc
        raise PriceFeedError(f"TCGCSV returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise PriceFeedError(f"Could not reach TCGCSV: {exc.reason}") from exc


def default_fetcher(path: str) -> str:
    """
    Fetch one TCGCSV path, spacing requests as their docs ask.

    A path beginning "/" is taken from the site root rather than from the
    ``/tcgplayer`` prefix. That distinction exists because ``last-updated.txt``
    lives at the root while everything else is under the prefix -- and the
    previous attempt to express it, a relative "../last-updated.txt", was sent
    literally: urllib does not normalise "..", so TCGCSV received
    "/tcgplayer/../last-updated.txt" and answered 404. The gate that is
    supposed to make a same-day refresh cost one request was therefore failing
    on every run.
    """
    time.sleep(REQUEST_SPACING_SECONDS)
    if path.startswith("/"):
        return _http_get(f"{TCGCSV_ROOT}{path}")
    return _http_get(f"{TCGCSV_BASE}/{path}")


def _json_results(payload: str, what: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PriceFeedError(f"TCGCSV returned unreadable JSON for {what}") from exc
    if isinstance(data, dict) and data.get("errors"):
        raise PriceFeedError(f"TCGCSV reported an error for {what}: {data['errors']}")
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise PriceFeedError(f"TCGCSV returned no results for {what}")
    return results


def fetch_last_updated(fetcher: Callable[[str], str] = default_fetcher) -> str:
    """
    The timestamp of the current daily snapshot.

    Checked before anything else so a refresh on an already-current day costs
    exactly one request instead of one per set.
    """
    return fetcher("/last-updated.txt").strip()


def refresh_group_index(
    db: Database,
    category_id: int = POKEMON_CATEGORY_ID,
    fetcher: Callable[[str], str] = default_fetcher,
) -> int:
    """
    Cache the set list, so a set code can be resolved to a TCGCSV group id.

    Their ``abbreviation`` is our ``set_code`` -- "SWSH06" on both sides --
    which is what makes this join possible without a hand-maintained map.
    """
    results = _json_results(fetcher(f"{category_id}/groups"), "groups")
    rows = [
        {
            "category_id": category_id,
            "group_id": int(g["groupId"]),
            "name": str(g.get("name") or ""),
            "abbreviation": str(g.get("abbreviation") or ""),
        }
        for g in results
        if g.get("groupId") is not None
    ]
    db.replace_tcgcsv_groups(category_id, rows)
    return len(rows)


def fetch_group_prices(
    group_id: int,
    category_id: int = POKEMON_CATEGORY_ID,
    fetcher: Callable[[str], str] = default_fetcher,
) -> Dict[Tuple[str, str], float]:
    """
    Market prices for one set, keyed by (productId, subTypeName).

    The printing must be part of the key. TCGCSV returns a row per printing,
    and on real cards Normal and Reverse Holofoil differ by 2-6x -- joining on
    productId alone would price every reverse holo as a normal, a silent
    underprice that looks entirely plausible.
    """
    results = _json_results(
        fetcher(f"{category_id}/{group_id}/prices"), f"prices for group {group_id}"
    )
    prices: Dict[Tuple[str, str], float] = {}
    for row in results:
        product_id = row.get("productId")
        market = row.get("marketPrice")
        if product_id is None or market is None:
            # No market price is "unknown", not zero. Skipping leaves the
            # stored price alone rather than collapsing it to the floor.
            continue
        sub_type = str(row.get("subTypeName") or "").strip()
        prices[(str(product_id), sub_type)] = float(market)
    return prices


def refresh_market_prices(
    db: Database,
    fetcher: Callable[[str], str] = default_fetcher,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Update stored market prices for every catalogued card we can resolve.

    Only cards carrying a tcgplayer_id and a set_code can be matched. Anything
    unmatched is reported and left untouched -- never zeroed, because the
    pricing rules multiply against this number and a silent zero would reprice
    the catalogue to the floor.
    """
    logs: List[Dict[str, str]] = []

    snapshot = fetch_last_updated(fetcher)
    previous = db.get_listing_setting("tcgcsv_last_updated", "")
    if snapshot and previous == snapshot and not force:
        logs.append({
            "level": "INFO",
            "message": (
                f"TCGCSV has not published a new snapshot since {snapshot}, so "
                f"nothing was fetched. It updates once a day."
            ),
        })
        return {
            "skipped": True, "snapshot": snapshot, "updated": 0,
            "unmatched": 0, "groups_fetched": 0, "logs": logs,
        }

    cards = db.get_cards_for_repricing()
    if not cards:
        logs.append({"level": "WARN",
                     "message": "No catalogued card has a TCGplayer ID to price."})
        return {"skipped": False, "snapshot": snapshot, "updated": 0,
                "unmatched": 0, "groups_fetched": 0, "logs": logs}

    set_codes = sorted({c["set_code"] for c in cards if c.get("set_code")})
    groups = db.get_tcgcsv_groups_by_abbreviation(POKEMON_CATEGORY_ID)
    if not groups or any(code.upper() not in groups for code in set_codes):
        count = refresh_group_index(db, POKEMON_CATEGORY_ID, fetcher)
        logs.append({"level": "INFO",
                     "message": f"Cached {count} TCGCSV set(s) for lookup."})
        groups = db.get_tcgcsv_groups_by_abbreviation(POKEMON_CATEGORY_ID)

    needed: Dict[int, str] = {}
    unresolved_sets = []
    for code in set_codes:
        group_id = groups.get(code.upper())
        if group_id is None:
            unresolved_sets.append(code)
        else:
            needed[group_id] = code

    for code in unresolved_sets:
        logs.append({
            "level": "WARN",
            "message": (
                f"Set code {code!r} does not match any TCGCSV set, so its "
                f"cards keep their existing prices. Check the code against "
                f"the abbreviations TCGCSV publishes."
            ),
        })

    prices: Dict[Tuple[str, str], float] = {}
    for group_id in sorted(needed):
        prices.update(fetch_group_prices(group_id, POKEMON_CATEGORY_ID, fetcher))

    updates: List[Tuple[str, float]] = []
    unmatched: List[str] = []
    for card in cards:
        key = (str(card["tcgplayer_id"]).strip(),
               str(card.get("printing") or "").strip())
        market = prices.get(key)
        if market is None:
            unmatched.append(card["manifest_id"])
            continue
        updates.append((card["manifest_id"], market))

    if updates:
        db.record_market_prices(updates, source=f"tcgcsv:{snapshot or 'unknown'}")
    if snapshot:
        db.set_listing_settings({"tcgcsv_last_updated": snapshot},
                                user_id=SHARED_SCOPE)

    logs.append({
        "level": "SUCCESS",
        "message": (
            f"Updated the market price of {len(updates)} card(s) from "
            f"{len(needed)} set(s), snapshot {snapshot or 'unknown'}."
        ),
    })
    if unmatched:
        logs.append({
            "level": "WARN",
            "message": (
                f"{len(unmatched)} card(s) had no price in TCGCSV for their "
                f"(TCGplayer ID, printing) pair and keep their existing "
                f"price: {', '.join(unmatched[:8])}"
                + (" ..." if len(unmatched) > 8 else "")
            ),
        })

    return {
        "skipped": False,
        "snapshot": snapshot,
        "updated": len(updates),
        "unmatched": len(unmatched),
        "groups_fetched": len(needed),
        "logs": logs,
    }


def build_reprice_csv(
    db: Database, user_id: int = SHARED_SCOPE
) -> Dict[str, Any]:
    """
    An eBay Revise file that changes price only, built from stored prices.

    This is what makes a price refresh actionable for the File Exchange
    listings. Module A prices from the columns of the export it is given, so
    without this there is no way to push a new price without re-uploading a
    dump.

    Listings created through the Inventory API are excluded: the automatic
    repricer owns those, applies a hold window to falling prices, and can
    reach them directly. A row here for one of them would be a row File
    Exchange cannot apply and a second opinion on a price that already has an
    owner.

    Rows are emitted only where the computed price differs from the price eBay
    is known to hold, on the same principle as Module A: a file full of
    unchanged rows tells the operator nothing and asks eBay to rewrite every
    listing for no reason.
    """
    import csv
    import io

    logs: List[Dict[str, str]] = []
    rules = db.get_pricing_rules(user_id=user_id)
    multipliers = {
        m["condition_key"]: m["multiplier"]
        for m in db.get_condition_multipliers(user_id=user_id)
    }

    rows: List[Dict[str, Any]] = []
    unchanged = 0
    unknown_price = 0
    api_managed = 0
    unpriced_grades = set()

    # Listings the Inventory API manages are the automatic repricer's, and a
    # Revise row for one of them is worse than useless: File Exchange cannot
    # revise a listing created through the Inventory API, so the upload
    # succeeds and changes nothing. This file offered seven such rows before
    # the repricer existed, which is how the overlap came to light.
    managed_parents = {
        str(row["ebay_parent_id"]).strip()
        for row in db.get_managed_listings()
        if str(row.get("ebay_parent_id") or "").strip()
    }

    for card in db.get_live_cards_for_repricing():
        if str(card.get("ebay_parent_id") or "").strip() in managed_parents:
            api_managed += 1
            continue

        base = float(card.get("market_price") or 0.0)
        if base <= 0:
            # Nothing to price from. Refusing beats emitting the floor price.
            unknown_price += 1
            continue

        adjusted, factor = apply_condition_multiplier(
            base, card.get("condition"), multipliers
        )
        if factor is None and card.get("condition"):
            unpriced_grades.add(str(card["condition"]))
        price, _ = apply_pricing_rules(rules, adjusted)

        known = card.get("last_known_price")
        if known is not None and round(float(known), 2) == round(price, 2):
            unchanged += 1
            continue

        label = (card.get("custom_label") or "").strip() or card["manifest_id"]
        rows.append({
            "Action": "Revise",
            "ItemID": str(card["ebay_parent_id"]).strip(),
            "CustomLabel": label,
            "Price": f"{price:.2f}",
        })

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=REPRICE_HEADERS,
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)

    if unchanged:
        logs.append({
            "level": "INFO",
            "message": (
                f"{unchanged} listing(s) already match the price your rules "
                f"compute, so no row was written for them."
            ),
        })
    if unknown_price:
        logs.append({
            "level": "WARN",
            "message": (
                f"{unknown_price} listing(s) have no stored market price and "
                f"were skipped rather than priced from nothing. Refresh "
                f"prices first."
            ),
        })
    if api_managed:
        logs.append({
            "level": "INFO",
            "message": (
                f"{api_managed} card(s) are on listings this application "
                f"manages through the eBay API and are repriced "
                f"automatically, so no Revise row was written for them. "
                f"File Exchange cannot revise those listings at all."
            ),
        })
    if unpriced_grades:
        logs.append({
            "level": "WARN",
            "message": (
                "No condition multiplier is configured for: "
                + ", ".join(sorted(unpriced_grades))
                + ". Those were priced at full market value."
            ),
        })
    logs.append({
        "level": "SUCCESS" if rows else "INFO",
        "message": (
            f"Reprice file ready: {len(rows)} listing(s) to update."
            if rows else
            "Nothing to reprice: every listing already matches your rules."
        ),
    })

    return {
        "csv_content": buffer.getvalue(),
        "reprice_count": len(rows),
        "unchanged_count": unchanged,
        "missing_price_count": unknown_price,
        "api_managed_count": api_managed,
        "logs": logs,
    }
