# Privacy Policy

**TCG Card Inventory Middleware**

Last updated: 9 September 2026

## What this application is

A private, self-hosted tool that bridges one seller's own card inventory
between [SortSwift](https://sortswift.com) and their own eBay listings. It runs
on hardware the operator controls — a Synology NAS — and is not a public
service, does not accept sign-ups, and is not offered to third parties. Access
is restricted to accounts the operator has individually approved.

## Data this application stores

**Account data, for access control only.** When someone signs in with Google,
the application stores their Google account identifier, email address and
display name. This is used solely to decide whether that person may use the
dashboard, and to show who is signed in. Nothing is inferred from it, and it is
never used for advertising, profiling or analytics.

**The operator's own card inventory.** Card names, sets, conditions,
quantities, bin locations and prices, imported from the operator's own
SortSwift export.

**The operator's own eBay listing metadata.** eBay item numbers, custom labels
(SKUs), quantities and prices for listings the operator owns, so that the
application can tell what needs updating.

**An eBay OAuth refresh token**, if the operator connects their eBay account.
This authorises the application to manage the operator's own listings and read
their own orders. It is stored on the operator's own hardware and is never
transmitted anywhere except to eBay.

## Data this application does not store

**No personal data belonging to any eBay user.** No buyer name, address, email
address, telephone number or eBay username is written to any database at any
point.

When an eBay order report is processed to reduce stock, it is read in memory
and only the card identity and quantity are used. Nothing from the order —
including anything identifying the buyer — is retained after the request
finishes. Deducting stock requires only an order reference, a SKU and a
quantity; shipping is handled entirely through eBay's own interface, so buyer
details are never needed.

This is enforced as a documented invariant of the codebase, not merely a
current habit. See
[the compliance section of the design notes](docs/ebay-api-design.md).

## eBay marketplace account deletion

The application implements eBay's marketplace account deletion / closure
notification endpoint and verifies the cryptographic signature on every
notification it receives. Because no eBay user's personal data is stored, there
is nothing to erase when such a notification arrives; it is verified,
acknowledged, and recorded only by its topic. The notification payload — which
identifies the closing account — is deliberately not logged, so that receiving
it does not itself create a record of that person.

## Sharing

Nothing is shared, sold, or transferred to any third party. The application
communicates only with:

* **Google**, to verify sign-in tokens;
* **eBay**, to read and manage the operator's own listings and orders;
* **[TCGCSV](https://tcgcsv.com)**, to read publicly available market prices.
  No account or inventory data is sent — only requests for public price data.

## Retention and deletion

All data lives in SQLite databases on the operator's own hardware. There is no
cloud component and no external backup. The operator can delete any account, or
the entire database, from the dashboard at any time. Disconnecting the eBay
account discards the stored refresh token immediately.

## Security

Access requires Google Sign-In; there is no password to guess and no
registration form. The deployment is reachable only over HTTPS. Session cookies
are signed, `HttpOnly` and `Secure`. The eBay refresh token is the only
long-lived credential stored and it is never rendered in the interface or
included in any diagnostic output.

## Contact

Questions about this policy, or requests concerning data held by this
application, should be directed to the repository owner via
[github.com/mrhappyasthma/TCG-Inventory-Middleware](https://github.com/mrhappyasthma/TCG-Inventory-Middleware/issues).
