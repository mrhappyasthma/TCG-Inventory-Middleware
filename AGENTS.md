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
* **Seller requirements**: an `Add` needs an item location or eBay returns error 10009. Emit `PostalCode` and **never** `Location` -- they are alternatives and sending both causes that same error. Business policy names (`ShippingProfileName`, `ReturnProfileName`, `PaymentProfileName`) are matched case-sensitively by eBay and are passed through verbatim; a column is omitted entirely when its setting is blank.
* **Item specifics are forwarded, not reconstructed**: any `C:`/`*C:` column in the uploaded export is passed through to the Add file (the asterisk is a template annotation, strip it). Variation parent rows carry only the specifics uniform across the group -- a field that varies per card cannot be a listing-level specific. Singles carry all of theirs. `Game` is required by eBay and falls back to the `default_game` setting.
* **Condition Descriptors**: card listings require a `CD:40001` value for ungraded cards. `ConditionID` stays `4000` for every ungraded card regardless of grade; the grade is carried only by the descriptor. Value IDs differ by card family (game/CCG vs sports `261328`) and are selected from `category_id`. This mapping is the sanctioned exception to the pass-through rule, because eBay's four-value enum is not something SortSwift supplies. An explicit `CD:40001` column in the input still wins.
* **eBay variation syntax**: within one attribute, values are separated by `;`; a `|` starts a *different* attribute. The parent row leaves `Relationship` **empty**; only child rows are marked `Variation`.
* **Variation grouping key is (set, condition)**: eBay applies one ConditionID per listing, so a set holding both NM and LP cards must produce two listings.
* **Batch quantities are additive**: Module B adds to the live store mirror, so batches are fingerprinted in `processed_batches` and a duplicate upload must be refused unless explicitly forced.

---

## 4. 🔐 Authentication Constraints

* **Google Sign-In is the only mechanism.** Do not reintroduce local password login, a registration form, or an auth-disabled development mode, even as a testing convenience. Tests stub `verify_google_id_token` instead.
* **`GOOGLE_CLIENT_ID` is required configuration.** The app must fail fast at startup without it rather than booting into an unusable state, and the ID token audience check must always run.
* **Never ship a default signing secret.** `JWT_SECRET` comes from the environment, or is randomly generated and persisted to the data volume.
* **Google rejects raw-IP and plain-HTTP OAuth origins** (only `localhost` is exempt), so the NAS deployment requires an HTTPS hostname in front of the container. Keep this constraint documented in the README.
