# Project Guidelines & Rules: TCG Card Inventory Middleware

## 1. 🔄 Git Commit Workflow & History Tracking (Mandatory Rule)

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
* **Atomic & Reversible**: Keep commits logical and granular so that changes can be easily tracked, reviewed, or rolled back if needed.

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
* **Multi-Item Variation Listings**: When `group_by_set` is enabled, group cards below `single_threshold` by Set Name into multi-item variation listings.
* **Single Listings**: Cards at or above `single_threshold` are listed as individual singles, as is every card when `group_by_set` is disabled.
* **eBay 80-Character Title Limit**: Variation titles default to `{set_name}: Pick Your Card - Near Mint - Complete Your Set`, with automatic fallback to `NM` if over 80 characters.
* **Pass source values through; do not re-map them.** Condition (and comparable fields) are taken verbatim from the input export. There is deliberately no `CONDITION_MAP`: the data comes from SortSwift and goes to eBay or back to SortSwift, so a table of our own is a third vocabulary that can disagree with both. A row missing either the `Condition` string or a numeric `ConditionID` is **skipped with a WARN** rather than having a value guessed for it.
* **eBay variation syntax**: within one attribute, values are separated by `;`; a `|` starts a *different* attribute. The parent row leaves `Relationship` **empty**; only child rows are marked `Variation`.
* **Variation grouping key is (set, condition)**: eBay applies one ConditionID per listing, so a set holding both NM and LP cards must produce two listings.
* **Batch quantities are additive**: Module B adds to the live store mirror, so batches are fingerprinted in `processed_batches` and a duplicate upload must be refused unless explicitly forced.

---

## 4. 🔐 Authentication Constraints

* **Google Sign-In is the only mechanism.** Do not reintroduce local password login, a registration form, or an auth-disabled development mode, even as a testing convenience. Tests stub `verify_google_id_token` instead.
* **`GOOGLE_CLIENT_ID` is required configuration.** The app must fail fast at startup without it rather than booting into an unusable state, and the ID token audience check must always run.
* **Never ship a default signing secret.** `JWT_SECRET` comes from the environment, or is randomly generated and persisted to the data volume.
* **Google rejects raw-IP and plain-HTTP OAuth origins** (only `localhost` is exempt), so the NAS deployment requires an HTTPS hostname in front of the container. Keep this constraint documented in the README.
