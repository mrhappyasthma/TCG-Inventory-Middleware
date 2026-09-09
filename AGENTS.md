# Project Guidelines & Rules: TCG Card Inventory Middleware

## 1. 🔄 Git Commit & Push Workflow (Mandatory Rule)

* **Remote**: [`github.com/mrhappyasthma/TCG-Inventory-Middleware`](https://github.com/mrhappyasthma/TCG-Inventory-Middleware) (`origin`, branch `main`).

* **Commit Every Change**: Always create a clean git commit after completing each feature implementation, bug fix, refactoring, or configuration update.
* **Verify Before Committing**: Always run the automated test suites before committing to ensure no regressions:
  ```powershell
  python -m unittest discover -s tcg_engine/tests
  python -m unittest tests/test_web_app.py
  ```
* **Conventional Commit Format**: Use clear, descriptive commit messages following conventional commits:
  * `feat: <summary>` for new features or enhancements
  * `fix: <summary>` for bug fixes
  * `refactor: <summary>` for code restructuring or cleanup
  * `test: <summary>` for test additions or updates
  * `docs: <summary>` for documentation or guideline changes
  * `chore: <summary>` for configuration, Docker, or dependency adjustments
* **Push Immediately After Committing**: Run `git push origin main` as soon as each commit is created. Do not batch commits up locally and do not wait to be asked — the NAS deploys via `git pull origin main`, so an unpushed commit cannot be deployed.
* **Sole Attribution**: Commits are attributed to the repository owner only. Do **not** append a `Co-Authored-By:` trailer (for Claude or any other tool) to commit messages. Message quality is unchanged — conventional prefix, subject line, and a body explaining *why* — only the trailer is omitted.
* **Atomic & Reversible**: Keep commits logical and granular so that changes can be easily tracked, reviewed, or rolled back if needed.
* **History is Append-Only Once Pushed**: The standing push permission covers ordinary commits to `main` only. Force-pushing, rewriting or rebasing pushed commits, creating tags or releases, and deleting branches all require asking first.
* **Never Commit Secrets**: `.env`, `data/*.db` and `data/.session_secret` are gitignored and must stay that way. `.env.example` carries empty placeholders only — never a real `GOOGLE_CLIENT_ID` or `JWT_SECRET`.

---

## 2. 🐳 Synology NAS Deployment & Environment Constraints

* **Deployment Target**: Synology NAS via Docker / Container Manager (`docker-compose.yml`).
* **Persistent Storage**: All persistent database storage must be mapped to `/data` in container and volume mounted to host.
* **Separation of Concerns**:
  * Core engine in [`tcg_engine/`](file:///c:/Users/Mark/Documents/GitHub/SoftSwift-Ebay-CSV-Converter/tcg_engine) MUST remain pure Python with zero web dependencies so it can be extracted to its own repo or executed via CLI.
  * Web backend in [`app/`](file:///c:/Users/Mark/Documents/GitHub/SoftSwift-Ebay-CSV-Converter/app) imports `tcg_engine`.
* **Platform Compatibility**: Code is developed on Windows and deployed to Linux (Synology DSM). Path handling must use `os.path` and avoid OS-specific hardcoding. Explicitly close SQLite connections to prevent Windows file locking.

---

## 3. 🏷️ Business Logic & eBay Listing Rules

* **Compact Sequential IDs**: Master catalog uses `ID1001`, `ID1002`, ... to bypass eBay's 50-character Custom Label SKU limits. IDs are allocated inside a single `BEGIN IMMEDIATE` transaction; never split the lookup and the insert across connections.
* **Bin Location Encoding**: SortSwift remarks are encoded into eBay Custom Labels as `ID1001-Bin_A12`.
* **Runtime settings are the source of truth**: pricing tiers (`pricing_rules`) and listing behaviour (`single_threshold`, `group_by_set`, `variation_title_template`, `category_id` in `listing_settings`) are user-editable and persisted in SQLite. Never hardcode these values in prose or logic; read them via `db.get_listing_setting` / `db.get_pricing_rules`. If documentation disagrees with the configured rules, **fix the documentation**.
* **Multi-Item Variation Listings**: When `group_by_set` is enabled, group cards below `single_threshold` by Set Name **and Condition** into multi-item variation listings (see the grouping-key note below).
* **Single Listings**: Cards at or above `single_threshold` are listed as individual singles, as is every card when `group_by_set` is disabled.
* **eBay 80-Character Title Limit**: Variation titles default to `{set_name}: Pick Your Card - {condition} - Complete Your Set`. When a title is too long, trim the **set name**; never abbreviate or alter the condition, which is a factual claim about the cards.
* **Pass source values through; do not re-map them.** Condition (and comparable fields) are taken verbatim from the input export. There is deliberately no `CONDITION_MAP`: the data comes from SortSwift and goes to eBay or back to SortSwift, so a table of our own is a third vocabulary that can disagree with both. A row missing either the `Condition` string or a numeric `ConditionID` is **skipped with a WARN** rather than having a value guessed for it.
* **Parse third-party CSVs through `tcg_engine.csvtools`**: use `find_column`, `read_csv_text` and `decode_csv_bytes` rather than re-implementing them. Every module previously carried its own column lookup, so a UTF-8 BOM on an eBay report corrupted the first header (`Item number`) and Module B silently recorded every item number as "UNKNOWN" while appearing to succeed. Column matching must stay tolerant of case, whitespace, a BOM and eBay's leading asterisk on required fields.
* **A File Exchange upload identifies a listing by `ItemID`**, not `Item Number`. The latter is the Active Listings *report*'s name for the same value; using it in an upload leaves the row with no identifier. Revising `PicURL` **replaces** the listing's whole picture set rather than adding to it.
* **Deduction quantities are NEGATIVE**: SortSwift's inventory import adds the quantity column to existing stock, so a positive value increases inventory instead of reducing it. Every quantity written into a deduction file must be negative; `deduction_row()` enforces this with `-abs()`. Stock clamps at 0, so over-deducting floors rather than erroring.
* **Seller requirements**: an `Add` needs an item location or eBay returns error 10009. Emit `PostalCode` and **never** `Location` -- they are alternatives and sending both causes that same error. Business policy names (`ShippingProfileName`, `ReturnProfileName`, `PaymentProfileName`) are matched case-sensitively by eBay and are passed through verbatim; a column is omitted entirely when its setting is blank.
* **Item specifics are forwarded, not reconstructed**: any `C:`/`*C:` column in the uploaded export is passed through to the Add file (the asterisk is a template annotation, strip it). Variation parent rows carry only the specifics uniform across the group -- a field that varies per card cannot be a listing-level specific. Singles carry all of theirs. `Game` is required by eBay and its `default_game` setting **overrides** the export, because eBay only accepts values from its own per-category list while SortSwift exports a looser label. Specifics absent as `C:` columns are derived from the plain SortSwift columns (Set, Name, Card Number, Language, Rarity, Printing).
* **Condition Descriptors**: card listings require a `CD:40001` value for ungraded cards. `ConditionID` stays `4000` for every ungraded card regardless of grade; the grade is carried only by the descriptor. Value IDs differ by card family (game/CCG vs sports `261328`) and are selected from `category_id`. This mapping is the sanctioned exception to the pass-through rule, because eBay's four-value enum is not something SortSwift supplies. An explicit `CD:40001` column in the input still wins.
* **Per-variation images must name their option**: a child row's `PicURL` is `<option name>=<url>`. A bare URL is silently ignored by eBay. Only one variation attribute may carry photos. Because `=` is that delimiter, it is stripped from option names alongside `;` and `|`.
* **Variation options are sorted by card number**, numerically (4 before 16 before 133) with prefixes like `TG12` handled, and the parent's option list must stay in the same order as the child rows.
* **eBay variation syntax**: within one attribute, values are separated by `;`; a `|` starts a *different* attribute. The parent row leaves `Relationship` **empty**; only child rows are marked `Variation`.
* **Variation grouping key is (set, condition)**: eBay applies one ConditionID per listing, so a set holding both NM and LP cards must produce two listings.
* **Renaming or removing an element id is a deploy hazard.** A browser holding a cached `index.html` pairs old markup with the new `app.js`, and the failure surfaces as `Cannot read properties of null (reading 'classList')` far from the cause. `GET /` is therefore sent `no-store` and `app.js`/`style.css` URLs carry a content hash, so the HTML and the script it names always match. In `updateAuthUI`, fetch elements with `requireElement(id)` rather than `getElementById`, so a stale page names the missing element and tells the user to hard-reload.
* **A long-running module shows its own busy overlay, cleared in a `finally`.** Use `setModuleBusy(key, title, detail)` / `clearModuleBusy(key)` with keys `Batch`/`Sync`/`Orders`. Clearing anywhere other than a `finally` leaves the card locked forever on an early return or a thrown error -- `handleBatchUpload` returns early on the duplicate-file path, so this is not hypothetical. Keep progress bars indeterminate: the server reports no progress and a percentage would be invented.
* **Bulk work goes inside `db.session()`.** Opening and closing a connection per operation costs ~10 ms, so a few thousand card rows spend minutes on setup alone; the session holds one connection open and is a 6x speedup with unchanged commit semantics. It is thread-local because `Database` is a shared singleton in the web app. Also never read an unchanging table inside a per-row loop -- load pricing rules once and use the pure `apply_pricing_rules`.
* **Never do the pipelines' synchronous work inline in an `async def` endpoint.** Use `run_in_threadpool`: inline, it blocks uvicorn's event loop and freezes the whole dashboard rather than just that request, which reads to the user as a hang.
* **A Revise row is only written when it would change something.** Suppression compares against `last_known_qty` and `last_known_price` -- what eBay is *known* to hold -- and treats every unknown as a change. Never suppress based on `pending_qty`, or a change generated but never uploaded would vanish from all later files. Because `last_known_price` comes from Module B's `Current price` column, nothing is suppressed until a sync has run, which is the correct conservative default.
* **Only Module B may write `ebay_variations.last_known_qty`.** That column means "what eBay reports". Module A records its intent in `pending_qty` instead and must never write `last_known_qty`: generating a CSV is not evidence it was uploaded, and writing it there made the dashboard claim eBay was updated while hiding the drift the two columns exist to show. `upsert_variation` is Module B's call and clears `pending_qty`, since a report from eBay supersedes any outstanding request. Module B also zeroes linked cards absent from the report -- they are not live any more.
* **"Cards" and "copies" are different units.** `total_cards`/`active_listings` count kinds of card; `total_on_hand`/`total_stock` sum physical copies. Label every figure with which it is, and keep the count and sum of the same set filtered identically -- otherwise the ribbon shows copies belonging to cards it is not counting.
* **Batch quantities default to *replace*, not add.** `process_batch_csv(quantity_mode=...)` takes `"set"` (a full inventory dump; the file's total replaces what is stored) or `"add"` (a scan delta). `"set"` is the default because SortSwift's inventory export is a full dump, and the additive reading added the whole inventory to itself on every upload -- a real overselling bug. Never change the default without saying so loudly. Batches are still fingerprinted in `processed_batches` so a duplicate upload is surfaced, though in `"set"` mode a repeat is harmless.
* **In `"set"` mode, rows for one card sum before replacing.** A card in two bins is two rows with one identity, so build a per-file total keyed by `manifest_id` and emit exactly one Revise row per card. Two rows would carry `CustomLabel`s differing by bin, and only one exists on the listing.
* **A Revise row's `CustomLabel` must be the label eBay knows**, read from `ebay_variations.custom_label`, which Module B fills in from eBay's Active Listings report. Do not rebuild it from a card's identity: the bin/remark suffix is not part of the identity and cannot be recovered. Leave `Price` blank on a zero-out row so the listed price is untouched.

---

* **The inventory is shared, not per-user.** There is no owner column on a card, so both backup and restore are administrative and live behind the admin-only Database panel. Enforce it on the endpoint, not just by hiding the control.
* **Pricing rules and listing settings *are* per-user.** Both tables carry a `user_id`, and `SHARED_SCOPE = 0` is the baseline a user inherits until their first save. Never add a query against either table that omits the scope: an unscoped `DELETE` or `SELECT` silently mixes every user's rules together. Thread the caller's `user["id"]` from the endpoint down through `process_batch_csv`; the engine's default of `SHARED_SCOPE` exists for the command line, which has no signed-in user, and is the wrong thing for a request. Pricing rules resolve all-or-nothing, listing settings merge key by key -- see the README for why.
* **Both databases run in WAL mode.** Never copy a `.db` file directly to snapshot it -- recent commits may still be in a `-wal` sidecar. Use `VACUUM INTO`, which checkpoints into a single self-contained file.

---

## 4. 🔐 Authentication Constraints

* **Google Sign-In is the only mechanism.** Do not reintroduce local password login, a registration form, or an auth-disabled development mode, even as a testing convenience. Tests stub `verify_google_id_token` instead.
* **`GOOGLE_CLIENT_ID` is required configuration.** The app must fail fast at startup without it rather than booting into an unusable state, and the ID token audience check must always run.
* **Never ship a default signing secret.** `JWT_SECRET` comes from the environment, or is randomly generated and persisted to the data volume.
* **Google rejects raw-IP and plain-HTTP OAuth origins** (only `localhost` is exempt), so the NAS deployment requires an HTTPS hostname in front of the container. Keep this constraint documented in the README.
