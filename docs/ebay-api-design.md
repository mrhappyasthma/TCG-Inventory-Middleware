# Design: replacing the CSV hand-off with the eBay APIs

**Status: in progress.** The `ebay_client` library, the staging tables, the
plan engine and the drafts page are built; nothing contacts eBay yet. See §9
for what is done and what is not. This file records the design and, more
importantly, the constraints eBay imposes on it, so that the implementation
does not rediscover them one failed push at a time.

---

## 1. The decision: staging is ours, eBay is only ever written live

Every planned change is staged in **our** database and reviewed on a drafts
page in the dashboard. eBay is contacted only at push time, and only for
changes that have been approved. eBay is never used as a staging area.

eBay does offer something that looks like staging — the Inventory API's
`createOffer` produces an *unpublished* offer, and a separate `publishOffer`
makes it live — and the first draft of this design used it. It was rejected for
two reasons.

The first is a hazard specific to variation listings.
`createOrReplaceInventoryItemGroup` **automatically updates a live listing**
when the group's membership changes. There is no publish step to gate it. So
the one operation this project most needs to stage — moving a card into or out
of a variation listing — is precisely the operation that cannot be staged on
eBay. An edit made in a staging spirit reaches buyers immediately.

The second is simpler: two independent records of "what is planned" can
disagree, and eBay's copy is the one we can neither inspect on demand nor
repair. Keeping the plan in SQLite makes the approval gate a single predicate
the push worker filters on, rather than a property we have to trust a remote
system to preserve.

**Invariant: no eBay write may occur for a plan that is not `approved`, and
approval is the only thing that authorises a write.** Reads are unrestricted —
polling orders, reading offers, or pulling market prices from TCGCSV changes
nothing a buyer can see.

---

## 2. One funnel, many producers

Today five different things produce a CSV for manual upload. All five become
producers of the same draft plan:

| Producer | Today | Becomes |
|---|---|---|
| SortSwift batch upload (Module A) | Add + Revise CSVs | a plan of `create_listing` / `update_qty` items |
| Manual quantity edit | edits the DB, then a Revise CSV | a one-item plan |
| TCGCSV price refresh | `build_reprice_csv` | a plan of `update_price` items |
| Cover photo change | `ebay_listing_overrides` + Revise CSV | an `update_images` item |
| Grouping / ungrouping | not currently possible | `add_to_group` / `remove_from_group` items |

The value of the single funnel is that suppression, validation, and the
approval gate are each written once and apply to every producer. Today the
suppression rule lives in the batch path only, which is why a manual edit can
generate a Revise row that changes nothing.

---

## 3. Schema

```sql
CREATE TABLE listing_plan (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',
        -- draft | approved | pushing | pushed | partial | failed | discarded
    source       TEXT NOT NULL,          -- batch | manual | reprice | photo | grouping
    source_ref   TEXT,                   -- batch sha256, etc.
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    approved_at  TIMESTAMP,
    approved_by  INTEGER,
    pushed_at    TIMESTAMP
);

CREATE TABLE listing_plan_item (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id        INTEGER NOT NULL REFERENCES listing_plan(id) ON DELETE CASCADE,
    manifest_id    TEXT    NOT NULL REFERENCES manifest(manifest_id),
    group_key      TEXT,                 -- the (set, condition) listing this belongs to
    action         TEXT    NOT NULL,
        -- create_listing | update | zero_out | remove_from_group | end_listing
        --
        -- Refined during implementation: quantity and price were originally
        -- separate actions, but eBay changes both in one
        -- bulkUpdatePriceQuantity call. Splitting them would double the call
        -- count and leave a listing briefly at a new price with an old
        -- quantity, so one `update` carries both.
    proposed_qty   INTEGER,
    proposed_price REAL,
    observed_qty   INTEGER,              -- last_known_qty at plan time
    observed_price REAL,                 -- last_known_price at plan time
    status         TEXT NOT NULL DEFAULT 'pending',
        -- pending | excluded | pushed | failed | deferred
    validation     TEXT,                 -- JSON: our own pre-push findings
    error_code     TEXT,
    error_message  TEXT
);
```

`ebay_variations` gains `ebay_sku`, `ebay_offer_id` and `inventory_group_key`
beside the existing `ebay_parent_id` and `custom_label`. The two eras coexist:
a listing created by File Exchange keeps working through its `ebay_parent_id`
while new listings carry offer ids, so there is no cutover day.

`observed_qty` / `observed_price` are snapshots, not live reads. They exist so
the drafts page can show *what changed and from what*, and so a plan approved
an hour after a sync can be detected as stale rather than silently applying
against different numbers than the ones reviewed.

---

## 3a. The Inventory API cannot see the existing store

**Found while building the read path, and it invalidates an assumption in §4.**

The Inventory API only manages listings that were created *through* the
Inventory API. That is what `bulkMigrateListing` exists for: it converts an
existing eBay listing into the Inventory Location, Inventory Item and Offer
objects the API works in. A store built with File Exchange has none of those,
so `getOffers` returns nothing for it and `bulkUpdatePriceQuantity` has
nothing to update.

So the store divides in two, and the boundary is how a listing was created:

| | Existing File Exchange listings | Listings we create via the Inventory API |
|---|---|---|
| Read | Feed API `LMS_ACTIVE_INVENTORY_REPORT` | `getOffers`, or the same report |
| Update | needs migration first | `bulkUpdatePriceQuantity` |

**Reading is solved and needs no migration — and now verified against the
real store.** The Feed API's LMS reports live in the same world as File
Exchange and see the listings as they are. That is what `/api/ebay/sync` uses,
and it feeds the same parser the uploaded report does.

Confirmed equivalent to the manual download, card for card: 62 variations
across 4 listings, split 35 / 21 / 4 / 2, with 50 unmanaged singles ignored.
The Feed report yields 112 records where the Seller Hub report yields 116 rows
for the same store, because Seller Hub emits a parent row *in addition* to its
children while the Feed report nests the children inside the parent's block.
Two shapes, same facts, same mirror.

Two properties of that report cost real damage to learn, and are recorded in
`AGENTS.md`: it is XML rather than CSV, and its variations are nested inside
the item block with a blank SKU at the item level.

`GetSellerList` would also read them, but it requires a start-time window of
at most 120 days — so an older store means paging over several windows and
stitching them — and it returns XML needing a second parser.

**Writing is an open decision, and it is the real one.** Three options:

1. **Migrate** the existing listings with `bulkMigrateListing`, then use the
   Inventory API for everything. Cleanest end state, and §4 becomes true as
   written. But migration mutates live listings, so it is exactly the kind of
   change that belongs behind the approval gate rather than in a setup script.
2. **Write through the Trading API** (`ReviseFixedPriceItem`) for the legacy
   listings, and the Inventory API only for new ones. No migration, but two
   push paths to maintain, on an API eBay is retiring.
3. **Feed API `LMS_REVISE_INVENTORY_STATUS`** for quantity and price on legacy
   listings. Same asynchronous shape as the report we already fetch, so it
   reuses this code, and no migration.

Option 3 looks strongest for reprices and quantity changes — the bulk of what
this system does — with the Inventory API used for genuinely new listings.
Not decided; §10 tracks it.

## 4. Push semantics

The push worker walks approved items grouped by `group_key`, because eBay's
unit of publication is the listing, not the card.

**New variation listing** (four call types):

1. `createOrReplaceInventoryItem` per SKU — the card, its aspects, its images
2. `createOffer` per SKU — price, quantity, policies, category
3. `createOrReplaceInventoryItemGroup` — title, `variantSKUs`, `variesBy`
4. `publishOfferByInventoryItemGroup` — goes live

**Existing listing:**

| Change | Call | Note |
|---|---|---|
| quantity and/or price | `bulkUpdatePriceQuantity` | 25 records per call |
| images | `createOrReplaceInventoryItem` | full replacement of that SKU |
| add a card | item + offer, then group replace | group replace applies it live |
| remove a card | group replace — **but see §6** | often not permitted |

`manifest_id` becomes the SKU directly. The compact `ID1001` scheme was
invented to fit inside eBay's 50-character Custom Label limit; as a SKU it is
simply a good stable identifier, and several File Exchange workarounds fall
away with it (§7).

Note that `createOrReplaceInventoryItemGroup` is a **complete replacement**:
every field, including the full member list, is required on every call whether
or not it changed. Always rebuild the whole payload from stored state. A
partial payload silently drops whatever it omits — the same failure shape as
`PicURL` replacing a listing's entire picture set.

---

## 5. Validation must happen before approval, not at push

**`publishOfferByInventoryItemGroup` fails if *any* offer in the group is
invalid.** One card with a missing required aspect therefore blocks the entire
variation listing — potentially hundreds of cards — and the failure arrives
after approval, when the user has already walked away.

So the drafts page must run our own validation before the approve button is
live, per group, and show failures against the specific card responsible:

- title within 80 characters after the template is applied
- every required item specific present, including `Game` from `default_game`
- `ConditionID` and the `CD:40001` descriptor resolvable for the category
- a postal code, and every configured business policy name non-empty
- a price that is not zero or unknown — an unknown market price must never
  reach a push, since the pricing rules multiply against it
- at least one image per SKU that carries images

This is the drafts page's real job. Grouping and price editing are what the
user came for; catching these is what keeps a push from failing halfway.

---

## 6. Removing a card from a live listing is often impossible

**A variation that has one or more sales cannot be removed from a live
listing**, by `deleteOffer` or by dropping it from the group's `variantSKUs`.
eBay's own guidance is to set that variation's available quantity to 0, which
greys it out on the View Item page, and remove it from the group later.

This lands directly on the "move cards out of a variation listing" feature, and
it will bite exactly the cards most likely to be moved — the ones that sold.
The drafts UI must therefore model removal as two outcomes rather than one:

- a variation with no sales is removed outright
- a variation with sales is set to quantity 0 and its `listing_plan_item` goes
  to `deferred`, with the removal retried later

Convenient consequence: the zero-out path already exists. Module B zeroes cards
absent from an Active Listings report, and a full SortSwift dump zeroes cards
it omits, so "quantity 0 means gone from the buyer's view" is already how this
system thinks. The removal becomes an eventual cleanup rather than a user-facing
failure.

---

## 7. What this migration deletes

Much of the accumulated File Exchange knowledge in `AGENTS.md` becomes dead
weight, which is worth stating plainly because it is the strongest argument for
the migration:

- **`<option name>=<url>` image encoding** — gone. Each SKU carries its own
  `imageUrls`, so per-variation images stop being a delimiter trick, and the
  "only one attribute may carry photos" limitation goes with it.
- **50-character Custom Label limit** — gone, and with it the reason the
  compact ID scheme had to be compact.
- **Stripping `=`, `;` and `|` from option names** — gone. JSON arrays have no
  delimiters to collide with.
- **The parent container row requirement** — gone. The group is a first-class
  object, so a variation update no longer needs a hand-built parent row
  declaring the full option list.
- **`CustomLabel` cannot be renamed** — no longer relevant; the SKU is chosen
  once, by us, and never needs to encode anything mutable.
- **"Success does not mean applied"** — a REST call returns a real error.

One thing gets *harder*: the bin location. Today `custom_label` is
`ID1001-Bin_A12`, so eBay's own order screen tells you where the card
physically is. A SKU should be stable, and a bin is not — a card that moves
bins would need its listing ended and recreated. The recommendation is that the
SKU stays `ID1001` and the bin is surfaced in our own orders view instead,
looked up from `manifest.remarks`. **This changes the physical picking workflow
and needs sign-off before it is built.**

---

## 7a. Compliance: no eBay user personal data, ever

eBay disables a keyset outright until the developer either receives
marketplace account-deletion notifications or holds an exemption, and the
exemption is available only to applications that do not persist eBay data.

Today that claim is true by construction: `orders.py` reads an order export in
memory and stores nothing from it, and the store mirror holds only our own
listings' item numbers, labels, quantities and prices. No buyer name, address,
email, phone or eBay username exists anywhere in either schema.

**The order ingest must keep it true.** Deducting stock needs
`(order_id, line_item_id, sku, quantity, timestamp)` and nothing else. The
buyer is never required, because shipping happens through eBay's own
interface. An `orders` table that acquires a `buyer_name` column would put the
account out of compliance silently.

The endpoint at `/api/ebay/notifications` is implemented regardless, because
receiving the notification is unambiguous where the exemption is an attestation
somebody has to keep true. It logs a notification's **topic only** — an
account-deletion payload carries the closing user's username and id, so logging
it verbatim would create a durable record of exactly the data being disclaimed.
There is nothing to erase on receipt; if that ever changes, that handler is
where the deletion path goes.

## 8. Orders: webhook for latency, poll for correctness

Both paths funnel into one idempotent `ingest_order(order_id)`, deduplicating
on `(order_id, line_item_id)`, which then feeds Module C.

The poll is not a fallback for a flaky webhook; it is the correctness
mechanism. A webhook stream carries no sequence numbers, so a missed
notification is indistinguishable from a quiet afternoon. `getOrders` filtered
on `lastmodifieddate:[cursor..]` is self-healing: advance the cursor only after
a successful ingest and any gap closes on the next tick, whether or not we knew
it existed. Every deploy on the NAS is a container restart, and so a delivery
window we are down for; eBay retries, but a subscriber down for the whole retry
window loses the event permanently. The notification payload carries ids rather
than the order, so a `getOrder` call follows regardless — the webhook is a
trigger, not data.

A missed order means stock is never deducted, which is overselling. That is the
same class of failure as the `skipped_count == 0` rule, and deserves the same
belt-and-braces treatment. Poll every 15 minutes even with webhooks healthy;
the 90-day filter window bounds the worst-case catch-up.

Order push itself still rides the **legacy** Platform Notifications
(`SetNotificationPreferences`, events `FixedPriceTransaction` /
`AuctionCheckoutComplete` / `ItemMarkedPaid`). Note that `ItemSold` fires when a
*listing* ends with a sale, so on a multi-quantity variation listing it stays
silent until the last copy is gone — it is the wrong event for this store.

---

## 9. Build order

Steps 1-4 cannot damage the storefront. Only step 5 can.

1. **OAuth** user token with refresh, plus the marketplace account deletion
   webhook. The latter is mandatory for production keys anyway, and its
   signature verification (`X-EBAY-SIGNATURE`, `getPublicKey`, cache the key
   about an hour) is the same code the order webhook needs.
   *Library done* — `ebay_client` implements both OAuth grants, the transport,
   the endpoint challenge and signature verification, with 45 tests and no
   network access. Not yet wired to an endpoint or a token store, and no eBay
   credentials are configured.
   *Staging done* — the `listing_plan` tables, `tcg_engine.plans` and the
   Drafts tab are built and a plan can be built, edited and approved. Approval
   authorises a push that does not exist yet.
2. **Module B via API** — **done and verified.** Not `getOffers`, which cannot
   see these listings at all (§3a), but the Feed API's Active Inventory
   report, reconciled by the same parser the uploaded report uses. Confirmed
   card-for-card against a manual download of the same store.
3. **Order ingest** — the poll, then the webhook as an accelerator. Still no
   writes to eBay.
4. **Plan tables and the drafts page**, computing real plans with push
   disabled. Validate by diffing generated plans against what the existing CSV
   generator produces for the same input; that oracle already exists and should
   be a test.
5. **Enable push.**

The drafts page therefore arrives at step 4, before any write risk exists.

---

## 10. Open questions

* ~~**How to write to the existing File Exchange listings**~~ (§3a) — settled
  as **option 1, migration**, once the Inventory API path had proved itself on
  real listings it created itself. What changed the answer was that the second
  path stopped being free: dual-path safety has to be expressed in the CSV
  builders, the drafts page, the cover dialog and now the repricer, and every
  one of those is a place to get it wrong silently. `LMS_REVISE_INVENTORY_
  STATUS` would have kept two write paths alive indefinitely on listings that
  are a shrinking minority.

  Migration runs from `scripts/migrate_csv_listings.py`, deliberately outside
  the web app: it is irreversible, it runs once, and nothing in the dashboard
  should be able to trigger it. One listing per call rather than eBay's
  permitted five, recording the returned offer ids immediately and verifying
  the listing is still published before touching the next.
* ~~**The Feed report's exact column names**~~ — settled. It is XML with
  nested variations; see §3a and `AGENTS.md`. `/api/ebay/sync` returns the
  headers it saw and, when nothing matched, an element outline carrying no
  values, so a future shape change is one look rather than an inference.

* **Bin location in the SKU** (§7) — the recommendation moves it out of eBay
  entirely, which changes how orders are physically picked.
* **Approval granularity** — the assumption here is per-plan approval with
  per-item exclusion (`status = 'excluded'`), which is what "move cards in and
  out, then approve" implies. Per-card approval would need a different model.
* **Adding a SKU to an already-live group** is described as applying
  immediately via the group replace, but whether the newly added SKU's offer
  must be published separately first is not settled by the documentation.
  Verify in sandbox before relying on it.
* **Scheduler location** — in-container (APScheduler) versus a Synology task
  hitting an endpoint. Affects the daily price refresh and the order poll
  equally.
