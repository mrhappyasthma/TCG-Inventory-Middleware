# Design: automating Module C (orders → stock deductions)

**Status: not started.** This file records where the project stands, what eBay
actually offers for order events, and the design that follows from it. Written
before any code, because the obvious plan — real-time notifications — turns
out not to be available in the API this project uses.

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
| 1 | **A** — SortSwift export ingest | CSV **in** | Automated on upload. Catalogues cards, corrects quantities, detects sold-out cards, **stages a draft** |
| — | Drafts → push | **API out** | The only route to eBay. Reviewed, approved, pushed through the Inventory API |
| — | Automatic repricer | **API out** | Nightly, price only, with a boundary margin and a 14-day hold on falls |
| 2 | **B** — store mirror sync | **API in** (Feed API) or CSV in | Automated when eBay is connected; the manual upload is the fallback |
| 3 | **C** — orders → deductions | CSV **in**, CSV **out** | **Entirely manual.** Download the orders report, upload it, download the deduction file, import it into SortSwift |

Everything eBay-bound is now API-only: all eleven listings were migrated with
`bulkMigrateListing`, and the File Exchange output path was deleted. Module C
is the last place a CSV is handled by hand on the eBay side.

### Constraints that bind this work

* **No eBay user personal data is ever persisted.** No buyer name, address,
  email, phone or eBay username, in either database. This is not a
  preference — it underpins the account-deletion exemption, and
  `/api/ebay/notifications` logs the **topic only** for the same reason.
* **Google Sign-In is the only auth mechanism.**
* **SortSwift's export is a full inventory dump.** Quantities replace stored
  values; cards absent from it are sold out. This matters enormously below.
* **Nothing writes to eBay without an approved plan** — except the repricer,
  which is a deliberate, argued exception limited to price.

---

## 2. The finding: there is no order notification topic

The plan "real-time via notifications, plus a 15-minute poll" cannot be built
as stated, because the first half does not exist in the API this project uses.

**The REST Notification API has no order topic.** Its topics cover listing and
item events — `ITEM_AVAILABILITY`, `ITEM_PRICE_REVISION`,
`MARKETPLACE_ACCOUNT_DELETION`, listing-preview task status, buyer quote
requests. Nothing fires when an order is created or paid for. The authoritative
check is `getTopics` against our own connected account, which is the first task
below.

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

## 3. The harder problem: who owns the quantity after a sale

This deserves settling before any code, because it decides how much the
automation is worth.

When a card sells, three systems have to end up agreeing:

| System | How it learns | Automated? |
|---|---|---|
| **eBay** | Decrements its own quantity at the moment of sale | Yes, by eBay |
| **Our store mirror** (`ebay_variations.last_known_qty`) | Module B sync, or a confirmed push | Yes |
| **SortSwift** | The deduction file, imported by hand | **No** |
| **Our catalogue** (`manifest.quantity`) | The next SortSwift export | Indirectly |

Module C's entire purpose is the third row. Our catalogue is *not* the source
of truth for physical stock: SortSwift is, and its export is a full dump that
**replaces** our quantities. So deducting from our own catalogue and stopping
there is worse than useless — it would be silently overwritten by the next
export, and in the meantime the draft would propose a quantity change that
eBay has already made itself.

**The consequence: automating the eBay half does not make this hands-off.**
Polling `getOrders` removes the "download the orders report from Seller Hub"
step. Importing the result into SortSwift stays manual unless SortSwift can
accept it programmatically.

**Open question, and the one worth answering first:** does SortSwift offer an
API or a watched-folder import? If it does, this becomes genuinely end-to-end.
If it does not, the realistic win is:

* no more downloading a report from Seller Hub on a schedule;
* deductions accumulate automatically and are always ready;
* a standing, dated list of what sold since the last import, so nothing is
  missed and nothing is counted twice — which is the part a manual process
  gets wrong.

That is worth having. It is just not "no more CSVs", and it should not be
built under that expectation.

---

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

### 4e. Reuse the output, not just the input

`build_deduction_csv` and `deduction_row` already produce exactly the right
file, negative quantities and all. The poller feeds them rows instead of a
parsed CSV. Nothing about the SortSwift format needs revisiting.

---

## 5. Build order

1. **Call `getTopics`** against the connected account and write down the
   actual topic list. One call; settles §2 from the source rather than from
   documentation, and it is cheap to be wrong about here and expensive later.
2. **Find out whether SortSwift can import programmatically.** This decides
   whether step 6 is the end of the story or the middle of it (§3).
3. `ebay_client/orders.py` — `get_orders(transport, since, limit, offset)`,
   paginating to exhaustion, with its own tests against an injected opener.
   Knows nothing about cards.
4. The adapter in `app/`, projecting away the personal data (§4b). **Test that
   the projection is exhaustive**: feed it a full realistic payload and assert
   the output dict's keys are exactly the six allowed.
5. `tcg_engine/order_sync.py` — reconcile projected lines against
   `ebay_order_line`, map SKUs to cards, decide what is newly deductible.
6. The poll loop and the endpoint, following the repricer's shape: a manual
   "Poll now", a background interval, terminal logging, and a preview that
   contacts eBay but writes nothing.
7. The dashboard: Module C's card becomes "pending deductions", with the
   download; the upload stays as the fallback exactly as Module B's does.

Steps 1 and 2 are research and should happen before any of the rest.

---

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
