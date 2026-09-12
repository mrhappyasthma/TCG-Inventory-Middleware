# Design: automating Module C (orders → stock deductions)

**Status: designed, not built.** This file records where the project stands,
what eBay actually offers for order events, and the design that follows.
Written before any code, because the obvious plan — real-time
notifications — turns out not to exist in the API this project uses, and
because the question of who owns a quantity after a sale mattered more than
the transport did.

---

## 1. Where the project stands

Four things now exist, and the boundary between them is the architecture.

**`tcg_engine/`** — pure Python, no eBay dependency. The catalogue, the
planner, the pricing rules, the repricer's decisions. Testable with no
network, no credentials and no quota.

**`ebay_client/`** — a standalone library that speaks SKUs, offers and
inventory item groups and knows nothing about cards. Its own tests (116) and
its own injected HTTP transport.

**`app/`** — FastAPI. Owns the adapter binding the two, the Google
Sign-In-only auth, the background jobs and the dashboard.

**`scripts/`** — one-off operational tools, deliberately outside the web app.

### The pipeline as it is today

| Step | Module | Direction | State |
|---|---|---|---|
| 1 | **A** — SortSwift export ingest | CSV **in** | Automated on upload. Catalogues cards, **adds** their quantities, **stages a draft** |
| — | Drafts → push | **API out** | The only route to eBay. Reviewed, approved, pushed through the Inventory API |
| — | Automatic repricer | **API out** | Nightly, price only, with a boundary margin and a 14-day hold on falls |
| 2 | **B** — store mirror sync | **API in** (Feed API) or CSV in | Automated when eBay is connected; the manual upload is the fallback |
| 3 | **C** — orders → stock deductions | CSV **in** | **Entirely manual, and now the only gap.** Download the orders report and upload it. Nothing else deducts a sale from the catalogue |

Everything eBay-bound is now API-only: all eleven listings were migrated with
`bulkMigrateListing`, and the File Exchange output path was deleted. Module C
is the last place a CSV is handled by hand on the eBay side.

### Constraints that bind this work

* **No eBay user personal data is ever persisted.** No buyer name, address,
  email, phone or eBay username, in either database. This is not a
  preference — it underpins the account-deletion exemption, and
  `/api/ebay/notifications` logs the **topic only** for the same reason.
* **Google Sign-In is the only auth mechanism.**
* **A SortSwift upload is a delta of newly scanned cards.** Its quantities
  **add**; a card absent from it means nothing. Our catalogue owns the
  number. This is what makes the work below necessary rather than merely
  convenient.
* **Nothing writes to eBay without an approved plan** — except the repricer,
  which is a deliberate, argued exception limited to price.

---

## 2. The finding: there is no order notification topic

The plan "real-time via notifications, plus a 15-minute poll" cannot be built
as stated, because the first half does not exist in the API this project uses.

**The REST Notification API has no order topic.** Its topics cover listing and
item events — `ITEM_AVAILABILITY`, `ITEM_PRICE_REVISION`,
`MARKETPLACE_ACCOUNT_DELETION`, listing-preview task status, buyer quote
requests. Nothing fires when an order is created or paid for. `getTopics`
against our own connected account would confirm it from the source, and is
worth one call before relying on this.

**Order events exist only in legacy Platform Notifications**, part of the
XML/SOAP Trading API: `FixedPriceTransaction`, `AuctionCheckoutComplete`,
`ItemSold`, `ItemMarkedPaid`, configured with `SetNotificationPreferences`.
Three reasons not to build on it:

1. It is a different protocol end to end. eBay POSTs SOAP XML to a URL, with
   none of the signature scheme our existing notification endpoint implements.
   It needs a second parser and a second verification path — the same reason
   this project chose the Feed API over `GetSellerList` for reading listings.
2. It is the API eBay is retiring. `docs/ebay-api-design.md` already declined
   to write through the Trading API for that reason.
3. **eBay's own documentation says to poll anyway.** Its guidance for
   `AuctionCheckoutComplete` is to *also* configure periodic polling of
   `GetOrders`. A notification you cannot trust as the only signal buys
   latency, not correctness.

**So: poll, and do not build notifications.** Fifteen minutes is a sensible
interval and nothing about it is load-bearing — the design is correct at any
interval, which is the property to aim for.

---

## 3. Settled: our catalogue owns the quantity

This was the open question, and it is now answered. Mark is removing the
re-upload of deductions into SortSwift — his inventory is too large to keep in
step that way — and SortSwift uploads are per-batch deltas rather than a full
dump of the shelf.

So the arithmetic changed in `c85b96b`, before any of this was built:

* an upload's quantities **add** to what is held; there is no replace mode;
* a card **absent** from an upload means nothing, so the sold-out-if-absent
  reconciliation is gone;
* **our catalogue is the source of truth for physical stock.**

Which resolves the design completely, and also makes it urgent:

| System | How it learns a sale | Automated? |
|---|---|---|
| **eBay** | Decrements itself at the moment of sale | Yes, by eBay |
| **Our store mirror** | Module B sync, or a confirmed push | Yes |
| **Our catalogue** | **Nothing, until this is built** | **No — this is the gap** |
| SortSwift | No longer kept in step, by choice | n/a |

**There is now nothing that deducts a sale from the catalogue.** The old route
was the sold-out sweep on a full dump, and that is gone. Left as is, the
catalogue drifts upward from reality: sell two of five, and it still says
five, while eBay says three. The draft then proposes pushing five — selling
stock that is not there.

So this is no longer an efficiency project. It closes a hole that the switch
to deltas opened, and it is the only remaining way a sale reaches our own
records.

It also means there is **no deduction file to produce**. Nothing consumes it:
SortSwift is out of the loop, and our catalogue is written directly. The
orders poller adjusts `manifest.quantity` and records what it did.

## 4. The design

### 4a. Poll

`GET /sell/fulfillment/v1/order`, filtered on modification date:

```
filter=lastmodifieddate:[2026-09-11T08:00:00.000Z..]
limit=50&offset=0
```

* **Watermark, with overlap.** Store the last successful poll time and query
  from *watermark minus a few minutes*. Clock skew and late-arriving
  modifications are real, the line-item table makes re-seeing an order free,
  and the alternative is a silently missed sale.
* **Paginate to exhaustion.** Never treat the first page as the answer; that
  is the same failure mode as reading a bulk call's HTTP status instead of its
  body.
* **Ninety days is the filter's limit.** A first run must bound its start date
  accordingly rather than asking for everything.
* **Reads only.** No fulfillment is created, nothing is marked shipped.

### 4b. Project away the personal data at the boundary

`getOrders` returns a great deal we must not keep: `buyer.username`,
`buyer.buyerRegistrationAddress`, and
`fulfillmentStartInstructions[].shippingStep.shipTo` with a name, a full
contact address, an email and a phone number.

**The adapter in `app/` must project each order down to the only fields that
cross into `tcg_engine` or the database:**

```
order_id, line_item_id, sku, quantity, last_modified_date, line_item_status
```

Nothing else. Not "stored but unused" — not carried across the boundary at
all, so that no future change can start persisting it by accident. The payload
must not be logged either, on the same rule the notification endpoint already
follows.

This is what lets a deletion request be answered with "we hold nothing about
your buyers", which is the whole basis of the exemption.

### 4c. Be idempotent on `(order_id, line_item_id)`

A new table, keyed on the pair, because that is the unit that sells:

```sql
CREATE TABLE ebay_order_line (
    order_id      TEXT NOT NULL,
    line_item_id  TEXT NOT NULL,
    sku           TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    manifest_id   TEXT,              -- NULL when the SKU matched nothing
    status        TEXT NOT NULL,     -- eBay's line item status, last seen
    seen_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deducted_at   TIMESTAMP,         -- set when it reaches a deduction file
    PRIMARY KEY (order_id, line_item_id)
);
```

* An order re-seen because of the overlap window updates `status` and nothing
  else. `deducted_at` is what makes a deduction happen exactly once.
* **A SKU that matches no card is recorded with `manifest_id` NULL and
  reported.** It must not be silently dropped: it means either a card
  catalogued under a different id, or a listing made outside this
  application, and both are things to look at.
* SKU → card reuses the existing decoder. Module C already strips the bin
  suffix (`ID1001-Bin_A-12` → `ID1001`) to find the card and its SortSwift
  `skuId`; that logic is in `tcg_engine/orders.py` and does not change.

### 4d. Cancellations and refunds are *not* automatic restocks

A cancelled or refunded line item means eBay's money moved back. It does not
mean the card is on the shelf — it may already have shipped. So a status
change away from a sale is **surfaced, never acted on**: it appears in the
console and the log, and a human decides. Mirrors the repricer's hold: the
asymmetric direction is the one that gets a human.

### 4e. There is no file at the end of it

`build_deduction_csv` and `deduction_row` exist to produce a file SortSwift
imports, and SortSwift is out of the loop (§3). The poller writes the
catalogue directly and records what it did. The CSV upload path stays as the
fallback for a poller that cannot reach eBay, exactly as Module B's does.

---

## 5. Build order

Both research steps this originally opened with are answered: there is no
order topic to subscribe to, and SortSwift is out of the loop. So it is all
code.

1. `ebay_client/orders.py` — `get_orders(transport, since, limit, offset)`,
   paginating to exhaustion, with its own tests against an injected opener.
   Knows nothing about cards.
2. The adapter in `app/`, projecting away the personal data (§4b). **Test
   that the projection is exhaustive**: feed it a full realistic payload and
   assert the output's keys are exactly the six allowed.
3. `ebay_order_line` and its accessors (§4c).
4. `tcg_engine/order_sync.py` — reconcile projected lines against what has
   been seen, map SKUs to cards, deduct the catalogue once each.
5. The poll loop and the endpoints, following the repricer's shape: a manual
   "Poll now", a background interval, terminal logging, and a preview that
   contacts eBay but writes nothing.
6. The dashboard: Module C's card shows what was deducted and when the last
   poll succeeded. The upload stays as the fallback.

## 6. Risks

* **A missed sale is invisible.** Nothing in the system notices an order that
  was never polled. The overlap window and the line-item table make
  double-counting impossible, so the bias should be heavily toward re-seeing
  orders. A "last successful poll" timestamp on screen is worth more than it
  sounds: a poller that silently stopped looks exactly like a shop with no
  sales.
* **Multi-quantity line items.** A buyer taking three of one card is one line
  item with `quantity: 3`. The deduction must use that quantity, not count
  line items.
* **Personal data creeping in later.** The projection is the only thing
  standing between `getOrders` and a compliance problem. It needs a test that
  fails when a field is added, not a comment asking for care.
* **The 90-day filter window.** A poller offline for longer than that has a
  gap it cannot close from the API. Worth a warning when the watermark is
  older than, say, 60 days.
