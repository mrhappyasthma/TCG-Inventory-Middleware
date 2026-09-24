"""
Promoted Listings: putting an ad on a listing that already exists.

A different API from everything else here. Listings are the Inventory API;
an *ad* belongs to a **campaign** in the Marketing API, and the two know
nothing about each other -- so promoting a listing is a separate call made
after it is published, not a field on the offer.

Three facts shape this module.

**An ad cannot exist without a campaign.** eBay has no "just promote this
at 2%" call; a campaign is the container that holds the funding model and
the ads. So a campaign id is configuration, and with none set nothing here
is called at all.

**The bid is a percentage of the sale price**, charged only when the item
sells through the ad -- `COST_PER_SALE`. It is sent as a string with two
decimals, which is how eBay's own examples spell it and how the value
comes back.

**Promoting is never worth failing a listing over.** A listing that went
live but was not promoted is a working listing; one that was rolled back
because an ad could not be created is not. Every caller here is expected
to treat a failure as a warning, which is why nothing in this module
retries or raises anything the transport does not.

Vocabulary, as everywhere in this package: listing ids and percentages.
Nothing here knows what a card is.
"""

from typing import Any, Dict, List, Optional, Sequence

from .errors import ApiError

MARKETING_BASE = "/sell/marketing/v1"

# eBay's bounds for a Promoted Listings Standard bid. Checked here so a bad
# setting is refused while we still know which value it was, rather than
# arriving as a 400 on a listing that has just gone live.
MIN_BID_PERCENTAGE = 2.0
MAX_BID_PERCENTAGE = 100.0

# The funding model these ads use: the seller pays only when the item sells
# through the ad. The other model, COST_PER_CLICK, is a different product
# with a different campaign shape.
COST_PER_SALE = "COST_PER_SALE"


def format_bid(rate) -> str:
    """
    A bid percentage as eBay wants it: a string, two decimals.

    Raises ValueError for anything outside eBay's own bounds, naming the
    value. A rate is configuration, and configuration that would be
    refused is better refused here than three calls later.
    """
    try:
        number = float(rate)
    except (TypeError, ValueError):
        raise ValueError(f"{rate!r} is not a number, so it cannot be a bid")
    if not MIN_BID_PERCENTAGE <= number <= MAX_BID_PERCENTAGE:
        raise ValueError(
            f"a bid of {number}% is outside eBay's range of "
            f"{MIN_BID_PERCENTAGE}% to {MAX_BID_PERCENTAGE}%"
        )
    return f"{number:.2f}"


def get_campaigns(
    transport, marketplace_id: str = "", limit: int = 100
) -> List[Dict[str, Any]]:
    """
    The seller's ad campaigns, so one can be chosen rather than guessed.

    A campaign id appears nowhere in Seller Hub's own URLs in a form worth
    copying, which is the same problem business policy ids have -- so this
    exists for the same reason ``account.get_policies`` does.
    """
    query = f"?limit={int(limit)}"
    if marketplace_id:
        query += f"&marketplace_id={marketplace_id}"
    response = transport.get(f"{MARKETING_BASE}/ad_campaign{query}") or {}
    campaigns = response.get("campaigns")
    return list(campaigns) if isinstance(campaigns, list) else []


def running_campaigns(campaigns: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The campaigns an ad can actually be added to.

    An ended or paused campaign accepts the call and promotes nothing,
    which is the kind of success worth filtering out before it is offered
    as a choice.
    """
    return [
        c for c in campaigns
        if str(c.get("campaignStatus") or "").upper() in ("RUNNING", "SCHEDULED")
    ]


def create_ad(
    transport,
    campaign_id: str,
    listing_id: str,
    bid_percentage: str,
) -> Optional[str]:
    """
    Promote one listing, returning the ad id eBay assigned.

    ``listingId`` rather than a SKU: Promoted Listings addresses the
    published listing, which is why this can only run after a publish has
    returned an id.

    An ad that already exists for the listing is reported by eBay as an
    error rather than being replaced, and that is left to the caller --
    "already promoted" is not a failure worth undoing anything for.
    """
    payload = {
        "listingId": str(listing_id),
        "bidPercentage": str(bid_percentage),
    }
    response = transport.post(
        f"{MARKETING_BASE}/ad_campaign/{campaign_id}/ad", payload
    ) or {}
    ad_id = response.get("adId")
    if ad_id:
        return str(ad_id)
    # eBay answers 201 with the new ad's location and sometimes no body.
    # An id we did not get back is not a failure -- the ad exists -- so
    # this returns None rather than raising, and the caller logs it.
    return None


def describe_campaign(campaign: Dict[str, Any]) -> str:
    """One campaign in a line, for a chooser or a log."""
    return (
        f"{campaign.get('campaignName') or '(unnamed)'} "
        f"[{campaign.get('campaignId')}] "
        f"{campaign.get('campaignStatus') or '?'}"
    )
