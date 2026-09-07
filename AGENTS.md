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

* **Compact Sequential IDs**: Master catalog uses `ID1001`, `ID1002`, ... to bypass eBay's 50-character Custom Label SKU limits.
* **Bin Location Encoding**: SortSwift remarks are encoded into eBay Custom Labels as `ID1001-Bin_A12`.
* **Multi-Item Variation Listings**: Group cards under the `$5.00` threshold by Set Name into multi-item variation listings.
* **Single Listings**: Cards at or above `$5.00` are listed as individual singles.
* **eBay 80-Character Title Limit**: Variation titles default to `{set_name}: Pick Your Card - Near Mint - Complete Your Set`, with automatic fallback to `NM` if over 80 characters.
* **Condition Integers**: eBay category 183454 requires numeric ConditionIDs (`3000` for NM, `4000` for LP, `5000` for MP, `6000` for HP/DM).
