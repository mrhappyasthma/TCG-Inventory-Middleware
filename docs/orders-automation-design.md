# Design: automating Module C (orders → stock deductions)

**Status: built.** Shipped in `3d4cd70`. This file records why it is a poll
rather than a webhook, why our catalogue owns the quantity, and the
properties the implementation had to have -- all of which were decided
before any code, because the obvious plan did not survive contact with the
API and the harder question turned out not to be the transport.

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

## 5. What was built

| Piece | Where |
|---|---|
| `get_orders`, paginating to exhaustion | `ebay_client/orders.py` |
| The six-field projection | `app/ebay_orders.py` |
| `ebay_order_line` and its accessors | `tcg_engine/db.py` |
| Reconcile, deduct once, adopt on a first run | `tcg_engine/order_sync.py` |
| The poll loop, endpoints and watermark | `app/main.py` |
| Module C's card, showing the last successful poll | `app/static/` |

Configured by `ORDER_POLL_ENABLED` and `ORDER_POLL_INTERVAL_MINUTES`
(default 15). The watermark lives in `listing_settings` under
`orders_last_polled_at`, advances only on success, and is recorded as the
moment the poll *started* -- so an order modified during the call falls in
the next window rather than in neither.

One thing the design did not anticipate and the implementation added: the
**first poll adopts**. With no watermark, eBay hands over ninety days of
history, every sale in it already accounted for. Deducting that would take
three months of stock off the shelf a second time, so the first run records
every line as handled with a deducted quantity of zero and removes nothing.

## 6. Risks

* **A missed sale is invisible**, and that is still true -- nothing notices
  an order it never read. Mitigated rather than solved: a thirty-minute
  overlap on every poll, `ebay_order_line` making a second deduction
  impossible so the bias can safely favour re-reading, a refusal to return a
  truncated page, and the last successful poll on screen in amber once it is
  overdue. That last one earns its place: a poller that silently stopped
  looks exactly like a shop with no sales.
* **Multi-quantity line items.** A buyer taking three of one card is one line
  item with `quantity: 3`. The deduction must use that quantity, not count
  line items.
* **Personal data creeping in later.** Handled the way it had to be: the
  projection is a keep-list of six named fields, `order_sync` refuses any
  line whose keys are not exactly those six, and the test feeds a full
  payload carrying a buyer's username, name, email, phone and address and
  asserts not one value survives.
* **The 90-day filter window.** A poller offline for longer than that has a
  gap it cannot close from the API. Worth a warning when the watermark is
  older than, say, 60 days.
