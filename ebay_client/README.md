# ebay-client

A standalone client for the eBay Sell APIs, used by the TCG Inventory
Middleware but deliberately independent of it.

## Why it is its own package

`tcg_engine` holds the inventory and pricing domain logic. `app/` is the web
backend. This is the third component, and it exists separately because it is
the only one that talks to a remote system with credentials, quotas, retries
and signed callbacks. Keeping that behind one seam is what makes it testable
without network access, and replaceable when eBay deprecates an API — which
this project has already lived through once, since the File Exchange CSV flow
it was originally built on is now a deprecated path.

The boundary is a **vocabulary** boundary. This library speaks eBay's
language: SKUs, offers, inventory item groups, order line items. It knows
nothing about manifest IDs, pricing rules or SortSwift. Translation happens in
the layer above, so neither model leaks into the other.

## No dependencies, by design

Everything except signature verification is standard library, built on
`urllib` for the same reason `tcg_engine.pricing_feed` is: the package installs
and its tests run on a bare interpreter. Verifying an eBay notification needs
ECDSA, which the standard library does not offer, so `cryptography` is an
*optional* extra and its import is deferred into `ebay_client.notifications`:

```
pip install -e .[notifications]
```

## Layout

| Module | Responsibility |
|---|---|
| `config.py` | Which environment, which credentials, which scopes |
| `errors.py` | Typed failures, so a caller can tell "eBay said no" from "eBay is busy" |
| `transport.py` | The only place HTTP happens: retries, backoff, error translation |
| `oauth.py` | The consent flow, refresh, and application-only tokens |
| `notifications.py` | Endpoint challenge, and signature verification |
| `client.py` | The facade that wires the above together |

Resource wrappers (inventory items, offers, groups, orders) are intentionally
absent until the step that first needs them, so no untested call surface
accumulates ahead of a caller who could have proved it works.

## Testing

```
python -m unittest discover -s tests
```

Nothing reaches the network. Every test injects an `opener` with the same
signature as the real one — that seam exists precisely so a test cannot spend
the application's daily call quota. Signature verification is tested by
round-tripping against a locally generated EC key, which is the only way to
exercise it without a live notification and still catches a wrong curve, a
wrong digest, or the mistake of verifying a re-serialised body.

## Two token types, not interchangeable

* A **user token** acts as the seller; everything touching listings or orders
  needs one. Obtained once through consent, then kept alive by a refresh token
  that lasts about eighteen months — after which the seller must consent again.
* An **application token** acts as the developer account, and is what
  notification subscriptions and verification keys use.

`EbayClient` therefore exposes two transports, `seller` and `application`.
Using the wrong one yields a 403 whose message does not mention tokens.
