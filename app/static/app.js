// Application State
let currentUser = null;
let googleClientId = "";
let currentPage = 1;
const pageSize = 20;
let currentSearch = "";
let currentSetFilter = "";

// External link patterns. eBay's /itm/ form is long-standing.
// TCGplayer's numeric /product/ form could not be verified from here (their
// site is a single-page app that returns 200 for any id), so if these links
// do not resolve this is the line to change.
const EBAY_ITEM_URL = "https://www.ebay.com/itm/";
const TCGPLAYER_PRODUCT_URL = "https://www.tcgplayer.com/product/";
let currentSortBy = "manifest_id";
let currentSortDir = "ASC";
// Held so the "Force process anyway" button can resubmit the same upload.
let pendingBatchFile = null;
// Module C still produces a file: the SortSwift deduction import is read by
// SortSwift, not by eBay. The eBay-bound Add and Revise files are gone --
// every listing is managed through the eBay API now.
let storedGeneratedCSVs = {
    orders: null
};

// Initialize Application
document.addEventListener("DOMContentLoaded", () => {
    switchWorkspaceTab("inventory");
    initAuth();
    setupDropzones();
    setupTableListeners();
});

// -------------------------------------------------------------------
// 1. AUTHENTICATION & USER MANAGEMENT
// -------------------------------------------------------------------

// -------------------------------------------------------------------
// MODAL SCROLL LOCK
// -------------------------------------------------------------------

// While any modal is open the page behind it must not scroll, otherwise a
// wheel gesture aimed at the dialog moves the dashboard instead.
function openModals() {
    // Select on the data-modal marker, NOT an id suffix: "[id$=Modal]" also
    // matched the btnOpenLoginModal and btnOpenAddCardModal buttons, and
    // btnOpenAddCardModal is never hidden, so the lock could never release.
    return Array.from(document.querySelectorAll("[data-modal]"))
        .filter(el => !el.classList.contains("hidden"));
}

function syncModalScrollLock() {
    document.body.classList.toggle("overflow-hidden", openModals().length > 0);
}

// Google Sign-In readiness. The GSI client script is loaded with async/defer,
// so the button can only be rendered once BOTH the SDK has loaded and the
// server has told us which client ID to use. Either can win the race.
let googleSdkReady = false;
let authConfigLoaded = false;
let googleButtonRendered = false;

// Documented GSI hook, invoked by the Google script once it is ready.
window.onGoogleLibraryLoad = () => {
    googleSdkReady = true;
    maybeRenderGoogleButton();
};

async function initAuth() {
    try {
        const res = await fetch("/api/auth/me");
        const data = await res.json();
        googleClientId = data.google_client_id;
        currentUser = data.user;

        authConfigLoaded = true;
        maybeRenderGoogleButton();

        updateAuthUI(data);

        if (data.is_authenticated) {
            fetchStats();
            fetchSetFilter();
            fetchInventory();
            // Cheap: reads local configuration and the stored token, and calls
            // nothing at eBay. Done on arrival so Module B's card can point at
            // the automated path without the eBay panel being opened first.
            refreshEbayStatus();
            // The console starts with what happened while nobody was here,
            // rather than empty.
            loadPersistedLogs();
        } else if (data.is_pending) {
            document.getElementById("pendingApprovalBanner").classList.remove("hidden");
        } else {
            openAuthModal();
        }

        // If the Google script never arrives (no outbound network, blocked
        // domain), say so instead of leaving an empty modal.
        setTimeout(() => {
            if (!googleButtonRendered) showGoogleUnavailable();
        }, 4000);
    } catch (err) {
        logToTerminal("ERROR", `Auth check failed: ${err.message}`);
    }
}

function maybeRenderGoogleButton() {
    if (!googleSdkReady || !authConfigLoaded || googleButtonRendered) return;

    const target = document.getElementById("googleSignInBtn");
    if (!target) return;

    if (!googleClientId || !window.google || !window.google.accounts || !window.google.accounts.id) {
        showGoogleUnavailable();
        return;
    }

    try {
        google.accounts.id.initialize({
            client_id: googleClientId,
            callback: handleGoogleCallback,
        });
        google.accounts.id.renderButton(target, {
            type: "standard",
            theme: "filled_black",
            size: "large",
            text: "signin_with",
            shape: "pill",
            logo_alignment: "left",
        });
        googleButtonRendered = true;
        document.getElementById("googleAuthUnavailable")?.classList.add("hidden");
    } catch (err) {
        logToTerminal("ERROR", `Google Sign-In could not initialize: ${err.message}`);
        showGoogleUnavailable();
    }
}

function showGoogleUnavailable() {
    document.getElementById("googleAuthUnavailable")?.classList.remove("hidden");
}

// A browser holding a cached index.html from a previous deploy will be missing
// elements this script expects, and the failure surfaces as an unreadable
// "Cannot read properties of null" somewhere far from the cause. Naming the
// element and the remedy turns that into a one-line diagnosis. The server now
// sends the HTML no-store so this should not recur, but a proxy or service
// worker can still serve a stale copy.
function requireElement(id) {
    const el = document.getElementById(id);
    if (!el) {
        throw new Error(
            `This page is out of date: it has no "${id}" element, which this ` +
            `script version expects. Reload with Ctrl+Shift+R (Cmd+Shift+R on ` +
            `a Mac) to pick up the current page.`
        );
    }
    return el;
}

function updateAuthUI(data) {
    const btnOpenLogin = requireElement("btnOpenLoginModal");
    const btnAccountMenu = requireElement("btnAccountMenu");
    const pendingBanner = requireElement("pendingApprovalBanner");

    if (data.is_authenticated && data.user) {
        btnOpenLogin.classList.add("hidden");
        btnAccountMenu.classList.remove("hidden");
        btnAccountMenu.classList.add("flex");
        pendingBanner.classList.add("hidden");

        requireElement("userNameText").innerText = data.user.username;
        requireElement("userAvatarText").innerText = data.user.username[0].toUpperCase();
        requireElement("accountMenuName").innerText = data.user.username;
        requireElement("accountMenuEmail").innerText = data.user.email || "";

        const roleBadge = requireElement("userRoleBadge");
        roleBadge.innerText = data.user.role.toUpperCase();

        // One group rather than two buttons: the divider and the whole admin
        // section should disappear together for an ordinary user, leaving a
        // menu that holds only Sign out.
        const adminGroup = requireElement("accountMenuAdminGroup");
        if (data.user.role === "admin") {
            adminGroup.classList.remove("hidden");
            roleBadge.className = "text-[10px] uppercase px-1.5 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-800";
        } else {
            // Both entries expose or replace shared data. The endpoints
            // enforce that too; this only hides controls that would 403.
            adminGroup.classList.add("hidden");
            roleBadge.className = "text-[10px] uppercase px-1.5 py-0.5 rounded bg-indigo-950 text-indigo-300 border border-indigo-800";
        }
    } else {
        btnOpenLogin.classList.remove("hidden");
        btnAccountMenu.classList.add("hidden");
        btnAccountMenu.classList.remove("flex");
        requireElement("accountMenuAdminGroup").classList.add("hidden");
        closeAccountMenu();

        if (data.is_pending) {
            pendingBanner.classList.remove("hidden");
        } else {
            pendingBanner.classList.add("hidden");
        }
    }
}

function openAuthModal() {
    document.getElementById("authModal").classList.remove("hidden");
    syncModalScrollLock();
    document.getElementById("authErrorMsg").classList.add("hidden");
    maybeRenderGoogleButton();
}

function closeAuthModal() {
    document.getElementById("authModal").classList.add("hidden");
    syncModalScrollLock();
}

async function handleGoogleCallback(response) {
    const errorBox = document.getElementById("authErrorMsg");
    errorBox.classList.add("hidden");
    try {
        const res = await fetch("/api/auth/google", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id_token: response.credential })
        });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || "Google sign-in failed");
        }
        closeAuthModal();
        logToTerminal("SUCCESS", data.message);
        initAuth();
    } catch (err) {
        errorBox.innerText = err.message;
        errorBox.classList.remove("hidden");
    }
}

// -------------------------------------------------------------------
// ACCOUNT MENU
// -------------------------------------------------------------------

function accountMenuIsOpen() {
    const menu = document.getElementById("accountMenu");
    return menu && !menu.classList.contains("hidden");
}

function openAccountMenu() {
    const menu = document.getElementById("accountMenu");
    if (!menu) return;
    menu.classList.remove("hidden");
    document.getElementById("btnAccountMenu")?.setAttribute("aria-expanded", "true");
    document.getElementById("accountMenuChevron")?.classList.add("rotate-180");
}

function closeAccountMenu() {
    const menu = document.getElementById("accountMenu");
    if (!menu) return;
    menu.classList.add("hidden");
    document.getElementById("btnAccountMenu")?.setAttribute("aria-expanded", "false");
    document.getElementById("accountMenuChevron")?.classList.remove("rotate-180");
}

document.getElementById("btnAccountMenu")?.addEventListener("click", (e) => {
    // Stop this from immediately reaching the close-on-outside-click handler.
    e.stopPropagation();
    if (accountMenuIsOpen()) closeAccountMenu(); else openAccountMenu();
});

// Any choice in the menu leads somewhere else, so the menu always closes. This
// runs on the container, so it covers every entry including ones added later.
document.getElementById("accountMenu")?.addEventListener("click", closeAccountMenu);

document.addEventListener("click", (e) => {
    if (!accountMenuIsOpen()) return;
    if (e.target.closest("#accountMenu") || e.target.closest("#btnAccountMenu")) return;
    closeAccountMenu();
});

// Escape closes the menu. Handled separately from the modal handler, which
// bails out when no modal is open and would otherwise leave this stuck.
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && accountMenuIsOpen()) closeAccountMenu();
});

document.getElementById("btnLogout").addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    logToTerminal("INFO", "Logged out.");
    window.location.reload();
});

document.getElementById("btnOpenLoginModal").addEventListener("click", openAuthModal);

// Pricing Rules Management
let cachedPricingRules = [];

document.getElementById("btnPricingRules").addEventListener("click", openPricingModal);

function openPricingModal() {
    document.getElementById("pricingModal").classList.remove("hidden");
    syncModalScrollLock();
    loadPricingRules();
}

function closePricingModal() {
    document.getElementById("pricingModal").classList.add("hidden");
    syncModalScrollLock();
}

async function loadPricingRules() {
    const tbody = document.getElementById("pricingRulesTableBody");
    try {
        const res = await fetch("/api/pricing-rules");
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        cachedPricingRules = data.rules;
        setScopeBadge("pricingScopeBadge", data.is_own);
        renderPricingRulesEditor();
        updateTestPricePreview();
        loadConditionMultipliers();
        loadAutoReprice();
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-rose-400">Failed to load rules: ${escapeHtml(err.message)}</td></tr>`;
    }
}

// Rules and settings are per-user, but an inherited set looks exactly like an
// edited one. The badge is the only thing that distinguishes them, which
// matters because resetting an inherited set does nothing visible.
function setScopeBadge(elementId, isOwn) {
    const badge = document.getElementById(elementId);
    if (!badge) return;
    if (isOwn) {
        badge.innerText = "Yours";
        badge.title = "You have saved your own; other users are unaffected by changes here.";
        badge.className = "text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded bg-brand-600/20 text-brand-400 border border-brand-500/40";
    } else {
        badge.innerText = "Shared defaults";
        badge.title = "You have not customised these, so you are using the shared defaults. Saving creates your own copy.";
        badge.className = "text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700";
    }
}

function renderPricingRulesEditor() {
    const tbody = document.getElementById("pricingRulesTableBody");
    // The section starts collapsed, so the count is how you know there is
    // anything behind the caret without opening it.
    const badge = document.getElementById("pricingRulesCount");
    if (badge) {
        const n = cachedPricingRules.length;
        badge.innerText = `${n} rule${n === 1 ? "" : "s"}`;
    }
    if (cachedPricingRules.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-slate-500">No pricing rules defined. Click "Add Tier Rule" or "Reset Defaults".</td></tr>`;
        return;
    }

    tbody.innerHTML = cachedPricingRules.map((rule, idx) => {
        return `
            <tr class="hover:bg-dark-900/60 transition-colors">
                <td class="py-2 px-3">
                    <div class="flex items-center gap-1">
                        <span class="text-slate-500">$</span>
                        <input type="number" step="0.01" min="0" value="${rule.min_price}" onchange="updateRuleField(${idx}, 'min_price', this.value)" class="w-24 px-2 py-1 rounded bg-dark-900 border border-slate-700 text-white font-mono text-xs focus:outline-none focus:border-brand-500">
                    </div>
                </td>
                <td class="py-2 px-3">
                    <div class="flex items-center gap-1">
                        <span class="text-slate-500">$</span>
                        <input type="number" step="0.01" min="0" placeholder="Infinity" value="${rule.max_price !== null && rule.max_price !== undefined ? rule.max_price : ''}" onchange="updateRuleField(${idx}, 'max_price', this.value)" class="w-24 px-2 py-1 rounded bg-dark-900 border border-slate-700 text-white font-mono text-xs focus:outline-none focus:border-brand-500">
                    </div>
                </td>
                <td class="py-2 px-3">
                    <select onchange="updateRuleField(${idx}, 'rule_type', this.value)" class="px-2 py-1 rounded bg-dark-900 border border-slate-700 text-slate-200 text-xs focus:outline-none focus:border-brand-500">
                        <option value="fixed" ${rule.rule_type === 'fixed' ? 'selected' : ''}>Fixed Base Price ($)</option>
                        <option value="markup_fixed" ${rule.rule_type === 'markup_fixed' ? 'selected' : ''}>Market Price + Diff ($)</option>
                        <option value="markup_percent" ${rule.rule_type === 'markup_percent' ? 'selected' : ''}>Market Price + Markup (%)</option>
                    </select>
                </td>
                <td class="py-2 px-3">
                    <div class="flex items-center gap-1">
                        <input type="number" step="0.01" value="${rule.rule_value}" onchange="updateRuleField(${idx}, 'rule_value', this.value)" class="w-24 px-2 py-1 rounded bg-dark-900 border border-slate-700 text-white font-mono text-xs focus:outline-none focus:border-brand-500">
                    </div>
                </td>
                <td class="py-2 px-3 text-right">
                    <button onclick="deletePricingRuleRow(${idx})" class="p-1 rounded text-slate-500 hover:text-rose-400 hover:bg-rose-950 transition-colors" title="Delete Rule">
                        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                    </button>
                </td>
            </tr>
        `;
    }).join("");
}

function updateRuleField(idx, field, value) {
    if (field === "min_price" || field === "rule_value") {
        cachedPricingRules[idx][field] = parseFloat(value || 0);
    } else if (field === "max_price") {
        cachedPricingRules[idx][field] = value === "" || value === null ? null : parseFloat(value);
    } else {
        cachedPricingRules[idx][field] = value;
    }
    updateTestPricePreview();
}

function addEmptyPricingRuleRow() {
    let lastMax = 0;
    if (cachedPricingRules.length > 0) {
        const lastRule = cachedPricingRules[cachedPricingRules.length - 1];
        lastMax = lastRule.max_price !== null ? lastRule.max_price : lastRule.min_price + 1.0;
    }
    cachedPricingRules.push({
        min_price: lastMax,
        max_price: null,
        rule_type: "fixed",
        rule_value: 1.99,
        sort_order: cachedPricingRules.length + 1
    });
    renderPricingRulesEditor();
    updateTestPricePreview();
}

function deletePricingRuleRow(idx) {
    cachedPricingRules.splice(idx, 1);
    renderPricingRulesEditor();
    updateTestPricePreview();
}

async function savePricingRules() {
    try {
        const res = await fetch("/api/pricing-rules", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rules: cachedPricingRules })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        cachedPricingRules = data.rules;
        setScopeBadge("pricingScopeBadge", data.is_own);
        await saveConditionMultipliers();
        closePricingModal();
        logToTerminal("SUCCESS", "Saved your pricing rules and condition multipliers. Other users are unaffected.");
    } catch (err) {
        alert(err.message);
    }
}

// -------------------------------------------------------------------
// CONDITION MULTIPLIERS
// -------------------------------------------------------------------
//
// The market price available to us is product-level: neither TCGplayer's
// public price data nor the SortSwift export it was relayed through breaks
// down by condition. The grade discount is therefore policy, configured
// here, and applied before the tier rules so a played card falls into a
// cheaper tier rather than the one its mint price implies.

let cachedConditionMultipliers = [];

async function loadConditionMultipliers() {
    const target = document.getElementById("conditionMultipliersRows");
    if (!target) return;
    try {
        const res = await fetch("/api/condition-multipliers");
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        cachedConditionMultipliers = data.multipliers || [];
        setScopeBadge("conditionScopeBadge", data.is_own);
        const countBadge = document.getElementById("conditionMultipliersCount");
        if (countBadge) {
            const n = cachedConditionMultipliers.length;
            countBadge.innerText = `${n} grade${n === 1 ? "" : "s"}`;
        }

        target.innerHTML = cachedConditionMultipliers.map((m, idx) => `
            <label class="flex items-center gap-2 p-2 rounded-lg bg-dark-900/70 border border-slate-700/70">
                <span class="w-9 shrink-0 text-[11px] font-bold font-mono text-slate-200">${escapeHtml(m.condition_key)}</span>
                <input type="number" step="0.01" min="0" max="10" value="${m.multiplier}"
                       onchange="updateConditionMultiplier(${idx}, this.value)"
                       class="w-20 bg-dark-800 border border-slate-700 rounded px-2 py-1 text-xs font-mono text-white focus:border-brand-500 focus:outline-none">
                <span class="text-[10px] text-slate-500 truncate">${escapeHtml(m.label || "")}</span>
            </label>
        `).join("");
    } catch (err) {
        target.innerHTML = `<p class="text-[11px] text-rose-300">${escapeHtml(err.message)}</p>`;
    }
}

function updateConditionMultiplier(idx, value) {
    const parsed = parseFloat(value);
    // Leave the cached value alone on a non-numeric entry rather than storing
    // NaN, which serialises as null and would be rejected by the endpoint.
    if (!Number.isFinite(parsed)) return;
    cachedConditionMultipliers[idx].multiplier = parsed;
    updateTestPricePreview();
}

async function saveConditionMultipliers() {
    const res = await fetch("/api/condition-multipliers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ multipliers: cachedConditionMultipliers })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    cachedConditionMultipliers = data.multipliers;
    setScopeBadge("conditionScopeBadge", data.is_own);
}

async function resetConditionMultipliers() {
    if (!confirm("Discard your own condition multipliers and go back to the shared defaults?")) return;
    try {
        const res = await fetch("/api/condition-multipliers/reset", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);
        logToTerminal("INFO", "Condition multipliers reset to the shared defaults.");
        await loadConditionMultipliers();
        updateTestPricePreview();
    } catch (err) {
        alert(err.message);
    }
}

// -------------------------------------------------------------------
// MARKET PRICE REFRESH / REPRICE
// -------------------------------------------------------------------

async function refreshMarketPrices() {
    const button = document.getElementById("btnRefreshPrices");
    const label = document.getElementById("btnRefreshPricesLabel");
    const icon = document.getElementById("refreshPricesIcon");
    const status = document.getElementById("priceRefreshStatus");

    button.disabled = true;
    label.innerText = "Fetching\u2026";
    icon.classList.add("animate-spin");
    status.innerText = "";

    try {
        const form = new FormData();
        form.append("force", "false");
        const res = await fetch("/api/pricing/refresh", {
            method: "POST", body: form,
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Refresh failed");

        (data.logs || []).forEach(l => logToTerminal(l.level, `[PRICES] ${l.message}`));

        status.innerText = data.skipped
            ? `Already current (${data.snapshot || "unknown snapshot"})`
            : `Updated ${data.updated} card(s) from ${data.groups_fetched} set(s)`;

        // The nightly repricer will act on these prices; show what it
        // would now do.
        await loadAutoReprice();
    } catch (err) {
        // A failed fetch leaves every stored price untouched, so this is
        // informational rather than something to recover from.
        status.innerText = err.message;
        logToTerminal("ERROR", `[PRICES] ${err.message}`);
    } finally {
        button.disabled = false;
        label.innerText = "Refresh now";
        icon.classList.remove("animate-spin");
    }
}

// -- automatic repricing ---------------------------------------------------
//
// The nightly job logs to the terminal, which is what was asked for, but a
// Synology container's console is nobody's dashboard. These read the same
// decisions the job reaches, so a hold can be reviewed before it expires.

async function loadAutoReprice() {
    const badge = document.getElementById("autoRepriceBadge");
    try {
        const [preview, settings] = await Promise.all([
            fetch("/api/pricing/auto-reprice/preview").then(r => r.json()),
            fetch("/api/listing-settings").then(r => r.json()),
        ]);

        const s = settings.settings || {};
        document.getElementById("autoRepriceEnabled").checked =
            String(s.auto_reprice_enabled ?? "true").toLowerCase() !== "false";
        document.getElementById("priceHoldDays").value = s.price_hold_days || "14";
        document.getElementById("priceBoundaryMargin").value =
            s.price_boundary_margin_percent || "10";
        document.getElementById("repriceMaxChange").value =
            s.reprice_max_change_percent || "25";

        if (badge) {
            badge.innerText = preview.enabled ? "On" : "Off";
            badge.className = preview.enabled
                ? "text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded bg-emerald-950 text-accent-emerald border border-emerald-800"
                : "text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700";
        }
        renderAutoReprice(preview);
    } catch (err) {
        // Advisory, like the reprice indicator: the job runs whether or not
        // this panel could be drawn.
        console.error("auto reprice preview:", err);
    }
}

function renderAutoReprice(preview) {
    const box = document.getElementById("autoRepriceSummary");
    const text = document.getElementById("autoRepriceSummaryText");
    const holds = document.getElementById("autoRepriceHolds");
    const changes = document.getElementById("autoRepriceChanges");
    if (!box || !text || !holds || !changes) return;

    if (!preview.considered) {
        text.innerText = "No API-managed listing has a price to review yet. Push a plan to eBay and these listings become eligible.";
        holds.innerHTML = "";
        changes.innerHTML = "";
        box.classList.remove("hidden");
        return;
    }

    const parts = [`${preview.considered} card(s) reviewed`];
    if (preview.change_count) parts.push(`${preview.change_count} would change now`);
    if (preview.hold_count) parts.push(`${preview.hold_count} in a holding window`);
    text.innerText = preview.over_cap
        ? `${parts.join(", ")}. That is ${Math.round((preview.change_share || 0) * 100)}% of eligible cards, over the cap — the run would be refused as suspected bad market data.`
        : `${parts.join(", ")}.`;

    holds.innerHTML = (preview.holds || []).map(h => `
        <div class="flex items-center gap-2 text-[11px] p-1.5 rounded-lg bg-amber-950/40 border border-amber-800/60">
            <span class="text-amber-400 shrink-0" title="Held above the market">&#9873;</span>
            <span class="font-mono text-slate-300 truncate">${escapeHtml(h.label)}</span>
            <span class="ml-auto font-mono text-slate-200 shrink-0">$${Number(h.current_price).toFixed(2)}
                <span class="text-slate-500">held vs</span> $${Number(h.target_price).toFixed(2)}</span>
            <span class="font-mono text-amber-400 shrink-0">${Math.round(h.days_remaining)}d</span>
        </div>`).join("");

    changes.innerHTML = (preview.changes || []).map(c => `
        <div class="flex items-center gap-2 text-[11px] p-1.5 rounded-lg bg-dark-800/60 border border-slate-700/60">
            <span class="${c.verdict === "raise" ? "text-accent-emerald" : "text-rose-400"} shrink-0">${c.verdict === "raise" ? "&uarr;" : "&darr;"}</span>
            <span class="font-mono text-slate-300 truncate">${escapeHtml(c.label)}</span>
            <span class="ml-auto font-mono text-slate-200 shrink-0">$${Number(c.current_price).toFixed(2)} &rarr; $${Number(c.target_price).toFixed(2)}</span>
        </div>`).join("");

    box.classList.remove("hidden");
}

async function saveRepriceSettings() {
    const settings = {
        auto_reprice_enabled:
            document.getElementById("autoRepriceEnabled").checked ? "true" : "false",
        price_hold_days: document.getElementById("priceHoldDays").value || "14",
        price_boundary_margin_percent:
            document.getElementById("priceBoundaryMargin").value || "10",
        reprice_max_change_percent:
            document.getElementById("repriceMaxChange").value || "25",
    };
    try {
        const res = await fetch("/api/listing-settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ settings }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Save failed");
        logToTerminal("SUCCESS", `[REPRICE] Saved: ${settings.auto_reprice_enabled === "true" ? "on" : "off"}, ${settings.price_hold_days}-day hold, ${settings.price_boundary_margin_percent}% boundary margin, refusing runs over ${settings.reprice_max_change_percent}%.`);
        await loadAutoReprice();
    } catch (err) {
        logToTerminal("ERROR", `[REPRICE] ${err.message}`);
    }
}

async function runAutoReprice() {
    const changes = await fetch("/api/pricing/auto-reprice/preview")
        .then(r => r.json())
        .catch(() => null);
    if (changes && changes.change_count) {
        const ok = confirm(`Apply ${changes.change_count} price change(s) to your live eBay listings now?\n\nOnly the price is sent — never quantity. ${changes.hold_count || 0} card(s) in a holding window are left alone.`);
        if (!ok) return;
    }

    const button = document.getElementById("btnRepriceNow");
    const label = document.getElementById("btnRepriceNowLabel");
    button.disabled = true;
    label.innerText = "Repricing…";
    try {
        const res = await fetch("/api/pricing/auto-reprice", {
            method: "POST", body: new FormData(),
        });
        const data = await readJsonResponse(res);
        if (!res.ok) throw new Error(data.detail || "Reprice failed");

        (data.logs || []).forEach(l => logToTerminal(l.level, `[REPRICE] ${l.message}`));
        if (!data.attempted && data.reason) {
            logToTerminal("WARN", `[REPRICE] Nothing applied: ${data.reason}`);
        }
        await loadAutoReprice();
        await fetchEbayListings();
    } catch (err) {
        logToTerminal("ERROR", `[REPRICE] ${err.message}`);
    } finally {
        button.disabled = false;
        label.innerText = "Run now";
    }
}

async function resetDefaultPricingRules() {
    if (!confirm("Discard your own pricing rules and go back to the shared defaults?")) return;
    try {
        const res = await fetch("/api/pricing-rules/reset", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        cachedPricingRules = data.rules;
        setScopeBadge("pricingScopeBadge", data.is_own);
        renderPricingRulesEditor();
        updateTestPricePreview();
        logToTerminal("INFO", "Your pricing rules were discarded; you are back on the shared defaults.");
    } catch (err) {
        alert(err.message);
    }
}

async function resetListingSettings() {
    if (!confirm("Discard your own listing settings and go back to the shared defaults?")) return;
    try {
        const res = await fetch("/api/listing-settings/reset", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        logToTerminal("INFO", "Your listing settings were discarded; you are back on the shared defaults.");
        await loadListingSettings();
        updateTitlePreview();
    } catch (err) {
        alert(err.message);
    }
}

async function updateTestPricePreview() {
    const inputVal = parseFloat(document.getElementById("testPriceInput").value || "0");
    const resultBox = document.getElementById("testPriceResult");

    // Compute locally using cached rules
    let finalPrice = inputVal;
    let matched = null;

    for (const rule of cachedPricingRules) {
        const minP = rule.min_price;
        const maxP = rule.max_price;
        let isMatch = false;
        if (maxP !== null && maxP !== undefined) {
            if (inputVal >= minP && inputVal < maxP) isMatch = true;
        } else {
            if (inputVal >= minP) isMatch = true;
        }

        if (isMatch) {
            matched = rule;
            if (rule.rule_type === "fixed") finalPrice = rule.rule_value;
            else if (rule.rule_type === "markup_fixed") finalPrice = inputVal + rule.rule_value;
            else if (rule.rule_type === "markup_percent") finalPrice = inputVal * (1 + rule.rule_value / 100);
            break;
        }
    }

    resultBox.innerText = `$${finalPrice.toFixed(2)}`;
}

document.getElementById("testPriceInput")?.addEventListener("input", updateTestPricePreview);

// -------------------------------------------------------------------
// LISTING & VARIATION GROUPING RULES MANAGEMENT
// -------------------------------------------------------------------

document.getElementById("btnListingRules")?.addEventListener("click", openListingModal);

function openListingModal() {
    document.getElementById("listingModal").classList.remove("hidden");
    syncModalScrollLock();
    loadListingSettings();
}

function closeListingModal() {
    document.getElementById("listingModal").classList.add("hidden");
    syncModalScrollLock();
}

async function loadListingSettings() {
    try {
        const res = await fetch("/api/listing-settings");
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        const s = data.settings || {};
        setScopeBadge("listingScopeBadge", (data.own_keys || []).length > 0);
        if (s.single_threshold) {
            document.getElementById("settingSingleThreshold").value = s.single_threshold;
        }
        if (s.variation_title_template) {
            document.getElementById("settingTitleTemplate").value = s.variation_title_template;
        }
        document.getElementById("settingGroupBySet").checked =
            String(s.group_by_set ?? "true").toLowerCase() !== "false";
        document.getElementById("settingDescriptorStyle").value =
            s.condition_descriptor_style || "label_id";
        document.getElementById("settingPostalCode").value = s.seller_postal_code || "";
        document.getElementById("settingDefaultGame").value = s.default_game || "";
        document.getElementById("settingOptionTemplate").value =
            s.variation_option_template || "{name} ({card_number})";
        document.getElementById("settingCoverImage").value = s.cover_image_url || "";
        document.getElementById("settingShippingProfile").value = s.shipping_profile_name || "";
        document.getElementById("settingReturnProfile").value = s.return_profile_name || "";
        document.getElementById("settingPaymentProfile").value = s.payment_profile_name || "";
        updateTitlePreview();
    } catch (err) {
        logToTerminal("ERROR", `Failed to load listing settings: ${err.message}`);
    }
}

async function saveListingSettings(e) {
    if (e && e.preventDefault) e.preventDefault();
    const threshold = document.getElementById("settingSingleThreshold").value || "5.00";
    const template = document.getElementById("settingTitleTemplate").value || "{set_name}: Pick Your Card - {condition} - Complete Your Set";
    const groupBySet = document.getElementById("settingGroupBySet").checked;
    const descriptorStyle = document.getElementById("settingDescriptorStyle").value;
    const postalCode = document.getElementById("settingPostalCode").value.trim();
    const defaultGame = document.getElementById("settingDefaultGame").value.trim();
    const optionTemplate = document.getElementById("settingOptionTemplate").value.trim()
        || "{name} ({card_number})";
    const coverImage = document.getElementById("settingCoverImage").value.trim();
    const shippingProfile = document.getElementById("settingShippingProfile").value.trim();
    const returnProfile = document.getElementById("settingReturnProfile").value.trim();
    const paymentProfile = document.getElementById("settingPaymentProfile").value.trim();

    try {
        const res = await fetch("/api/listing-settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                settings: {
                    single_threshold: threshold,
                    variation_title_template: template,
                    group_by_set: groupBySet ? "true" : "false",
                    condition_descriptor_style: descriptorStyle,
                    seller_postal_code: postalCode,
                    default_game: defaultGame,
                    variation_option_template: optionTemplate,
                    cover_image_url: coverImage,
                    shipping_profile_name: shippingProfile,
                    return_profile_name: returnProfile,
                    payment_profile_name: paymentProfile
                }
            })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        setScopeBadge("listingScopeBadge", (data.own_keys || []).length > 0);
        closeListingModal();
        if (!postalCode) {
            logToTerminal("ERROR", "No postal code set - eBay will reject Add files with error 10009 (missing Item.Location).");
        }
        logToTerminal(
            "SUCCESS",
            `Updated Listing Rules: Single Threshold = $${parseFloat(threshold).toFixed(2)}, ` +
            `Set Grouping = ${groupBySet ? "on" : "off"}, Title Template saved.`
        );
    } catch (err) {
        alert(err.message);
    }
}

let titlePreviewTimer = null;

// Ask the server to render the title rather than reimplementing the
// 80-character fallback here. Two copies of that logic would inevitably drift.
function updateTitlePreview() {
    clearTimeout(titlePreviewTimer);
    titlePreviewTimer = setTimeout(runTitlePreview, 200);
}

async function runTitlePreview() {
    const setName = (document.getElementById("testSetTitleInput")?.value || "").trim();
    const condition = (document.getElementById("testConditionInput")?.value || "").trim();
    const template = document.getElementById("settingTitleTemplate")?.value
        || "{set_name}: Pick Your Card - {condition} - Complete Your Set";

    const displayEl = document.getElementById("generatedTitleDisplay");
    const countEl = document.getElementById("titleCharCount");

    try {
        const res = await fetch("/api/listing-settings/preview-title", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ set_name: setName, condition, template })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Preview failed");

        if (displayEl) displayEl.innerText = data.generated_title;
        if (countEl) {
            countEl.innerText = `${data.char_count} / 80 Chars`;
            countEl.className = data.is_valid
                ? "font-mono text-accent-cyan font-semibold"
                : "font-mono text-rose-400 font-semibold";
        }
    } catch (err) {
        if (displayEl) displayEl.innerText = `Preview unavailable: ${err.message}`;
    }
}

document.getElementById("settingTitleTemplate")?.addEventListener("input", updateTitlePreview);
document.getElementById("testSetTitleInput")?.addEventListener("input", updateTitlePreview);
document.getElementById("testConditionInput")?.addEventListener("input", updateTitlePreview);

// Admin User Management
document.getElementById("btnAdminPanel").addEventListener("click", openAdminModal);

function openAdminModal() {
    document.getElementById("adminModal").classList.remove("hidden");
    syncModalScrollLock();
    loadAdminUsers();
}

function closeAdminModal() {
    document.getElementById("adminModal").classList.add("hidden");
    syncModalScrollLock();
}

async function loadAdminUsers() {
    const tbody = document.getElementById("adminUserTableBody");
    try {
        const res = await fetch("/api/admin/users");
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        if (data.users.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" class="py-4 text-center text-slate-500">No users found.</td></tr>`;
            return;
        }

        tbody.innerHTML = data.users.map(u => {
            const isSelf = currentUser && u.id === currentUser.id;
            const statusColor = u.status === 'active' ? 'text-emerald-400 bg-emerald-950 border-emerald-800' :
                                u.status === 'pending' ? 'text-amber-400 bg-amber-950 border-amber-800' :
                                'text-rose-400 bg-rose-950 border-rose-800';

            return `
                <tr class="hover:bg-dark-900/60 transition-colors">
                    <td class="py-2.5 px-3 font-medium text-white">${escapeHtml(u.username)} ${isSelf ? '<span class="text-[10px] text-brand-400 font-mono">(You)</span>' : ''}</td>
                    <td class="py-2.5 px-3 text-slate-400">${escapeHtml(u.email || '-')}</td>
                    <td class="py-2.5 px-3 font-mono text-[11px] text-slate-400">${escapeHtml(u.auth_provider)}</td>
                    <td class="py-2.5 px-3">
                        <span class="px-2 py-0.5 rounded text-[10px] uppercase font-semibold ${u.role === 'admin' ? 'bg-amber-950 text-amber-300 border border-amber-800' : 'bg-slate-800 text-slate-300'}">
                            ${escapeHtml(u.role)}
                        </span>
                    </td>
                    <td class="py-2.5 px-3">
                        <span class="px-2 py-0.5 rounded text-[10px] uppercase font-semibold border ${statusColor}">
                            ${escapeHtml(u.status)}
                        </span>
                    </td>
                    <td class="py-2.5 px-3 text-slate-500 text-[11px]">${new Date(u.created_at).toLocaleDateString()}</td>
                    <td class="py-2.5 px-3 text-right space-x-1">
                        ${u.status === 'pending' ? `
                            <button onclick="setUserStatus(${u.id}, 'active')" class="px-2 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-white text-[11px] font-semibold">Approve</button>
                        ` : ''}
                        ${!isSelf && u.status === 'active' ? `
                            <button onclick="setUserStatus(${u.id}, 'disabled')" class="px-2 py-1 rounded bg-slate-800 hover:bg-rose-900 text-slate-300 hover:text-rose-200 text-[11px]">Deactivate</button>
                        ` : ''}
                        ${!isSelf && u.status === 'disabled' ? `
                            <button onclick="setUserStatus(${u.id}, 'active')" class="px-2 py-1 rounded bg-slate-800 hover:bg-emerald-900 text-slate-300 hover:text-emerald-200 text-[11px]">Reactivate</button>
                        ` : ''}
                        ${!isSelf && u.role === 'user' ? `
                            <button onclick="setUserRole(${u.id}, 'admin')" class="px-2 py-1 rounded bg-slate-800 hover:bg-amber-900 text-slate-300 hover:text-amber-200 text-[11px]">Make Admin</button>
                        ` : ''}
                        ${!isSelf && u.role === 'admin' ? `
                            <button onclick="setUserRole(${u.id}, 'user')" class="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[11px]">Demote</button>
                        ` : ''}
                        ${!isSelf ? `
                            <button onclick="deleteUserAccount(${u.id})" class="px-2 py-1 rounded bg-rose-950 hover:bg-rose-900 text-rose-300 text-[11px]" title="Delete User">Del</button>
                        ` : ''}
                    </td>
                </tr>
            `;
        }).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" class="py-4 text-center text-rose-400">Failed to load users: ${escapeHtml(err.message)}</td></tr>`;
    }
}

async function setUserStatus(userId, status) {
    try {
        const res = await fetch(`/api/admin/users/${userId}/status`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status })
        });
        if (!res.ok) throw new Error((await res.json()).detail);
        logToTerminal("SUCCESS", `User #${userId} status set to ${status}`);
        loadAdminUsers();
    } catch (err) {
        alert(err.message);
    }
}

async function setUserRole(userId, role) {
    try {
        const res = await fetch(`/api/admin/users/${userId}/role`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ role })
        });
        if (!res.ok) throw new Error((await res.json()).detail);
        logToTerminal("SUCCESS", `User #${userId} role set to ${role}`);
        loadAdminUsers();
    } catch (err) {
        alert(err.message);
    }
}

async function deleteUserAccount(userId) {
    if (!confirm("Are you sure you want to delete this user?")) return;
    try {
        const res = await fetch(`/api/admin/users/${userId}`, { method: "DELETE" });
        if (!res.ok) throw new Error((await res.json()).detail);
        logToTerminal("INFO", `User #${userId} deleted.`);
        loadAdminUsers();
    } catch (err) {
        alert(err.message);
    }
}

// -------------------------------------------------------------------
// 2. DROPZONES & FILE PIPELINES (MODULES A, B, C)
// -------------------------------------------------------------------

function setupDropzones() {
    // 1. Orders
    bindDropzone("dropzoneOrders", "fileInputOrders", "labelOrders", handleOrdersUpload);
    // 2. Batch
    bindDropzone("dropzoneBatch", "fileInputBatch", "labelBatch", handleBatchUpload);
    // 3. Sync
    bindDropzone("dropzoneSync", "fileInputSync", "labelSync", handleSyncUpload);

    // Download button event listeners
    document.getElementById("btnDownloadOrders").addEventListener("click", () => {
        if (storedGeneratedCSVs.orders) {
            triggerBrowserDownload(storedGeneratedCSVs.orders, "sortswift_orders_import.csv");
        }
    });

    document.getElementById("btnDownloadOnlyBatch").addEventListener("click", () => {
        if (!pendingBatchFile) return;
        document.getElementById("batchDuplicateWarning").classList.add("hidden");
        logToTerminal("INFO", `[MODULE A] Previewing ${pendingBatchFile.name} — nothing will be written.`);
        handleBatchUpload(pendingBatchFile, "dry-run");
    });

    document.getElementById("btnForceProcessBatch").addEventListener("click", () => {
        if (!pendingBatchFile) return;
        document.getElementById("batchDuplicateWarning").classList.add("hidden");
        logToTerminal("WARN", `[MODULE A] Force processing ${pendingBatchFile.name} - quantities will be added again.`);
        handleBatchUpload(pendingBatchFile, "force");
    });

}

function bindDropzone(zoneId, inputId, labelId, uploadHandler) {
    const zone = document.getElementById(zoneId);
    const input = document.getElementById(inputId);
    const label = document.getElementById(labelId);

    zone.addEventListener("click", () => input.click());

    input.addEventListener("change", () => {
        if (input.files.length > 0) {
            label.innerText = input.files[0].name;
            uploadHandler(input.files[0]);
        }
    });

    zone.addEventListener("dragover", (e) => {
        e.preventDefault();
        zone.classList.add("dragover");
    });

    zone.addEventListener("dragleave", () => {
        zone.classList.remove("dragover");
    });

    zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("dragover");
        if (e.dataTransfer.files.length > 0) {
            const file = e.dataTransfer.files[0];
            label.innerText = file.name;
            uploadHandler(file);
        }
    });
}

async function handleOrdersUpload(file) {
    const formData = new FormData();
    formData.append("file", file);

    logToTerminal("INFO", `[MODULE C] Uploading ${file.name} for eBay Orders processing...`);
    const orderRows = await countCsvRows(file);
    setModuleBusy("Orders", "Processing orders\u2026",
                  orderRows === null ? file.name : `${orderRows.toLocaleString()} rows`);

    try {
        const res = await fetch("/api/process/orders", {
            method: "POST",
            body: formData
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to process orders");

        // Display logs
        if (data.logs) {
            data.logs.forEach(l => logToTerminal(l.level, l.message));
        }

        storedGeneratedCSVs.orders = data.csv_content;
        document.getElementById("resultBoxOrders").classList.remove("hidden");
        document.getElementById("ordersReadyText").innerText =
            `sortswift_orders_import.csv ready (${data.converted_count} items)`;

        // The file is built and held in memory; downloading is an explicit
        // click so an unwanted file is never dropped into Downloads.
        logToTerminal("SUCCESS", `[MODULE C] sortswift_orders_import.csv is ready (${data.converted_count} items). Click to download.`);
    } catch (err) {
        logToTerminal("ERROR", `[MODULE C] ${err.message}`);
    } finally {
        clearModuleBusy("Orders");
    }
}

// Which arithmetic the uploaded file implies. Defaults to treating it as a
// full inventory dump, because that is what SortSwift's inventory export is
// and because the additive reading double-counts stock on every upload.
function selectedQuantityMode() {
    const picked = document.querySelector('input[name="batchQuantityMode"]:checked');
    return picked ? picked.value : "set";
}

// "Force" means something different in each mode, so the button must not
// promise one behaviour while doing the other.
function syncBatchModeLabels() {
    const isSet = selectedQuantityMode() === "set";
    const label = document.getElementById("btnForceProcessBatchLabel");
    if (label) {
        label.innerText = isSet
            ? "Force process (replaces quantities)"
            : "Force process (adds quantities)";
    }
    const note = document.getElementById("batchModeNote");
    if (note) {
        note.innerText = isSet
            ? "Cards live on eBay but missing from a full dump are treated as sold out and set to 0."
            : "Only use this for a file containing nothing you have already processed.";
    }
}

document.querySelectorAll('input[name="batchQuantityMode"]').forEach(el => {
    el.addEventListener("change", syncBatchModeLabels);
});
syncBatchModeLabels();

// -------------------------------------------------------------------
// PER-MODULE BUSY STATE
// -------------------------------------------------------------------
//
// Each module gets its own indicator rather than one global spinner, because
// the three are independent and you may well be reading one while another
// runs. The bar is deliberately indeterminate: the server reports no progress,
// so a percentage would be invented. What it does show is real -- the row count
// read from the file, and an elapsed clock, which is what tells you a slow run
// on the NAS is alive rather than wedged.

const moduleBusyTimers = {};

// Row count straight from the file, so the message says something concrete
// before the server has even been reached. Counts non-blank lines and drops the
// header; a trailing newline must not become a phantom row.
async function countCsvRows(file) {
    try {
        const text = await file.text();
        const lines = text.split(/\r?\n/).filter(line => line.trim() !== "");
        return Math.max(0, lines.length - 1);
    } catch (err) {
        return null;
    }
}

function setModuleBusy(key, title, detail) {
    const overlay = document.getElementById(`busy${key}`);
    if (!overlay) return;

    document.getElementById(`busy${key}Title`).innerText = title;

    const started = Date.now();
    const detailEl = document.getElementById(`busy${key}Detail`);
    const paint = () => {
        const secs = Math.floor((Date.now() - started) / 1000);
        const clock = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
        detailEl.innerText = detail ? `${detail} \u00b7 ${clock}` : clock;
    };
    paint();

    clearInterval(moduleBusyTimers[key]);
    moduleBusyTimers[key] = setInterval(paint, 1000);

    overlay.classList.remove("hidden");
    setModuleInputsDisabled(key, true);
}

function clearModuleBusy(key) {
    clearInterval(moduleBusyTimers[key]);
    delete moduleBusyTimers[key];
    document.getElementById(`busy${key}`)?.classList.add("hidden");
    setModuleInputsDisabled(key, false);
}

// Locking the input is not cosmetic: a second drop into Module A mid-run would
// process the same dump twice.
function setModuleInputsDisabled(key, disabled) {
    const input = document.getElementById(`fileInput${key}`);
    if (input) input.disabled = disabled;

    const zone = document.getElementById(`dropzone${key}`);
    if (zone) {
        zone.classList.toggle("pointer-events-none", disabled);
        zone.classList.toggle("opacity-50", disabled);
    }
}

// mode: "normal" | "force" | "dry-run"
async function handleBatchUpload(file, mode = "normal") {
    if (mode === "normal") {
        document.getElementById("batchDuplicateWarning").classList.add("hidden");
        document.getElementById("resultBoxBatch").classList.add("hidden");
    }
    const formData = new FormData();
    formData.append("file", file);
    formData.append("force", mode === "force" ? "true" : "false");
    formData.append("dry_run", mode === "dry-run" ? "true" : "false");
    formData.append("quantity_mode", selectedQuantityMode());

    const intent = mode === "dry-run"
        ? "rebuilding files only"
        : mode === "force" ? "force processing" : "routing";
    logToTerminal("INFO", `[MODULE A] Uploading ${file.name} (${intent})...`);
    const batchRows = await countCsvRows(file);
    setModuleBusy(
        "Batch",
        mode === "dry-run" ? "Rebuilding CSVs\u2026" : "Processing batch\u2026",
        batchRows === null ? file.name : `${batchRows.toLocaleString()} rows`
    );

    try {
        const res = await fetch("/api/process/batch", {
            method: "POST",
            body: formData
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to process batch");

        // Display logs
        if (data.logs) {
            data.logs.forEach(l => logToTerminal(l.level, l.message));
        }

        // Batch quantities are additive, so the server refuses a file it has
        // already applied and writes nothing. Surface that inline with an
        // explicit override rather than a blocking confirm() dialog.
        if (data.duplicate) {
            pendingBatchFile = file;
            const detail = (data.logs || []).find(l => l.level === "WARN");
            const base = detail
                ? detail.message
                : `"${file.name}" has already been processed. Nothing was added to your live inventory.`;
            document.getElementById("batchDuplicateText").innerText = base
                + " Choose Download only to rebuild the CSVs with your current settings,"
                + " or Force process to apply the batch a second time.";
            document.getElementById("batchDuplicateWarning").classList.remove("hidden");
            document.getElementById("resultBoxBatch").classList.add("hidden");
            return;
        }

        // A clean run: hide any stale duplicate warning.
        pendingBatchFile = null;
        document.getElementById("batchDuplicateWarning").classList.add("hidden");

        const resultBox = document.getElementById("resultBoxBatch");
        resultBox.classList.remove("hidden");

        const parts = [];
        if (data.parsed_rows > 0) parts.push(`${data.parsed_rows} card(s) read`);
        if (data.new_catalog_count > 0) parts.push(`${data.new_catalog_count} new`);
        if (data.skipped_count > 0) parts.push(`${data.skipped_count} skipped`);
        // Worth calling out separately: these are cards being pulled from
        // sale, not routine changes.
        if (data.zeroed_count > 0) parts.push(`${data.zeroed_count} sold out → 0`);
        const suffix = data.dry_run ? " (nothing written)" : "";
        // Every row unusable is a failure, not an empty result. Saying
        // "nothing to do" there reads like everything was already in order,
        // which is the opposite of the truth.
        const nothingParsed = data.parsed_rows === 0 && data.skipped_count > 0;
        // The draft is staged by the server as part of the upload, so this
        // is what the ingest produced rather than a promise of a file.
        const planned = (data.plan && data.plan.item_count) || 0;
        document.getElementById("batchReadyText").innerText = nothingParsed
            ? `No rows could be read — all ${data.skipped_count} were skipped. See the console for why.`
            : planned
            ? `Catalogue updated — ${parts.join(", ")}. Draft staged with ${planned} change(s): review it on the Drafts tab.`
            : `Catalogue updated — ${parts.join(", ")}${suffix}. Nothing for eBay to change.`;

        logToTerminal(
            "SUCCESS",
            `[MODULE A] Ingest complete${parts.length ? " (" + parts.join(", ") + ")" : ""}${suffix}.`
            + (planned ? ` Draft staged with ${planned} change(s).` : "")
        );

        if (data.reconciled === false && data.skipped_count > 0
            && data.parsed_rows > 0) {
            logToTerminal(
                "WARN",
                `[MODULE A] Sold-out reconciliation was skipped: ${data.skipped_count} row(s) could not be read, so a missing card cannot be told apart from an unreadable one. No listing was revised to 0.`
            );
        }

        if (data.zeroed_count > 0) {
            logToTerminal(
                "WARN",
                `[MODULE A] ${data.zeroed_count} card(s) live on eBay were absent from this dump, so the catalogue is now 0 for them. The draft will ask eBay to stop selling them — check that list before approving it.`
            );
        }

        // A preview wrote nothing, so there is nothing to refresh.
        if (!data.dry_run) {
            fetchStats();
            fetchSetFilter();
            fetchInventory();
            // The server staged the draft as part of the upload, so this
            // only has to display it. It used to be rebuilt from here,
            // which meant a CLI or API upload left no draft at all.
            fetchDraftPlan();
        }
    } catch (err) {
        logToTerminal("ERROR", `[MODULE A] ${err.message}`);
    } finally {
        clearModuleBusy("Batch");
    }
}

async function handleSyncUpload(file) {
    const formData = new FormData();
    formData.append("file", file);

    logToTerminal("INFO", `[MODULE B] Uploading ${file.name} for eBay Store State synchronization...`);
    const syncRows = await countCsvRows(file);
    setModuleBusy("Sync", "Syncing store mirror\u2026",
                  syncRows === null ? file.name : `${syncRows.toLocaleString()} rows`);

    try {
        const res = await fetch("/api/process/sync", {
            method: "POST",
            body: formData
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to sync listings");

        if (data.logs) {
            data.logs.forEach(l => logToTerminal(l.level, l.message));
        }

        const resultBox = document.getElementById("resultBoxSync");
        resultBox.classList.remove("hidden");
        const listings = data.linked_listing_count || 0;
        document.getElementById("syncSummaryText").innerText =
            `Synced ${data.synced_count} variation(s) across ${listings} listing(s)`;

        // The listings view is derived from what this just wrote.
        fetchEbayListings();

        fetchStats();
        fetchSetFilter();
        fetchInventory();
    } catch (err) {
        logToTerminal("ERROR", `[MODULE B] ${err.message}`);
    } finally {
        clearModuleBusy("Sync");
    }
}

function triggerBrowserDownload(content, filename) {
    const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute("download", filename);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

// -------------------------------------------------------------------
// 3. LIVE INVENTORY TABLE & STATS
// -------------------------------------------------------------------

function setupTableListeners() {
    const searchInput = document.getElementById("inventorySearch");
    let debounceTimer = null;
    searchInput.addEventListener("input", (e) => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
            currentSearch = e.target.value;
            currentPage = 1;
            fetchInventory();
        }, 250);
    });

    const setFilter = document.getElementById("inventorySetFilter");
    if (setFilter) {
        setFilter.addEventListener("change", (e) => {
            currentSetFilter = e.target.value;
            currentPage = 1;
            fetchInventory();
        });
    }

    document.getElementById("btnOpenAddCardModal").addEventListener("click", () => {
        document.getElementById("addCardModal").classList.remove("hidden");
        syncModalScrollLock();
    });
}

function closeAddCardModal() {
    document.getElementById("addCardModal").classList.add("hidden");
    syncModalScrollLock();
}

async function handleAddCard(e) {
    e.preventDefault();
    const payload = {
        product_name: document.getElementById("addCardName").value,
        set_name: document.getElementById("addSetName").value,
        condition: document.getElementById("addCondition").value,
        printing: document.getElementById("addPrinting").value,
        ebay_parent_id: document.getElementById("addEbayId").value || null,
        quantity: parseInt(document.getElementById("addQuantity").value || "0")
    };

    try {
        const res = await fetch("/api/inventory/add", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        closeAddCardModal();
        logToTerminal("SUCCESS", `Card saved: [${data.manifest_id}] ${payload.product_name} (${payload.set_name})`);
        fetchStats();
        fetchSetFilter();
        fetchInventory();
    } catch (err) {
        alert(err.message);
    }
}

// Rebuilt after every change to the catalog so the filter never offers a set
// that no longer has cards behind it.
async function fetchSetFilter() {
    const select = document.getElementById("inventorySetFilter");
    if (!select) return;
    try {
        const res = await fetch("/api/inventory/sets");
        if (!res.ok) return;
        const sets = (await res.json()).sets || [];

        const previous = currentSetFilter;
        select.innerHTML = '<option value="">All expansion sets</option>'
            + sets.map(s =>
                `<option value="${escapeHtml(s.set_name)}">`
                + `${escapeHtml(s.set_name)} (${s.card_count})</option>`
              ).join("");

        // Keep the selection if that set still exists; otherwise fall back to
        // showing everything rather than silently filtering to nothing.
        if (previous && sets.some(s => s.set_name === previous)) {
            select.value = previous;
        } else if (previous) {
            currentSetFilter = "";
            select.value = "";
        }
    } catch (err) {
        console.error("Set filter load failed:", err);
    }
}

async function fetchStats() {
    try {
        const res = await fetch("/api/stats");
        if (res.ok) {
            const data = await res.json();
            document.getElementById("statTotalCards").innerText = data.total_cards.toLocaleString();
            document.getElementById("statTotalOnHand").innerText = (data.total_on_hand ?? 0).toLocaleString();
            document.getElementById("statActiveListings").innerText = data.active_listings.toLocaleString();
            document.getElementById("statTotalStock").innerText = data.total_stock.toLocaleString();
        }
    } catch (err) {
        console.error("Stats fetch error:", err);
    }
}

async function fetchInventory() {
    const tbody = document.getElementById("inventoryTableBody");
    const offset = (currentPage - 1) * pageSize;
    const params = new URLSearchParams({
        search: currentSearch,
        sort_by: currentSortBy,
        sort_dir: currentSortDir,
        limit: String(pageSize),
        offset: String(offset),
    });
    if (currentSetFilter) params.set("set_name", currentSetFilter);
    const url = `/api/inventory?${params.toString()}`;

    try {
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        renderInventoryTable(data.items, data.total, offset);
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="12" class="py-6 text-center text-rose-400">Failed to load inventory: ${escapeHtml(err.message)}</td></tr>`;
    }
}

// The quantity dialog needs the row it was opened from.
let lastInventoryItems = [];

// -------------------------------------------------------------------
// Card image preview on hover
// -------------------------------------------------------------------

// How far from the cursor the preview sits, so it never lands under the
// pointer and starts flickering between enter and leave.
const CARD_PREVIEW_OFFSET = 18;

function cardPreviewAttrs(item) {
    // Nothing is emitted for a card with no picture, so the whole preview
    // path is inert rather than showing an empty frame or a broken image.
    if (!item.cdn_image) return "";
    // The data attribute is both the payload and the CSS hook for the
    // zoom-in cursor, so a cell without a picture gets neither.
    return `data-card-image="${escapeHtml(item.cdn_image)}"`;
}

// A row-height thumbnail, shared by the inventory table and the drafts page.
//
// Hovering the thumbnail is what opens the full-size preview; the card name
// and number deliberately no longer do. A link you want to click should not
// also be a hover target, and putting the affordance on the picture makes it
// obvious where to point.
//
// An absent picture still renders a placeholder of the same size, so the
// column does not change width row to row and the table stays aligned.
function cardThumbnailCell(item, cellClasses = "py-2 px-4") {
    if (!item.cdn_image) {
        return `<td class="${cellClasses}"><span class="block w-7 h-10 rounded border border-dashed border-slate-800" title="No image in the export"></span></td>`;
    }
    return `
        <td class="${cellClasses}" ${cardPreviewAttrs(item)}>
            <img src="${escapeHtml(item.cdn_image)}" alt="" loading="lazy"
                class="block h-10 w-auto rounded border border-slate-700 bg-dark-900"
                onerror="this.style.visibility='hidden'">
        </td>`;
}

function positionCardPreview(event) {
    const box = document.getElementById("cardPreview");
    if (!box) return;
    const rect = box.getBoundingClientRect();
    // Flip to the other side of the cursor when there is not room, so a card
    // near the right edge or the bottom of the window stays fully visible
    // instead of being clipped.
    let left = event.clientX + CARD_PREVIEW_OFFSET;
    if (left + rect.width > window.innerWidth - 8) {
        left = event.clientX - rect.width - CARD_PREVIEW_OFFSET;
    }
    let top = event.clientY + CARD_PREVIEW_OFFSET;
    if (top + rect.height > window.innerHeight - 8) {
        top = window.innerHeight - rect.height - 8;
    }
    box.style.left = `${Math.max(8, left)}px`;
    box.style.top = `${Math.max(8, top)}px`;
}

// A card and a listing cover want different sizes: a card has a known
// physical shape and reads best at it, while a cover photo is any shape at all
// and only needs to be big enough to recognise. The kind travels on the
// element rather than being guessed from the image, because the same URL could
// legitimately be either.
function showCardPreview(url, event, kind = "card") {
    const box = document.getElementById("cardPreview");
    const img = document.getElementById("cardPreviewImage");
    if (!box || !img || !url) return;
    if (img.getAttribute("src") !== url) img.setAttribute("src", url);
    box.classList.toggle("preview-cover", kind === "cover");
    box.classList.toggle("preview-card", kind !== "cover");
    box.classList.add("visible");
    positionCardPreview(event);
}

function hideCardPreview() {
    document.getElementById("cardPreview")?.classList.remove("visible");
}

// Delegated on the table body rather than bound per cell: the rows are
// replaced wholesale on every render, and per-cell listeners would have to be
// rebound each time -- or leak.
let cardPreviewBound = false;

// Bound once on the document rather than per table. Both the inventory table
// and the drafts page carry thumbnails, the drafts page rebuilds its whole
// group list on every edit, and a per-container binding would have to be
// re-attached after each render -- or leak one listener per render.
function initCardPreview() {
    if (cardPreviewBound) return;
    cardPreviewBound = true;

    document.addEventListener("mouseover", (event) => {
        const cell = event.target.closest("[data-card-image]");
        if (!cell) return;
        showCardPreview(
            cell.getAttribute("data-card-image"),
            event,
            cell.getAttribute("data-preview-kind") || "card",
        );
    });
    document.addEventListener("mousemove", (event) => {
        // Cheapest possible guard first: mousemove fires constantly, and
        // there is nothing to reposition unless a preview is actually up.
        const box = document.getElementById("cardPreview");
        if (!box || !box.classList.contains("visible")) return;
        if (event.target.closest("[data-card-image]")) {
            positionCardPreview(event);
        } else {
            // Left the thumbnail without a mouseout firing -- happens when
            // the row is re-rendered from under the pointer.
            hideCardPreview();
        }
    });
    document.addEventListener("mouseout", (event) => {
        const from = event.target.closest("[data-card-image]");
        // relatedTarget is where the pointer went; staying inside the same
        // cell must not hide the preview, or it flickers.
        if (from && from.contains(event.relatedTarget)) return;
        if (from) hideCardPreview();
    });
    // A scroll moves the row out from under a preview that is positioned in
    // viewport coordinates, which would leave it floating over nothing.
    // Captured, so it catches scrolling inside any table wrapper too.
    document.addEventListener("scroll", hideCardPreview, {
        passive: true,
        capture: true,
    });
}

function renderInventoryTable(items, total, offset) {
    lastInventoryItems = items || [];
    const tbody = document.getElementById("inventoryTableBody");
    // Idempotent, and done here because the tbody is guaranteed to exist by
    // the time rows are being written into it.
    initCardPreview();
    // A re-render replaces the row the pointer was over, so a preview left
    // showing would belong to a card that is no longer under the cursor.
    hideCardPreview();

    if (!items || items.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="12" class="py-8 text-center text-slate-500">
                    No cards found. Process a SortSwift batch or add a card above.
                </td>
            </tr>
        `;
        document.getElementById("paginationInfo").innerText = "Showing 0 of 0 records";
        document.getElementById("btnPrevPage").disabled = true;
        document.getElementById("btnNextPage").disabled = true;
        return;
    }

    tbody.innerHTML = items.map(item => {
        const conditionBadge = getConditionBadgeClass(item.condition);
        const stockBadge = item.last_known_qty > 0
            ? 'bg-emerald-950/80 text-emerald-400 border border-emerald-800'
            : 'bg-slate-900 text-slate-500 border border-slate-800';

        // Our count vs what eBay last reported. A mismatch is the interesting
        // case, so flag it rather than making them diff by eye.
        const qty = item.quantity ?? 0;
        const drifted = qty !== item.last_known_qty;
        const qtyBadge = drifted
            ? 'bg-amber-950/80 text-amber-300 border border-amber-800'
            : 'bg-slate-900 text-slate-400 border border-slate-800';

        // The gap between the two figures is what the draft proposes to
        // change. There is no third "asked for" state any more: a push
        // confirms in the same call, so eBay's figure is either stale or
        // current, never in flight.
        const driftTitle = drifted
            ? `You hold ${qty}, eBay reports ${item.last_known_qty}. Rebuild the draft to push the difference, or run Module B to re-sync.`
            : 'Your count matches what eBay reports.';

        return `
            <tr class="hover:bg-dark-800/80 transition-colors">
                <td class="py-3 px-4 font-mono font-bold text-accent-cyan">${escapeHtml(item.manifest_id)}</td>
                <td class="py-3 px-4 font-medium text-white">${
                    item.tcgplayer_id
                        ? `<a href="${TCGPLAYER_PRODUCT_URL}${encodeURIComponent(item.tcgplayer_id)}" target="_blank" rel="noopener noreferrer" class="hover:text-accent-cyan hover:underline transition-colors" title="View on TCGplayer">${escapeHtml(item.product_name)}</a>`
                        : escapeHtml(item.product_name)
                }</td>
                <td class="py-3 px-4 font-mono text-slate-300">${item.card_number ? escapeHtml(item.card_number) : '<span class="text-slate-600 italic">-</span>'}</td>
                ${cardThumbnailCell(item, "py-2 px-4")}
                <td class="py-3 px-4 text-slate-400">${escapeHtml(item.set_name)}</td>
                <td class="py-3 px-4">
                    <span class="px-2 py-0.5 rounded text-[10px] font-medium ${conditionBadge}">
                        ${escapeHtml(item.condition)}
                    </span>
                </td>
                <td class="py-3 px-4">
                    <span class="px-2 py-0.5 rounded text-[10px] bg-dark-900 border border-slate-700 text-slate-300">
                        ${escapeHtml(item.printing)}
                    </span>
                </td>
                <td class="py-3 px-4 font-mono text-slate-300">
                    ${item.remarks ? `<span class="px-2 py-0.5 rounded text-[10px] bg-indigo-950 text-indigo-300 border border-indigo-800/60">${escapeHtml(item.remarks)}</span>` : '<span class="text-slate-600 italic text-[11px]">-</span>'}
                </td>
                <td class="py-3 px-4 font-mono text-slate-300">${
                    item.ebay_parent_id
                        ? `<a href="${EBAY_ITEM_URL}${encodeURIComponent(item.ebay_parent_id)}" target="_blank" rel="noopener noreferrer" class="text-accent-cyan hover:underline" title="Open the eBay listing">${escapeHtml(item.ebay_parent_id)}</a>`
                        : '<span class="text-slate-600 italic">Not on eBay</span>'
                }</td>
                <td class="py-3 px-4 text-center">
                    <button onclick="openQuantityModal('${item.manifest_id}')" title="${escapeHtml(driftTitle)} Click to adjust." class="inline-block min-w-[28px] px-2 py-0.5 rounded-full text-[11px] font-bold font-mono ${qtyBadge} hover:ring-1 hover:ring-brand-500 transition-all cursor-pointer">
                        ${qty}
                    </button>
                </td>
                <td class="py-3 px-4 text-center whitespace-nowrap" title="${escapeHtml(driftTitle)}">
                    <span class="inline-block min-w-[28px] px-2 py-0.5 rounded-full text-[11px] font-bold font-mono ${stockBadge}">
                        ${item.last_known_qty}
                    </span>
                </td>
                <td class="py-3 px-4 text-right">
                    <button onclick="deleteCard('${item.manifest_id}')" class="text-slate-500 hover:text-rose-400 p-1 transition-colors" title="Delete Card">
                        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                    </button>
                </td>
            </tr>
        `;
    }).join("");

    // Update Pagination UI
    const start = total > 0 ? offset + 1 : 0;
    const end = Math.min(offset + pageSize, total);
    document.getElementById("paginationInfo").innerText = `Showing ${start} to ${end} of ${total} records`;

    const totalPages = Math.ceil(total / pageSize) || 1;
    document.getElementById("pageIndicator").innerText = `Page ${currentPage} of ${totalPages}`;
    document.getElementById("btnPrevPage").disabled = currentPage <= 1;
    document.getElementById("btnNextPage").disabled = currentPage >= totalPages;
}

function handleSort(col) {
    if (currentSortBy === col) {
        currentSortDir = currentSortDir === "ASC" ? "DESC" : "ASC";
    } else {
        currentSortBy = col;
        currentSortDir = "ASC";
    }
    fetchInventory();
}

function changePage(delta) {
    currentPage += delta;
    if (currentPage < 1) currentPage = 1;
    fetchInventory();
}

async function deleteCard(manifestId) {
    if (!confirm(`Are you sure you want to delete card [${manifestId}] from catalog?`)) return;
    try {
        const res = await fetch(`/api/inventory/${manifestId}`, { method: "DELETE" });
        if (!res.ok) throw new Error((await res.json()).detail);
        logToTerminal("INFO", `Deleted card [${manifestId}]`);
        fetchStats();
        fetchSetFilter();
        fetchInventory();
    } catch (err) {
        alert(err.message);
    }
}

// -------------------------------------------------------------------
// MANUAL QUANTITY ADJUSTMENT
// -------------------------------------------------------------------

let quantityEditManifestId = null;
let quantityEditPrevious = 0;

function openQuantityModal(manifestId) {
    const row = (lastInventoryItems || []).find(i => i.manifest_id === manifestId);
    quantityEditManifestId = manifestId;
    quantityEditPrevious = row ? (row.quantity ?? 0) : 0;

    document.getElementById("quantityModalCard").innerText = row
        ? `[${row.manifest_id}] ${row.product_name}`
          + `${row.card_number ? " #" + row.card_number : ""} - ${row.set_name}`
        : `[${manifestId}]`;
    document.getElementById("quantityPrevious").innerText = quantityEditPrevious;
    document.getElementById("quantityInput").value = quantityEditPrevious;
    document.getElementById("quantityGenerateDeduction").checked = false;
    document.getElementById("quantityDeductionNote").classList.add("hidden");

    document.getElementById("quantityModal").classList.remove("hidden");
    syncModalScrollLock();
    document.getElementById("quantityInput").focus();
    document.getElementById("quantityInput").select();
}

function closeQuantityModal() {
    document.getElementById("quantityModal").classList.add("hidden");
    syncModalScrollLock();
    quantityEditManifestId = null;
}

// Show what a deduction would cover before the change is committed.
function updateQuantityDeductionNote() {
    const note = document.getElementById("quantityDeductionNote");
    const wanted = parseInt(document.getElementById("quantityInput").value || "0", 10);
    const checked = document.getElementById("quantityGenerateDeduction").checked;
    const delta = quantityEditPrevious - (isNaN(wanted) ? quantityEditPrevious : wanted);

    if (checked && delta > 0) {
        note.innerText = `A deduction CSV for ${delta} unit(s) will be produced.`;
        note.classList.remove("hidden");
    } else if (checked && delta <= 0) {
        note.innerText = "No deduction: the quantity is not decreasing.";
        note.classList.remove("hidden");
    } else {
        note.classList.add("hidden");
    }
}

document.getElementById("quantityInput")?.addEventListener("input", updateQuantityDeductionNote);
document.getElementById("quantityGenerateDeduction")?.addEventListener("change", updateQuantityDeductionNote);

async function saveQuantity(e) {
    if (e && e.preventDefault) e.preventDefault();
    if (!quantityEditManifestId) return;

    const quantity = parseInt(document.getElementById("quantityInput").value || "0", 10);
    const generate = document.getElementById("quantityGenerateDeduction").checked;

    try {
        const res = await fetch(`/api/inventory/${encodeURIComponent(quantityEditManifestId)}/quantity`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ quantity, generate_deduction: generate })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to update quantity");

        const id = quantityEditManifestId;
        closeQuantityModal();
        logToTerminal("SUCCESS",
            `Quantity for [${id}] changed from ${data.previous} to ${data.current}.`);

        if (data.csv_content) {
            triggerBrowserDownload(data.csv_content, `sortswift_deduction_${id}.csv`);
            logToTerminal("INFO",
                `Deduction CSV for ${data.deducted} unit(s) downloaded for [${id}].`);
        } else if (generate) {
            logToTerminal("INFO",
                "No deduction file: the quantity did not decrease.");
        }

        fetchStats();
        fetchInventory();
    } catch (err) {
        alert(err.message);
    }
}

function getConditionBadgeClass(condition) {
    const c = (condition || "").toLowerCase();
    if (c.includes("near mint")) return "badge-near-mint";
    if (c.includes("lightly played")) return "badge-lightly-played";
    if (c.includes("moderately played")) return "badge-moderately-played";
    if (c.includes("heavily played")) return "badge-heavily-played";
    if (c.includes("damaged")) return "badge-damaged";
    return "badge-near-mint";
}

// -------------------------------------------------------------------
// WORKSPACE TABS
// -------------------------------------------------------------------

const WORKSPACE_TABS = {
    inventory: { panel: "panelInventory", button: "tabBtnInventory" },
    listings: { panel: "panelListings", button: "tabBtnListings" },
    drafts: { panel: "panelDrafts", button: "tabBtnDrafts" },
    console: { panel: "panelConsole", button: "tabBtnConsole" },
};

const TAB_ACTIVE = "text-white border-brand-500";
const TAB_IDLE = "text-slate-400 hover:text-slate-200 border-transparent";

let activeWorkspaceTab = "inventory";
// Log lines that arrived while the console was hidden. Without this, an error
// would land on an invisible tab and never be noticed.
let consoleUnread = 0;
let consoleUnreadHasError = false;

function switchWorkspaceTab(key) {
    if (!WORKSPACE_TABS[key]) return;
    activeWorkspaceTab = key;

    for (const [name, ids] of Object.entries(WORKSPACE_TABS)) {
        const panel = document.getElementById(ids.panel);
        const button = document.getElementById(ids.button);
        const active = name === key;
        if (panel) panel.classList.toggle("hidden", !active);
        if (button) {
            button.className =
                "workspace-tab relative px-4 py-2.5 text-xs font-semibold rounded-t-lg "
                + "border-b-2 transition-colors "
                + (active ? TAB_ACTIVE : TAB_IDLE);
        }
    }

    if (key === "console") {
        clearConsoleUnread();
        const box = document.getElementById("terminalLogBox");
        if (box) box.scrollTop = box.scrollHeight;
    }
    // Listings are derived from the store mirror, so refetch on entry rather
    // than showing whatever was true when the page loaded.
    if (key === "listings") fetchEbayListings();
    // Same for drafts: a plan is a diff against the catalogue, and the
    // catalogue may have moved since this page loaded.
    if (key === "drafts") fetchDraftPlan();
}

function clearConsoleUnread() {
    consoleUnread = 0;
    consoleUnreadHasError = false;
    const badge = document.getElementById("consoleUnreadBadge");
    if (badge) badge.classList.add("hidden");
}

function noteConsoleActivity(level) {
    if (activeWorkspaceTab === "console") return;
    consoleUnread += 1;
    if (level === "ERROR") consoleUnreadHasError = true;

    const badge = document.getElementById("consoleUnreadBadge");
    if (!badge) return;
    badge.innerText = consoleUnread > 99 ? "99+" : String(consoleUnread);
    badge.className =
        "ml-1.5 px-1.5 py-0.5 rounded-full text-[10px] font-bold font-mono align-middle "
        + (consoleUnreadHasError
            ? "bg-rose-900 text-rose-100"
            : "bg-slate-700 text-slate-200");
}

// -------------------------------------------------------------------
// DATABASE RESTORE
// -------------------------------------------------------------------

// Set only once a file has passed validation, so "Replace database" cannot be
// pressed against an unchecked file.
let restoreValidatedFile = null;

document.getElementById("btnDatabasePanel")?.addEventListener("click", openDatabaseModal);
document.getElementById("btnEbayPanel")?.addEventListener("click", openEbayModal);

// -------------------------------------------------------------------
// eBay account connection (admin only)
// -------------------------------------------------------------------

function openEbayModal() {
    document.getElementById("accountMenu")?.classList.add("hidden");
    document.getElementById("ebayModal").classList.remove("hidden");
    refreshEbayStatus();
}

function closeEbayModal() {
    document.getElementById("ebayModal").classList.add("hidden");
}

async function refreshEbayStatus() {
    const box = document.getElementById("ebayStatusBox");
    const connect = document.getElementById("btnEbayConnect");
    const disconnect = document.getElementById("btnEbayDisconnect");
    if (!box) return;

    try {
        const res = await fetch("/api/ebay/status");
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not read eBay status");

        if (!data.available) {
            box.innerHTML = `<p class="text-amber-300">The eBay client library is not installed in this deployment, so every eBay feature is disabled. The rest of the app is unaffected.</p>`;
            connect.disabled = true;
            disconnect.classList.add("hidden");
            return;
        }
        if (!data.configured) {
            box.innerHTML = `
                <p class="text-amber-300 font-semibold">Not configured</p>
                <p class="text-slate-400">Set <span class="font-mono">EBAY_CLIENT_ID</span>, <span class="font-mono">EBAY_CLIENT_SECRET</span> and <span class="font-mono">EBAY_REDIRECT_URI</span> in <span class="font-mono">.env</span>, then restart the container.</p>`;
            connect.disabled = true;
            disconnect.classList.add("hidden");
            return;
        }

        // A credential/environment mismatch can never authenticate, and
        // eBay's error for it blames the secret. Say it plainly instead.
        const misconfigured = data.misconfiguration
            ? `<p class="text-amber-300 mt-1">${escapeHtml(data.misconfiguration)}</p>`
            : "";

        // Module B's card points at the automated path once there is an
        // account to use it with, without hiding the upload.
        document
            .getElementById("syncAutomatedHint")
            ?.classList.toggle("hidden", !data.connected);

        connect.disabled = false;
        if (data.connected) {
            // The refresh token dies after about eighteen months and the only
            // cure is another consent screen, so show the date rather than
            // waiting for it to fail.
            const expiry = data.refresh_expires_at
                ? new Date(data.refresh_expires_at * 1000).toLocaleDateString()
                : "unknown";
            box.innerHTML = `
                <p class="text-emerald-400 font-semibold">Connected</p>
                <p class="text-slate-400">Environment: <span class="font-mono">${escapeHtml(data.environment || "-")}</span> &middot; Marketplace: <span class="font-mono">${escapeHtml(data.marketplace_id || "-")}</span></p>
                ${data.connected_by ? `<p class="text-slate-400">Authorised by ${escapeHtml(data.connected_by)}${data.connected_at ? ` on ${escapeHtml(String(data.connected_at).slice(0, 10))}` : ""}</p>` : ""}
                <p class="text-slate-500">Access must be renewed by ${escapeHtml(expiry)}.</p>`;
            connect.innerText = "Reconnect";
            disconnect.classList.remove("hidden");
            document.getElementById("ebaySyncGroup")?.classList.remove("hidden");
            document.getElementById("ebayPushSetupGroup")?.classList.remove("hidden");
        } else {
            box.innerHTML = `
                <p class="text-slate-300 font-semibold">Not connected</p>
                <p class="text-slate-400">Environment: <span class="font-mono">${escapeHtml(data.environment || "-")}</span></p>
                <p class="text-slate-500">Connecting opens eBay's consent screen in a new tab.</p>
                ${misconfigured}`;
            connect.innerText = "Connect eBay account";
            disconnect.classList.add("hidden");
            document.getElementById("ebaySyncGroup")?.classList.add("hidden");
            document.getElementById("ebayPushSetupGroup")?.classList.add("hidden");
        }
    } catch (err) {
        box.innerHTML = `<p class="text-rose-400">${escapeHtml(err.message)}</p>`;
    }
}

async function connectEbayAccount() {
    const button = document.getElementById("btnEbayConnect");
    if (button) button.disabled = true;
    try {
        const res = await fetch("/api/ebay/connect", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not start the connection");
        // A new tab, so the dashboard is not lost if consent is abandoned.
        window.open(data.authorization_url, "_blank", "noopener");
        logToTerminal("INFO", "Opened eBay's consent screen in a new tab");
    } catch (err) {
        logToTerminal("ERROR", `eBay connect failed: ${err.message}`);
    } finally {
        if (button) button.disabled = false;
    }
}

async function syncFromEbay() {
    const button = document.getElementById("btnEbaySync");
    const box = document.getElementById("ebaySyncResult");
    if (button) {
        button.disabled = true;
        button.innerText = "Waiting for eBay…";
    }
    if (box) {
        box.classList.remove("hidden");
        box.innerHTML = `<p class="text-slate-400">eBay generates the report on its own schedule, so this can take a minute.</p>`;
    }
    try {
        const res = await fetch("/api/ebay/sync", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "The sync failed");

        for (const entry of data.logs || []) {
            logToTerminal(entry.level, entry.message);
        }
        const report = data.report || {};
        if (box) {
            // The headers are shown because the Feed report's exact column
            // names have not been seen against a real store yet; if nothing
            // synced, they are the first thing worth looking at.
            box.innerHTML = `
                <p class="${data.synced_count ? "text-emerald-400" : "text-amber-300"} font-semibold">
                    ${data.synced_count} variation(s) synced across ${data.linked_listing_count || 0} listing(s)
                </p>
                ${data.delisting_skipped ? `<p class="text-rose-400">Nothing matched, so no quantity was set to 0. The report's columns were probably not understood &mdash; your mirror is untouched. Compare the columns below against what the parser expects.</p>` : ""}
                ${data.delisted_count ? `<p class="text-amber-300">${data.delisted_count} card(s) no longer on eBay set to 0</p>` : ""}
                ${data.skipped_unmapped_count ? `<p class="text-amber-300">${data.skipped_unmapped_count} label(s) not in the catalogue &mdash; worth investigating</p>` : ""}
                <p class="text-slate-500">Report ${escapeHtml(report.status || "?")}, ${report.row_count ?? "?"} row(s), ${report.bytes ?? "?"} bytes</p>
                ${!data.synced_count && report.headers ? `<p class="text-slate-500">Columns seen: <span class="font-mono">${escapeHtml((report.headers || []).join(", "))}</span></p>` : ""}`;
        }
        if (data.delisting_skipped) {
            logToTerminal(
                "ERROR",
                "eBay sync matched nothing and refused to zero any quantity. "
                + "Treat this as a failed sync, not an empty store."
            );
        }
        // The mirror feeds both of these, so neither should show stale numbers.
        fetchEbayListings();
        fetchStats();
    } catch (err) {
        logToTerminal("ERROR", `eBay sync failed: ${err.message}`);
        if (box) box.innerHTML = `<p class="text-rose-400">${escapeHtml(err.message)}</p>`;
    } finally {
        if (button) {
            button.disabled = false;
            button.innerText = "Sync from eBay";
        }
    }
}

async function disconnectEbayAccount() {
    if (!confirm("Disconnect the eBay account? Reconnecting requires the consent screen again.")) {
        return;
    }
    try {
        const res = await fetch("/api/ebay/disconnect", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not disconnect");
        logToTerminal("INFO", "eBay account disconnected");
        refreshEbayStatus();
    } catch (err) {
        logToTerminal("ERROR", `eBay disconnect failed: ${err.message}`);
    }
}

function openDatabaseModal() {
    restoreValidatedFile = null;
    document.getElementById("restoreFileInput").value = "";
    document.getElementById("restoreSummary").classList.add("hidden");
    document.getElementById("restoreError").classList.add("hidden");
    document.getElementById("btnRestoreApply").disabled = true;
    document.getElementById("databaseModal").classList.remove("hidden");
    syncModalScrollLock();
    fetchDatabaseFiles();
}

function formatBytes(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// The list comes from the server rather than being hardcoded here, so adding a
// database file in one place makes it downloadable without touching the UI.
async function fetchDatabaseFiles() {
    const target = document.getElementById("databaseFileList");
    if (!target) return;

    try {
        const res = await fetch("/api/database/files");
        if (!res.ok) throw new Error("Could not list the database files");
        const data = await res.json();

        target.innerHTML = (data.files || []).map(f => {
            const facts = Object.entries(f.summary || {})
                .map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`)
                .join(" &middot; ");
            return `
                <div class="flex items-start justify-between gap-3 p-2.5 rounded-lg bg-dark-800 border border-slate-700/70">
                    <div class="min-w-0">
                        <p class="font-semibold text-slate-200">${f.label}
                            <span class="font-mono font-normal text-slate-500 ml-1">${f.filename}</span>
                        </p>
                        <p class="text-[11px] text-slate-400 mt-0.5">${f.description}</p>
                        <p class="text-[10px] text-slate-500 mt-1 font-mono">${formatBytes(f.size_bytes)}${facts ? " &middot; " + facts : ""}</p>
                    </div>
                    <a href="/api/database/download/${encodeURIComponent(f.name)}" download
                       class="shrink-0 inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-100 font-semibold text-[11px] transition-all">
                        <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                        </svg>
                        <span>.db</span>
                    </a>
                </div>`;
        }).join("") || `<p class="text-[11px] text-slate-500">No database files found.</p>`;
    } catch (err) {
        target.innerHTML = `<p class="text-[11px] text-rose-300">${escapeHtml(err.message)}</p>`;
    }
}

function closeDatabaseModal() {
    document.getElementById("databaseModal").classList.add("hidden");
    syncModalScrollLock();
    restoreValidatedFile = null;
}

document.getElementById("restoreFileInput")?.addEventListener("change", () => {
    // A new file invalidates any previous check.
    restoreValidatedFile = null;
    document.getElementById("btnRestoreApply").disabled = true;
    document.getElementById("restoreSummary").classList.add("hidden");
    document.getElementById("restoreError").classList.add("hidden");
});

function showRestoreError(message) {
    const box = document.getElementById("restoreError");
    box.innerText = message;
    box.classList.remove("hidden");
    document.getElementById("restoreSummary").classList.add("hidden");
    document.getElementById("btnRestoreApply").disabled = true;
    restoreValidatedFile = null;
}

async function postRestore(file, confirm) {
    const form = new FormData();
    form.append("file", file);
    form.append("confirm", confirm ? "true" : "false");
    const res = await fetch("/api/inventory/database", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "The file was rejected");
    return data;
}

async function checkRestoreFile() {
    const input = document.getElementById("restoreFileInput");
    const file = input.files && input.files[0];
    if (!file) {
        showRestoreError("Choose a backup file first.");
        return;
    }

    try {
        const data = await postRestore(file, false);
        const inc = data.incoming || {};
        const cur = data.current || {};
        const row = (label, incoming, current) =>
            `<div class="flex items-center justify-between">
                <span class="text-slate-400">${label}</span>
                <span class="font-mono text-slate-200">${current} &rarr; <strong>${incoming}</strong></span>
             </div>`;

        document.getElementById("restoreSummary").innerHTML =
            `<p class="text-[11px] text-slate-300 font-semibold mb-1">Current &rarr; after restore</p>`
            + row("Catalog cards", inc.manifest ?? 0, cur.manifest ?? 0)
            + row("Linked eBay cards", inc.ebay_variations ?? 0, cur.ebay_variations ?? 0)
            + `<div class="flex items-center justify-between"><span class="text-slate-400">Pricing rules</span><span class="font-mono text-slate-200">${inc.pricing_rules ?? 0}</span></div>`
            + `<div class="flex items-center justify-between"><span class="text-slate-400">Listing settings</span><span class="font-mono text-slate-200">${inc.listing_settings ?? 0}</span></div>`;
        document.getElementById("restoreSummary").classList.remove("hidden");
        document.getElementById("restoreError").classList.add("hidden");

        restoreValidatedFile = file;
        document.getElementById("btnRestoreApply").disabled = false;
        logToTerminal("INFO", `Backup file "${file.name}" validated. Nothing has been replaced yet.`);
    } catch (err) {
        showRestoreError(err.message);
    }
}

async function applyRestore() {
    if (!restoreValidatedFile) return;
    if (!confirm("Replace the inventory database for all users? Your current database will be copied aside first.")) {
        return;
    }

    try {
        const data = await postRestore(restoreValidatedFile, true);
        closeDatabaseModal();
        logToTerminal("SUCCESS",
            `Inventory database replaced from "${data.filename}".`);
        if (data.backup_path) {
            logToTerminal("INFO", `Previous database kept at ${data.backup_path}`);
        }
        fetchStats();
        fetchSetFilter();
        fetchInventory();
        fetchEbayListings();
    } catch (err) {
        showRestoreError(err.message);
    }
}

// Re-send what a live listing is made of. Pictures, item specifics, the title
// and the description live on the inventory items rather than on a plan, so
// once a listing is up there is no route to them through the drafts page -- a
// plan is a diff of quantities and prices, and a picture change produces no
// diff at all. Deliberately cannot move stock or price.
async function refreshListingContents(itemId, button) {
    if (!confirm(`Re-send listing #${itemId}'s pictures, item specifics, title and description to eBay?\n\nStock and price are not touched.`)) {
        return;
    }
    if (button) { button.disabled = true; button.textContent = "Sending…"; }
    try {
        const res = await fetch(`/api/ebay-listings/${encodeURIComponent(itemId)}/refresh`, {
            method: "POST",
        });
        const data = await readJsonResponse(res);
        if (data === null) {
            logToTerminal("WARN", (
                `The connection dropped while listing #${itemId} was being refreshed `
                + `(a ${res.status} page came back instead of a result). The refresh is `
                + "probably still running; check the listing on eBay in a minute."
            ));
            return;
        }
        if (!res.ok) throw new Error(data.detail || "Could not refresh the listing");
        (data.logs || []).forEach(e => logToTerminal(e.level, e.message));
        logToTerminal(data.failed ? "WARN" : "SUCCESS",
            `Listing #${itemId}: ${data.refreshed} variation(s) refreshed`);
    } catch (err) {
        logToTerminal("ERROR", `Refresh failed: ${err.message}`);
    } finally {
        if (button) { button.disabled = false; button.textContent = "Refresh"; }
    }
}

// -------------------------------------------------------------------
// COVER PHOTO
// -------------------------------------------------------------------

let coverEditItemId = null;

function openCoverModal(itemId) {
    const listing = lastEbayListings.find(l => l.ebay_parent_id === itemId);
    coverEditItemId = itemId;

    document.getElementById("coverModalListing").innerText = listing
        ? `eBay #${itemId} - ${listing.set_name || "?"}, ${listing.card_count} card(s)`
        : `eBay #${itemId}`;

    // eBay does not merge pictures: the set sent replaces what is there.
    // Every listing is API-managed now, so saving applies immediately -- but
    // a listing the API cannot see would silently not update, so say which.
    const managed = !!(listing && listing.managed);
    const how = document.getElementById("coverModalHowItApplies");
    const submit = document.getElementById("coverModalSubmit");
    if (how) {
        how.innerHTML = managed
            ? "eBay does not merge pictures — the set sent replaces what is there. Saving applies the change to the live listing immediately."
            : "This listing is not visible to the eBay API, so the choice can be saved but not applied. Sync from eBay first; if it stays unmanaged, the listing was created outside this application.";
    }
    if (submit) {
        submit.textContent = managed ? "Save & apply to eBay" : "Save anyway";
    }
    document.getElementById("coverUrlInput").value =
        listing ? (listing.cover_image_url || "") : "";
    updateCoverPreview();

    document.getElementById("coverModal").classList.remove("hidden");
    syncModalScrollLock();
    document.getElementById("coverUrlInput").focus();
}

function closeCoverModal() {
    document.getElementById("coverModal").classList.add("hidden");
    syncModalScrollLock();
    coverEditItemId = null;
}

// Loading the URL in the browser is a cheap sanity check: if it will not render
// here, eBay is unlikely to be able to fetch it either.
function updateCoverPreview() {
    const url = document.getElementById("coverUrlInput").value.trim();
    const wrap = document.getElementById("coverPreviewWrap");
    const img = document.getElementById("coverPreview");
    const err = document.getElementById("coverPreviewError");

    if (!/^https?:\/\//i.test(url)) {
        wrap.classList.add("hidden");
        return;
    }
    err.classList.add("hidden");
    img.style.display = "";
    img.onerror = () => {
        img.style.display = "none";
        err.classList.remove("hidden");
    };
    img.src = url;
    wrap.classList.remove("hidden");
}

document.getElementById("coverUrlInput")?.addEventListener("input", updateCoverPreview);

async function saveCoverPhoto(e) {
    if (e && e.preventDefault) e.preventDefault();
    if (!coverEditItemId) return;

    const url = document.getElementById("coverUrlInput").value.trim();
    try {
        const res = await fetch(`/api/ebay-listings/${encodeURIComponent(coverEditItemId)}/cover`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cover_image_url: url })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to save the cover photo");

        const id = coverEditItemId;
        closeCoverModal();

        // The endpoint applies it when it can and says why when it cannot.
        if (data.applied) {
            logToTerminal("SUCCESS",
                `Cover photo applied to eBay #${id} (${data.refreshed} variation(s) re-sent).`);
        } else {
            logToTerminal("SUCCESS", `Cover photo recorded for eBay #${id}.`);
            logToTerminal("WARN", data.reason
                || `It has not been applied to the live listing yet. Press Refresh on #${id}.`);
        }

        fetchEbayListings();
    } catch (err) {
        alert(err.message);
    }
}

// -------------------------------------------------------------------
// EBAY LISTINGS VIEW
// -------------------------------------------------------------------

// The cover photo dialog needs the listing it was opened from.
let lastEbayListings = [];

async function fetchEbayListings() {
    const tbody = document.getElementById("ebayListingsTableBody");
    const summary = document.getElementById("ebayListingsSummary");
    if (!tbody) return;
    // Cover photos here are hover targets too, and this table has its own
    // render path. initCardPreview is idempotent.
    initCardPreview();
    hideCardPreview();

    try {
        const res = await fetch("/api/ebay-listings");
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to load listings");

        const listings = data.listings || [];
        lastEbayListings = listings;
        if (!listings.length) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="8" class="py-8 text-center text-slate-500">
                        No linked eBay listings yet. Upload an Active Listings report
                        to Module B to link your catalog to live listings.
                    </td>
                </tr>`;
            if (summary) summary.innerText = "0 listings";
            return;
        }

        tbody.innerHTML = listings.map(l => {
            const drifted = l.catalog_quantity !== l.live_quantity;
            const liveBadge = drifted
                ? "bg-amber-950/80 text-amber-300 border border-amber-800"
                : "bg-emerald-950/80 text-emerald-400 border border-emerald-800";
            const driftTitle = drifted
                ? `Catalogued ${l.catalog_quantity}, eBay reports ${l.live_quantity}.`
                : "Catalogued quantity matches eBay.";

            // A listing should hold one set and one condition; say so when it
            // does not, rather than silently showing only the first.
            const setLabel = l.set_count > 1
                ? `<span class="text-amber-300" title="This listing spans ${l.set_count} sets">${escapeHtml(l.set_name)} +${l.set_count - 1} more</span>`
                : escapeHtml(l.set_name || "-");
            const condLabel = l.condition_count > 1
                ? `<span class="text-amber-300" title="This listing spans ${l.condition_count} conditions">${escapeHtml(l.condition)} +${l.condition_count - 1} more</span>`
                : escapeHtml(l.condition || "-");

            return `
                <tr class="hover:bg-dark-800/80 transition-colors">
                    <td class="py-3 px-4 font-mono">
                        <a href="${EBAY_ITEM_URL}${encodeURIComponent(l.ebay_parent_id)}" target="_blank" rel="noopener noreferrer" class="text-accent-cyan hover:underline" title="Open the eBay listing">${escapeHtml(l.ebay_parent_id)}</a>
                    </td>
                    <td class="py-3 px-4 text-slate-400">${setLabel}</td>
                    <td class="py-3 px-4">${condLabel}</td>
                    <td class="py-3 px-4 text-center font-mono text-slate-300">${l.card_count}</td>
                    <td class="py-3 px-4 text-center" title="${escapeHtml(driftTitle)}">
                        <span class="inline-block min-w-[28px] px-2 py-0.5 rounded-full text-[11px] font-bold font-mono bg-slate-900 text-slate-400 border border-slate-800">${l.catalog_quantity}</span>
                    </td>
                    <td class="py-3 px-4 text-center" title="${escapeHtml(driftTitle)}">
                        <span class="inline-block min-w-[28px] px-2 py-0.5 rounded-full text-[11px] font-bold font-mono ${liveBadge}">${l.live_quantity}</span>
                    </td>
                    <td class="py-3 px-4">
                        <button onclick="openCoverModal('${escapeHtml(l.ebay_parent_id)}')" class="flex items-center gap-2 text-left group/cover"
                            ${l.cover_image_url ? `data-card-image="${escapeHtml(l.cover_image_url)}" data-preview-kind="cover"` : ""}
                            title="${l.cover_image_url ? escapeHtml(l.cover_image_url) : "No cover photo recorded. Click to set one."}">
                            ${l.cover_image_url
                                ? `<img src="${escapeHtml(l.cover_image_url)}" alt="" class="w-8 h-8 rounded object-cover border border-slate-700 bg-dark-900" onerror="this.style.display='none'">`
                                : `<span class="w-8 h-8 rounded border border-dashed border-slate-700 flex items-center justify-center text-slate-600 text-[10px]">?</span>`}
                            <span class="text-[11px] ${l.cover_image_url ? "text-slate-400" : "text-slate-600 italic"} group-hover/cover:text-accent-cyan underline decoration-dotted">
                                ${l.cover_image_url ? "Change" : "Set cover"}
                            </span>
                        </button>
                    </td>
                    <td class="py-3 px-4 text-slate-500 text-[11px] font-mono">${escapeHtml(l.last_synced || "-")}</td>
                    <td class="py-3 px-4">
                        ${l.managed
                            ? `<button type="button" onclick="refreshListingContents('${escapeHtml(l.ebay_parent_id)}', this)"
                                title="Re-send this listing's pictures, item specifics, title and description. Cannot change stock or price."
                                class="text-[10px] font-semibold px-2 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition-all disabled:opacity-40">
                                Refresh
                              </button>`
                            : `<span class="text-[10px] text-amber-500/80" title="The Inventory API cannot see this listing, so nothing here can change it. Sync from eBay; if it persists, the listing was created outside this application.">unmanaged</span>`}
                    </td>
                </tr>`;
        }).join("");

        const cards = listings.reduce((n, l) => n + l.card_count, 0);
        const live = listings.reduce((n, l) => n + l.live_quantity, 0);
        if (summary) {
            summary.innerText =
                `${listings.length} listing(s), ${cards} linked card(s), ${live} unit(s) live on eBay`;
        }
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="8" class="py-6 text-center text-rose-400">Failed to load listings: ${escapeHtml(err.message)}</td></tr>`;
    }
}

// -------------------------------------------------------------------
// 3b. DRAFTS: staging for every eBay-bound change
// -------------------------------------------------------------------

// The draft currently on screen. Held so an edit can re-render without a
// second round trip for data the PATCH response already returned.
let currentDraftPlan = null;

// Which listings the user has collapsed, by group key. Kept outside the
// rendered markup because every edit on this page rebuilds the whole draft
// from scratch, which discards the open/closed state of each <details>.
// Storing the collapsed set rather than the open one means a listing that
// appears for the first time is open, which is the right default for a page
// whose purpose is review.
const collapsedDraftGroups = new Set();

// Capture phase, because a <details> toggle event does not bubble: a listener
// on document would never see it otherwise. Capturing also survives the
// element being replaced on the next render, which delegation is for.
document.addEventListener("toggle", (event) => {
    const details = event.target;
    if (!details || details.tagName !== "DETAILS") return;
    const key = details.getAttribute("data-group-key");
    if (key === null) return;
    if (details.open) {
        collapsedDraftGroups.delete(key);
    } else {
        collapsedDraftGroups.add(key);
    }
}, true);

const DRAFT_ACTION_LABELS = {
    create_listing: { label: "New listing", cls: "bg-emerald-950/80 text-emerald-400 border-emerald-800" },
    update: { label: "Update", cls: "bg-sky-950/80 text-sky-300 border-sky-800" },
    zero_out: { label: "Sold out → 0", cls: "bg-amber-950/80 text-amber-300 border-amber-800" },
    remove_from_group: { label: "Remove", cls: "bg-rose-950/80 text-rose-300 border-rose-800" },
    end_listing: { label: "End listing", cls: "bg-rose-950/80 text-rose-300 border-rose-800" },
};

function draftActionBadge(action) {
    const meta = DRAFT_ACTION_LABELS[action]
        || { label: action, cls: "bg-slate-900 text-slate-400 border-slate-800" };
    return `<span class="inline-block px-2 py-0.5 rounded-full text-[10px] font-bold border ${meta.cls}">${escapeHtml(meta.label)}</span>`;
}

function isSingleGroup(groupKey) {
    return typeof groupKey === "string" && groupKey.startsWith("single:");
}

function draftGroupTitle(group, items) {
    if (isSingleGroup(group.group_key)) {
        const first = items[0];
        return first
            ? `${escapeHtml(first.product_name)} — single listing`
            : "Single listing";
    }
    const set = group.set_name || "(no set)";
    const condition = group.condition || "(no condition)";
    return `${escapeHtml(set)} · ${escapeHtml(condition)}`;
}

async function fetchDraftPlan() {
    const container = document.getElementById("draftGroups");
    if (!container) return;

    try {
        const res = await fetch("/api/plans");
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to load drafts");

        const plans = data.plans || [];
        // Rendered before the branch, so approved plans and their files stay
        // on screen whether or not a draft is open. Rendering it inside the
        // draft footer was the original mistake: approving moves the plan out
        // of draft, and the no-open-draft state then hides the footer -- so
        // the files appeared and vanished in the same instant.
        renderPlanHistory(plans);
        // A push started before a tab switch is still running on the server.
        resumePushJob();

        const draft = plans.find(p => p.status === "draft");
        if (!draft) {
            currentDraftPlan = null;
            renderNoDraft(plans);
            return;
        }

        const detailRes = await fetch(`/api/plans/${draft.id}`);
        const detail = await detailRes.json();
        if (!detailRes.ok) throw new Error(detail.detail || "Failed to load draft");

        currentDraftPlan = detail;
        renderDraftPlan(detail);
    } catch (err) {
        container.innerHTML = `<div class="glass-card rounded-2xl border border-rose-900/60 bg-rose-950/20 p-6 text-center text-rose-400 text-xs">Failed to load drafts: ${escapeHtml(err.message)}</div>`;
    }
}

function renderNoDraft(plans) {
    const container = document.getElementById("draftGroups");
    const footer = document.getElementById("draftFooter");
    const discard = document.getElementById("btnDiscardPlan");
    const blockers = document.getElementById("draftBlockers");
    if (footer) footer.classList.add("hidden");
    if (discard) discard.classList.add("hidden");
    if (blockers) blockers.classList.add("hidden");
    setDraftsBadge(0);

    const approved = plans.filter(p => p.status !== "draft").length;
    container.innerHTML = `
        <div class="glass-card rounded-2xl border border-slate-800 bg-dark-800/40 p-8 text-center space-y-2">
            <p class="text-sm text-slate-300 font-semibold">No open draft</p>
            <p class="text-xs text-slate-500 max-w-md mx-auto">
                A draft is the difference between what your catalogue says and what eBay is
                known to hold. Rebuild one after a batch upload, a price refresh or a manual
                edit. An empty draft means nothing needs changing.
            </p>
            ${approved ? `<p class="text-[11px] text-slate-600">${approved} approved plan(s) below, with the files they authorised.</p>` : ""}
        </div>`;
}

function renderDraftPlan(detail) {
    const container = document.getElementById("draftGroups");
    // Same two calls the inventory renderer makes: arm the (idempotent)
    // delegation, and drop any preview belonging to a row about to be
    // replaced.
    initCardPreview();
    hideCardPreview();
    const footer = document.getElementById("draftFooter");
    const discard = document.getElementById("btnDiscardPlan");
    const items = detail.items || [];
    const groups = detail.groups || [];

    if (discard) discard.classList.remove("hidden");
    renderDraftBlockers(detail.blockers || []);

    const included = items.filter(i => i.status !== "excluded");
    setDraftsBadge(included.length);

    if (!items.length) {
        container.innerHTML = `
            <div class="glass-card rounded-2xl border border-slate-800 bg-dark-800/40 p-8 text-center space-y-2">
                <p class="text-sm text-slate-300 font-semibold">Nothing to change</p>
                <p class="text-xs text-slate-500 max-w-md mx-auto">
                    Every catalogued card already matches what eBay is known to hold. An empty
                    draft is the correct result of a dump that changed nothing.
                </p>
            </div>`;
        if (footer) footer.classList.add("hidden");
        return;
    }

    // Every group in the plan, offered as a move target on each row. Built
    // once rather than per row: a plan can hold thousands of items.
    const moveTargets = groups
        .filter(g => !isSingleGroup(g.group_key))
        .map(g => ({ key: g.group_key, label: `${g.set_name || "(no set)"} · ${g.condition || "-"}` }));

    const byGroup = new Map();
    for (const item of items) {
        const key = item.group_key || "";
        if (!byGroup.has(key)) byGroup.set(key, []);
        byGroup.get(key).push(item);
    }

    container.innerHTML = groups.map(group => {
        const groupItems = byGroup.get(group.group_key) || [];
        const invalid = groupItems.some(
            i => i.status !== "excluded" && i.validation && i.validation !== "[]"
        );
        const border = invalid ? "border-amber-800/70" : "border-slate-800";
        // Open by default: a draft is for reviewing, so hiding the rows would
        // defeat the point. Collapsing matters because one listing can hold a
        // hundred cards and scrolling past it to reach the next is the common
        // case once you have checked it.
        //
        // Which listings are collapsed is remembered across re-renders. Every
        // edit on this page -- a quantity, a cover photo, an exclusion --
        // refetches and rebuilds the whole draft, and rebuilding from markup
        // loses the open/closed state of every <details>. Without this,
        // setting a cover photo re-expands the six listings you had just
        // collapsed to get it out of the way.
        const open = collapsedDraftGroups.has(group.group_key) ? "" : "open";
        return `
            <details ${open} data-group-key="${escapeHtml(group.group_key)}" class="group glass-card rounded-2xl border ${border} bg-dark-800/40 overflow-hidden">
                <summary class="cursor-pointer list-none px-4 py-3 border-b border-slate-800/80 bg-dark-800/60 hover:bg-dark-800/90 transition-colors flex items-center gap-3">
                    <svg class="caret w-4 h-4 shrink-0 text-slate-400 transition-transform" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7" />
                    </svg>
                    <div class="min-w-0 flex-1">
                        <p class="text-xs font-bold text-white truncate">${draftGroupTitle(group, groupItems)}</p>
                        <p class="text-[11px] text-slate-500">
                            ${group.item_count} card(s)${group.excluded_count ? `, ${group.excluded_count} left out` : ""}
                            &middot; ${group.proposed_copies} cop${group.proposed_copies === 1 ? "y" : "ies"} proposed
                        </p>
                    </div>
                    ${draftCoverControl(group)}
                    ${invalid ? `<span class="shrink-0 text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-950/80 text-amber-300 border border-amber-800">Needs attention</span>` : ""}
                </summary>
                <div class="overflow-x-auto">
                    <table class="w-full text-left border-collapse text-xs">
                        <thead>
                            <tr class="border-b border-slate-800 text-slate-400 uppercase font-semibold text-[10px] tracking-wider">
                                <th class="py-2 px-4">Card</th>
                                <th class="py-2 px-4" title="Hover a thumbnail to see the card full size.">Image</th>
                                <th class="py-2 px-4">Change</th>
                                <th class="py-2 px-4 text-center">Quantity</th>
                                <th class="py-2 px-4 text-center">Price</th>
                                <th class="py-2 px-4">Listing</th>
                                <th class="py-2 px-4 text-right">Include</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-800/60 text-slate-300">
                            ${groupItems.map(i => draftItemRow(i, moveTargets)).join("")}
                        </tbody>
                    </table>
                </div>
            </details>`;
    }).join("");

    if (footer) footer.classList.remove("hidden");
    const summary = document.getElementById("draftSummary");
    if (summary) {
        const copies = included.reduce((n, i) => n + (i.proposed_qty || 0), 0);
        summary.innerText =
            `${included.length} change(s) across ${groups.length} listing(s), `
            + `${copies} cop${copies === 1 ? "y" : "ies"} in total`
            + (items.length - included.length ? `; ${items.length - included.length} left out` : "");
    }

    const approve = document.getElementById("btnApprovePlan");
    if (approve) {
        const blocked = (detail.blockers || []).length > 0;
        approve.disabled = blocked || !included.length;
        approve.title = blocked
            ? "Fix or leave out the flagged cards first"
            : (included.length ? "Approve this draft" : "Every card is left out");
    }
}

// The listing's cover photo, editable from the group header.
//
// A cover belongs to the listing rather than to any card in it, so it lives on
// the plan's group. The control shows the staged choice if one has been made,
// otherwise whatever the live listing already carries -- so an untouched
// listing shows its real picture rather than an empty frame, and a change is
// visibly a change.
//
// Not a hover-preview target: the header is a <summary>, so pointing at it is
// already how you collapse the section, and stacking a preview on that reads
// as a misclick waiting to happen.
function draftCoverControl(group) {
    const staged = group.cover_is_staged;
    const url = group.cover_image_url || "";
    const isNewListing = !group.ebay_parent_id;
    const title = isNewListing
        ? "This listing does not exist yet, so the cover applies when it is created."
        : "Revising the cover replaces the listing's whole picture set on eBay.";

    return `
        <button type="button" data-group-key="${escapeHtml(group.group_key)}"
            onclick="event.preventDefault();event.stopPropagation();openDraftCoverPrompt(this)"
            class="shrink-0 flex items-center gap-2 px-2 py-1 rounded-lg border ${staged ? "border-brand-500 bg-brand-600/15" : "border-slate-700 bg-dark-900/60"} hover:border-accent-cyan transition-colors"
            ${url ? `data-card-image="${escapeHtml(url)}" data-preview-kind="cover"` : ""}
            title="${escapeHtml(title)}">
            ${url
                ? `<img src="${escapeHtml(url)}" alt="" class="block h-8 w-auto rounded border border-slate-700 bg-dark-900" onerror="this.style.visibility='hidden'">`
                : `<span class="block w-6 h-8 rounded border border-dashed border-slate-700"></span>`}
            <span class="text-[10px] font-semibold ${staged ? "text-brand-400" : "text-slate-400"}">
                ${staged ? "Cover changed" : (url ? "Cover" : "Set cover")}
            </span>
        </button>`;
}

// Takes the button, not the key. A group key is built from a set name out of
// an uploaded CSV, and interpolating it into an inline handler was broken: the
// quotes JSON.stringify emits closed the onclick attribute early, so the
// button silently did nothing. Reading it from a data attribute is the same
// rule the move-target selects already follow.
async function openDraftCoverPrompt(button) {
    const groupKey = typeof button === "string"
        ? button
        : (button && button.getAttribute("data-group-key")) || "";
    if (!groupKey) return;
    if (!currentDraftPlan || !currentDraftPlan.plan) return;
    const group = (currentDraftPlan.groups || []).find(g => g.group_key === groupKey);
    // Seeded with the staged or current cover, falling back to a card's own
    // picture -- which is usually what you want for a brand-new listing and
    // saves pasting a URL by hand.
    const seed = (group && (group.cover_image_url || group.first_card_image)) || "";
    const entered = prompt(
        "Cover photo URL for this listing." + "\n\n"
            + "eBay replaces the listing's whole picture set when this is revised." + "\n"
            + "Leave empty to keep whatever the listing already has.",
        seed
    );
    if (entered === null) return;

    try {
        const res = await fetch(`/api/plans/${currentDraftPlan.plan.id}/cover`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ group_key: groupKey, cover_image_url: entered.trim() }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not set the cover photo");
        logToTerminal(
            "INFO",
            entered.trim()
                ? `[DRAFTS] Cover staged for ${groupKey}`
                : `[DRAFTS] Cover choice cleared for ${groupKey}`
        );
        await fetchDraftPlan();
    } catch (err) {
        logToTerminal("ERROR", `[DRAFTS] Cover photo failed: ${err.message}`);
    }
}

function draftItemRow(item, moveTargets) {
    const excluded = item.status === "excluded";
    const problems = item.validation ? JSON.parse(item.validation) : [];
    const rowCls = excluded
        ? "opacity-40"
        : (problems.length ? "bg-amber-950/10" : "hover:bg-dark-800/80");

    const from = (before, after, money) => {
        if (before === null || before === undefined) {
            return `<span class="text-slate-500 text-[10px]" title="eBay's value is unknown until a sync has run, so this counts as a change">new</span>`;
        }
        const fmt = v => (money ? `$${Number(v).toFixed(2)}` : v);
        return before === after
            ? ""
            : `<span class="text-slate-500 text-[10px] line-through">${escapeHtml(String(fmt(before)))}</span>`;
    };

    // The listing selector carries group keys as option values rather than
    // interpolating them into an inline handler: a group key is built from a
    // set name that came out of an uploaded CSV.
    const options = [
        `<option value="single:${escapeHtml(item.manifest_id)}"${isSingleGroup(item.group_key) ? " selected" : ""}>Own single listing</option>`,
        ...moveTargets.map(t =>
            `<option value="${escapeHtml(t.key)}"${t.key === item.group_key ? " selected" : ""}>${escapeHtml(t.label)}</option>`
        ),
    ].join("");

    return `
        <tr class="${rowCls} transition-colors">
            <td class="py-2.5 px-4">
                <p class="font-semibold text-slate-200">${escapeHtml(item.product_name || "-")}</p>
                <p class="text-[10px] text-slate-500 font-mono">${escapeHtml(item.manifest_id)}${item.card_number ? ` · #${escapeHtml(item.card_number)}` : ""}</p>
                ${problems.length ? `<ul class="mt-1 space-y-0.5">${problems.map(p => `<li class="text-[10px] text-amber-300">⚠ ${escapeHtml(p)}</li>`).join("")}</ul>` : ""}
            </td>
            ${cardThumbnailCell(item, "py-2 px-4")}
            <td class="py-2.5 px-4">${draftActionBadge(item.action)}</td>
            <td class="py-2.5 px-4 text-center whitespace-nowrap">
                ${from(item.observed_qty, item.proposed_qty, false)}
                <input type="number" min="0" value="${item.proposed_qty === null ? "" : item.proposed_qty}"
                    ${excluded ? "disabled" : ""}
                    onchange="updateDraftItem(${item.id}, { proposed_qty: parseInt(this.value, 10) })"
                    class="w-16 text-center px-1.5 py-1 rounded-lg bg-dark-900 border border-slate-700 text-slate-100 text-xs focus:outline-none focus:border-brand-500 disabled:opacity-50">
            </td>
            <td class="py-2.5 px-4 text-center whitespace-nowrap">
                ${from(item.observed_price, item.proposed_price, true)}
                <input type="number" min="0" step="0.01" value="${item.proposed_price === null ? "" : Number(item.proposed_price).toFixed(2)}"
                    ${excluded ? "disabled" : ""}
                    onchange="updateDraftItem(${item.id}, { proposed_price: parseFloat(this.value) })"
                    class="w-20 text-center px-1.5 py-1 rounded-lg bg-dark-900 border border-slate-700 text-slate-100 text-xs focus:outline-none focus:border-brand-500 disabled:opacity-50">
            </td>
            <td class="py-2.5 px-4">
                <select ${excluded ? "disabled" : ""}
                    onchange="updateDraftItem(${item.id}, { group_key: this.value })"
                    class="max-w-[14rem] truncate px-2 py-1 rounded-lg bg-dark-900 border border-slate-700 text-slate-200 text-[11px] focus:outline-none focus:border-brand-500 disabled:opacity-50"
                    title="Move this card into another listing, or give it one of its own">
                    ${options}
                </select>
            </td>
            <td class="py-2.5 px-4 text-right">
                <button onclick="updateDraftItem(${item.id}, { status: '${excluded ? "pending" : "excluded"}' })"
                    class="text-[11px] font-semibold px-2 py-1 rounded-lg border transition-all ${excluded
                        ? "bg-slate-800 border-slate-700 text-slate-400 hover:text-slate-200"
                        : "bg-dark-900 border-slate-700 text-slate-400 hover:text-rose-300"}"
                    title="${excluded ? "Put this card back into the draft" : "Leave this card out of the push"}">
                    ${excluded ? "Left out" : "Included"}
                </button>
            </td>
        </tr>`;
}

function renderDraftBlockers(blockers) {
    const box = document.getElementById("draftBlockers");
    const list = document.getElementById("draftBlockersList");
    const heading = document.getElementById("draftBlockersHeading");
    if (!box || !list) return;

    if (!blockers.length) {
        box.classList.add("hidden");
        return;
    }
    const total = blockers.reduce((n, b) => n + b.problems.length, 0);
    if (heading) {
        heading.innerText =
            `${total} problem(s) across ${blockers.length} listing(s) must be fixed or left out`;
    }
    list.innerHTML = blockers.map(b => {
        const name = isSingleGroup(b.group_key)
            ? "Single listing"
            : `${escapeHtml(b.set_name || "(no set)")} · ${escapeHtml(b.condition || "-")}`;
        return `
            <div>
                <span class="font-semibold text-amber-200">${name}</span>
                <ul class="ml-4 list-disc">
                    ${b.problems.map(p => `<li>${p.product_name ? `<span class="font-mono text-[10px]">${escapeHtml(p.manifest_id)}</span> ${escapeHtml(p.product_name)}: ` : ""}${escapeHtml(p.problem)}</li>`).join("")}
                </ul>
            </div>`;
    }).join("");
    box.classList.remove("hidden");
}

function setDraftsBadge(count) {
    const badge = document.getElementById("draftsCountBadge");
    if (!badge) return;
    if (!count) {
        badge.classList.add("hidden");
        return;
    }
    badge.innerText = count > 999 ? "999+" : String(count);
    badge.classList.remove("hidden");
}

async function buildDraftPlan() {
    const button = document.getElementById("btnBuildPlan");
    const container = document.getElementById("draftGroups");
    if (button) button.disabled = true;
    if (container) {
        container.innerHTML = `<div class="glass-card rounded-2xl border border-slate-800 bg-dark-800/40 p-8 text-center text-slate-500 text-xs">Comparing the catalogue against eBay&hellip;</div>`;
    }
    try {
        const res = await fetch("/api/plans/build", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ source: "manual" }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not build a draft");
        logToTerminal(
            "SUCCESS",
            `Draft ${data.plan_id}: ${data.item_count} change(s) across ${data.group_count} listing(s)`
                + (data.invalid_count ? `, ${data.invalid_count} needing attention` : "")
        );
        await fetchDraftPlan();
    } catch (err) {
        logToTerminal("ERROR", `Draft build failed: ${err.message}`);
        await fetchDraftPlan();
    } finally {
        if (button) button.disabled = false;
    }
}

async function updateDraftItem(itemId, changes) {
    try {
        const res = await fetch(`/api/plans/items/${itemId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(changes),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not apply that change");
        // Refetched rather than patched in place: a regrouping changes which
        // listings exist and therefore what every other row may move into.
        await fetchDraftPlan();
    } catch (err) {
        logToTerminal("ERROR", `Draft edit failed: ${err.message}`);
        await fetchDraftPlan();
    }
}

async function approveDraftPlan() {
    if (!currentDraftPlan || !currentDraftPlan.plan) return;
    const planId = currentDraftPlan.plan.id;
    const button = document.getElementById("btnApprovePlan");
    if (button) button.disabled = true;
    try {
        const res = await fetch(`/api/plans/${planId}/approve`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not approve this draft");
        logToTerminal(
            "SUCCESS",
            `Draft ${planId} approved: ${data.approved_items} change(s) cleared to push`
        );

        // An approved plan now yields the eBay files it authorised. Offered
        // rather than auto-downloaded, matching how Module A's files work,
        // and only the files that actually have rows in them.
        expandPlanFilesFor = planId;
        await fetchDraftPlan();
    } catch (err) {
        logToTerminal("ERROR", `Approval failed: ${err.message}`);
        if (button) button.disabled = false;
    }
}

// Set after approving, so that plan's files open by themselves rather than
// needing a click straight after the action that produced them.
let expandPlanFilesFor = null;

function statusBadge(status) {
    const styles = {
        approved: "bg-emerald-950/80 text-emerald-400 border-emerald-800",
        pushed: "bg-sky-950/80 text-sky-300 border-sky-800",
        partial: "bg-amber-950/80 text-amber-300 border-amber-800",
        failed: "bg-rose-950/80 text-rose-300 border-rose-800",
        discarded: "bg-slate-900 text-slate-500 border-slate-800",
    };
    const cls = styles[status] || "bg-slate-900 text-slate-400 border-slate-800";
    return `<span class="text-[10px] font-bold uppercase px-1.5 py-0.5 rounded border ${cls}">${escapeHtml(status)}</span>`;
}

// Approved plans, newest first, each able to hand back the files it
// authorised. Rendered whether or not a draft is open: a plan approved
// yesterday is still the record of what was authorised, and the files may be
// uploaded well after the approval.
function renderPlanHistory(plans) {
    const box = document.getElementById("draftHistory");
    if (!box) return;
    const history = (plans || []).filter(p => p.status !== "draft").slice(0, 8);
    if (!history.length) {
        box.innerHTML = "";
        return;
    }

    box.innerHTML = `
        <p class="text-[11px] font-semibold text-slate-400 mt-4">Approved plans</p>
        ${history.map(p => `
            <div class="glass-card rounded-xl border border-slate-800 bg-dark-800/40 px-3.5 py-2.5">
                <div class="flex items-center gap-3">
                    <span class="text-xs font-bold text-slate-200">Plan ${p.id}</span>
                    ${statusBadge(p.status)}
                    <span class="text-[11px] text-slate-500">
                        ${p.item_count} change(s)${p.excluded_count ? `, ${p.excluded_count} left out` : ""}
                        ${p.approved_at ? ` &middot; approved ${escapeHtml(String(p.approved_at).slice(0, 16))}` : ""}
                    </span>
                    <button type="button" onclick="loadPlanListings(${p.id})"
                        class="ml-auto text-[11px] font-semibold px-2 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 transition-all">
                        Listings
                    </button>
                    ${p.status === "approved" || p.status === "partial"
                        ? `<button type="button" onclick="pushPlan(${p.id}, this)"
                            class="text-[11px] font-semibold px-2 py-1 rounded-lg bg-emerald-900/60 hover:bg-emerald-800 border border-emerald-800 text-emerald-200 transition-all">
                            Push to eBay
                        </button>`
                        : ""}
                    ${p.status === "approved"
                        ? `<button type="button" onclick="deletePlan(${p.id})" title="Delete this plan"
                            class="text-[11px] font-bold w-6 h-6 leading-none rounded-lg bg-slate-800 hover:bg-rose-900 border border-slate-700 hover:border-rose-700 text-slate-400 hover:text-rose-200 transition-all">
                            &times;
                        </button>`
                        : ""}
                </div>
                <div id="planFiles${p.id}" class="hidden mt-2"></div>
            </div>`).join("")}`;

    if (expandPlanFilesFor) {
        const planId = expandPlanFilesFor;
        expandPlanFilesFor = null;
        loadPlanListings(planId);
    }
}

async function loadPlanListings(planId) {
    const box = document.getElementById(`planFiles${planId}`);
    if (!box) return;
    box.classList.remove("hidden");
    box.innerHTML = `<p class="text-[11px] text-slate-500">Reading the plan&hellip;</p>`;
    try {
        const res = await fetch(`/api/plans/${planId}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not read the plan");

        const items = data.items || [];
        const pushedCount = items.filter(i => i.status === "pushed").length;

        // One row per listing with its own Push button. A create cannot be
        // undone by pressing the button again, so the only sane way to start
        // is one small listing, checked in Seller Hub, before several hundred
        // cards go live in a single call.
        const pushable = (data.groups || []).filter(
            g => g.item_count > g.excluded_count
        );
        const rows = pushable.length
            ? `<ul class="space-y-1">${pushable.map(g => `
                   <li class="flex items-center gap-2">
                       <span class="text-slate-300 truncate">${escapeHtml(g.set_name || g.group_key)}${g.condition ? ` &middot; ${escapeHtml(g.condition)}` : ""}</span>
                       <span class="text-slate-600">${g.item_count - g.excluded_count} card(s)</span>
                       <button type="button" onclick="pushPlan(${planId}, this, this.dataset.groupKey)" data-group-key="${escapeHtml(g.group_key)}"
                           class="ml-auto text-[10px] font-semibold px-2 py-0.5 rounded-lg bg-emerald-900/60 hover:bg-emerald-800 border border-emerald-800 text-emerald-200 transition-all">
                           Push${g.ebay_parent_id ? "" : " (new)"}
                       </button>
                   </li>`).join("")}</ul>`
            : `<p class="text-slate-500">Nothing left to push in this plan.</p>`;

        box.innerHTML = `
            <div class="rounded-lg border border-slate-700 bg-dark-900/60 p-2.5 text-[11px] space-y-1.5">
                <p class="text-slate-400">Push one listing at a time:</p>
                ${rows}
                ${pushedCount
                    ? `<p class="text-sky-300/90 pt-1">${pushedCount} card(s) in this plan are already live on eBay.</p>`
                    : ""}
                ${pushable.some(g => !g.ebay_parent_id)
                    ? `<p class="text-slate-500 pt-1">A listing marked <span class="text-emerald-300">new</span> does not exist on eBay yet, and creating it cannot be undone by pressing the button again. Start with a small one and check it in Seller Hub.</p>`
                    : ""}
            </div>`;
    } catch (err) {
        box.innerHTML = `<p class="text-[11px] text-rose-400">${escapeHtml(err.message)}</p>`;
    }
}

// -------------------------------------------------------------------
// Direct API push setup: policy ids and an inventory location
// -------------------------------------------------------------------
//
// Business policy ids are not visible anywhere in Seller Hub, so the only way
// to learn them is to ask eBay. The names already configured for the CSV path
// are matched to the answer, which turns this from "find three ids" into
// "confirm these three". A select is pre-selected on that match, never saved
// on it: listing against the wrong shipping policy costs real money.

let ebayPushSetup = null;

async function loadEbayPushSetup() {
    const box = document.getElementById("ebayPushSetupBox");
    const button = document.getElementById("btnEbayLoadSetup");
    if (!box) return;
    box.classList.remove("hidden");
    box.innerHTML = `<p class="text-slate-500">Asking eBay&hellip;</p>`;
    if (button) button.disabled = true;
    try {
        const res = await fetch("/api/ebay/account-setup");
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not read the account");
        ebayPushSetup = data;
        renderEbayPushSetup(data);
    } catch (err) {
        box.innerHTML = `<p class="text-rose-400">${escapeHtml(err.message)}</p>`;
    } finally {
        if (button) button.disabled = false;
    }
}

function policySelect(id, entries, selected, configuredName) {
    const options = [`<option value="">&mdash; not set &mdash;</option>`]
        .concat((entries || []).map(entry =>
            `<option value="${escapeHtml(entry.id)}"${entry.id === selected ? " selected" : ""}>${escapeHtml(entry.name || entry.id)} (${escapeHtml(entry.id)})</option>`));
    const hint = configuredName
        ? `<span class="text-slate-500">CSV files use &ldquo;${escapeHtml(configuredName)}&rdquo;</span>`
        : "";
    return `<select id="${id}" class="w-full mt-1 bg-dark-900 border border-slate-700 rounded-lg px-2 py-1.5 text-[11px] text-slate-200">${options.join("")}</select>${hint}`;
}

function renderEbayPushSetup(data) {
    const box = document.getElementById("ebayPushSetupBox");
    if (!box) return;
    const current = data.current || {};
    const suggested = data.suggested || {};
    const names = data.configured_names || {};
    const pick = (kind, key) => current[key] || suggested[kind] || "";

    const locations = data.locations || [];
    const locationBlock = locations.length
        ? `<label class="block">Inventory location
               <select id="ebayLocationSelect" class="w-full mt-1 bg-dark-900 border border-slate-700 rounded-lg px-2 py-1.5 text-[11px] text-slate-200">
                   ${locations.map(loc => `<option value="${escapeHtml(loc.key)}"${loc.key === current.merchant_location_key ? " selected" : ""}>${escapeHtml(loc.name || loc.key)} &middot; ${escapeHtml(loc.postal_code)} (${escapeHtml(loc.key)})</option>`).join("")}
               </select>
           </label>`
        : `<div class="rounded-lg border border-amber-800/60 bg-amber-950/30 p-2">
               <p class="text-amber-300">No inventory location on this account.</p>
               <p class="text-amber-200/80 mt-1">eBay will not publish an offer without one. A warehouse location needs only a postal code &mdash; no street address.</p>
               <button type="button" onclick="createEbayInventoryLocation()" class="mt-2 text-[11px] font-semibold px-2 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200">
                   Create one from ${escapeHtml(data.postal_code || "the postal code in Listing Rules")}
               </button>
           </div>`;

    box.innerHTML = `
        <p class="text-slate-400">Marketplace <span class="font-mono">${escapeHtml(data.marketplace_id || "")}</span></p>
        <label class="block">Shipping policy
            ${policySelect("ebayShippingPolicy", data.policies.fulfillment, pick("fulfillment", "shipping_policy_id"), names.fulfillment)}
        </label>
        <label class="block">Return policy
            ${policySelect("ebayReturnPolicy", data.policies.return, pick("return", "return_policy_id"), names.return)}
        </label>
        <label class="block">Payment policy
            ${policySelect("ebayPaymentPolicy", data.policies.payment, pick("payment", "payment_policy_id"), names.payment)}
        </label>
        ${locationBlock}
        <button type="button" onclick="saveEbayPushSetup()" class="mt-2 text-[11px] font-bold px-3 py-1.5 rounded-lg bg-brand-600 hover:bg-brand-500 border border-brand-500 text-white transition-all">
            Save for pushing
        </button>`;
}

async function createEbayInventoryLocation() {
    try {
        const res = await fetch("/api/ebay/inventory-location", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ merchant_location_key: "home", name: "Home" }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not create the location");
        logToTerminal("SUCCESS", `eBay inventory location "${data.merchant_location_key}" created`);
        await loadEbayPushSetup();
    } catch (err) {
        logToTerminal("ERROR", `Location failed: ${err.message}`);
    }
}

async function saveEbayPushSetup() {
    const value = id => document.getElementById(id)?.value || "";
    const settings = {
        shipping_policy_id: value("ebayShippingPolicy"),
        return_policy_id: value("ebayReturnPolicy"),
        payment_policy_id: value("ebayPaymentPolicy"),
    };
    const location = document.getElementById("ebayLocationSelect");
    if (location) settings.merchant_location_key = location.value;
    try {
        const res = await fetch("/api/listing-settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ settings }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not save");
        logToTerminal("SUCCESS", "eBay push settings saved");
        if (typeof loadListingSettings === "function") loadListingSettings();
    } catch (err) {
        logToTerminal("ERROR", `Save failed: ${err.message}`);
    }
}

// The one action in this dashboard that changes a live eBay listing, so the
// confirm states what it will do in numbers rather than asking "are you
// sure?". A partial push is offered again because it is resumable: cards
// already pushed are skipped, so pressing it twice cannot duplicate a listing.
// Returns the parsed body, or null when the response is not JSON at all.
// A reverse proxy timing out on a long request answers with an HTML page, and
// calling res.json() on that throws "Unexpected token '<'" -- which tells the
// user nothing about what happened to their push.
async function readJsonResponse(res) {
    const text = await res.text();
    try {
        return JSON.parse(text);
    } catch (err) {
        return null;
    }
}

let activePushPoll = null;

// The push runs as a job on the server and this follows it, so that switching
// tabs, reloading or losing the connection does not lose sight of it. Held as
// one long request it outlasted the reverse proxy and the page forgot a push
// was even happening; progress belongs on the server, and this only watches.
function rememberPushJob(jobId, planId) {
    try {
        sessionStorage.setItem("activePushJob", JSON.stringify({ jobId, planId }));
    } catch (err) { /* private browsing; polling still works this session */ }
}

function forgetPushJob() {
    try { sessionStorage.removeItem("activePushJob"); } catch (err) { /* ignore */ }
}

// Called whenever the Drafts tab renders, so a push started before a tab
// switch is picked up again instead of vanishing.
function resumePushJob() {
    if (activePushPoll) return;
    let stored = null;
    try { stored = JSON.parse(sessionStorage.getItem("activePushJob") || "null"); }
    catch (err) { stored = null; }
    if (stored && stored.jobId) {
        followPushJob(stored.jobId, stored.planId, { resumed: true });
    }
}

async function followPushJob(jobId, planId, opts) {
    const options = opts || {};
    if (activePushPoll) clearTimeout(activePushPoll);
    let seen = 0;
    if (options.resumed) {
        logToTerminal("INFO", `Rejoining the push of plan ${planId}…`);
    }

    const tick = async () => {
        let data;
        try {
            const res = await fetch(`/api/push-jobs/${encodeURIComponent(jobId)}?since=${seen}`);
            data = await readJsonResponse(res);
            if (res.status === 404) {
                activePushPoll = null;
                forgetPushJob();
                setPushBanner("");
                logToTerminal("WARN", (data && data.detail)
                    || "That push is no longer being tracked. Check the eBay Listings tab.");
                await fetchDraftPlan();
                return;
            }
            if (data === null) throw new Error(`the server answered ${res.status}`);
        } catch (err) {
            // A blip in polling says nothing about the push, which is running
            // on the server either way. Keep watching rather than reporting.
            activePushPoll = setTimeout(tick, 4000);
            return;
        }

        (data.logs || []).forEach(e => logToTerminal(e.level, e.message));
        seen = data.log_count || seen;

        if (data.status === "running") {
            const last = (data.logs || []).slice(-1)[0];
            setPushBanner(`Pushing plan ${data.plan_id}${data.group_key ? ` · ${data.group_key}` : ""}…`,
                last ? last.message : "");
            activePushPoll = setTimeout(tick, 2000);
            return;
        }

        activePushPoll = null;
        forgetPushJob();
        setPushBanner("");
        const result = data.result;
        if (data.error) {
            logToTerminal("ERROR", `Push failed: ${data.error}`);
        } else if (result && result.attempted === false) {
            logToTerminal("WARN", `Nothing was sent to eBay. ${result.reason || ""}`);
        } else if (result) {
            logToTerminal(result.pushed && !result.failed ? "SUCCESS" : "WARN",
                `Push complete: ${result.pushed} card(s) pushed, ${result.failed} failed, ${result.deferred} left for CSV`);
        }
        expandPlanFilesFor = planId;
        await fetchDraftPlan();
        if (typeof fetchEbayListings === "function") fetchEbayListings();
    };

    tick();
}

function setPushBanner(title, detail) {
    const box = document.getElementById("pushBanner");
    if (!box) return;
    if (!title) {
        box.classList.add("hidden");
        box.innerHTML = "";
        return;
    }
    box.classList.remove("hidden");
    box.innerHTML = `
        <div class="flex items-center gap-3">
            <span class="w-3 h-3 rounded-full border-2 border-emerald-400 border-t-transparent animate-spin"></span>
            <div class="min-w-0">
                <p class="text-xs font-semibold text-emerald-200">${escapeHtml(title)}</p>
                ${detail ? `<p class="text-[11px] text-slate-400 truncate">${escapeHtml(detail)}</p>` : ""}
            </div>
            <span class="ml-auto text-[10px] text-slate-500">This continues on the server &mdash; you can switch tabs.</span>
        </div>`;
}

// The one action in this dashboard that changes a live eBay listing, so the
// confirm states what it will do in numbers rather than asking "are you
// sure?". A partial push is offered again because it is resumable: cards
// already pushed are skipped, so pressing it twice cannot duplicate a listing.
async function pushPlan(planId, button, groupKey) {
    const what = groupKey ? `the "${groupKey}" listing from plan ${planId}` : `all of plan ${planId}`;
    if (!confirm(`Push ${what} to eBay now? This creates and updates live listings immediately — there is no draft on eBay's side. Cards already pushed are skipped.`)) {
        return;
    }
    if (button) { button.disabled = true; button.textContent = "Pushing…"; }
    logToTerminal("INFO", `Pushing plan ${planId} to eBay…`);
    try {
        const res = await fetch(`/api/plans/${planId}/push`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(groupKey ? { group_key: groupKey } : {}),
        });
        const data = await readJsonResponse(res);
        if (data === null) throw new Error(`the server answered ${res.status}`);
        if (!res.ok) throw new Error(data.detail || "The push could not be started");
        if (data.already_running) {
            logToTerminal("WARN", "A push for this plan is already running; following that one.");
        }
        rememberPushJob(data.job_id, planId);
        followPushJob(data.job_id, planId, {});
    } catch (err) {
        logToTerminal("ERROR", `Push failed to start: ${err.message}`);
        if (button) { button.disabled = false; button.textContent = "Push to eBay"; }
    }
}

// Deleting an approved plan from the history list. The files it produced are
// rebuilt from the plan on demand rather than stored, so this also throws away
// the only way to regenerate them -- hence naming that in the prompt rather
// than asking a bare "are you sure?".
async function deletePlan(planId) {
    if (!confirm(`Delete plan ${planId}? Its Add and Revise files are built from it on demand, so they go too. Anything already uploaded to eBay stays as it is.`)) {
        return;
    }
    try {
        const res = await fetch(`/api/plans/${planId}`, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not delete this plan");
        logToTerminal("INFO", `Plan ${planId} deleted`);
        await fetchDraftPlan();
    } catch (err) {
        logToTerminal("ERROR", `Delete failed: ${err.message}`);
    }
}

async function discardDraftPlan() {
    if (!currentDraftPlan || !currentDraftPlan.plan) return;
    const planId = currentDraftPlan.plan.id;
    if (!confirm("Discard this draft? The catalogue is untouched and a new draft can be rebuilt at any time.")) {
        return;
    }
    try {
        const res = await fetch(`/api/plans/${planId}`, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not discard this draft");
        logToTerminal("INFO", `Draft ${planId} discarded`);
        await fetchDraftPlan();
    } catch (err) {
        logToTerminal("ERROR", `Discard failed: ${err.message}`);
    }
}

// -------------------------------------------------------------------
// 4. TERMINAL LOG CONSOLE
// -------------------------------------------------------------------

// How many lines the on-screen console keeps. Older ones are still there —
// they are in the database, behind "Full log" — but an unbounded list on a
// page that stays open for days is a memory leak with a scrollbar.
const CONSOLE_VISIBLE_LINES = 200;

function levelClasses(level) {
    if (level === "SUCCESS") return ["text-emerald-400", "text-emerald-400 font-bold"];
    if (level === "WARN") return ["text-amber-400", "text-amber-400 font-bold"];
    if (level === "ERROR") return ["text-rose-400", "text-rose-400 font-bold"];
    if (level === "INFO") return ["text-cyan-400", "text-cyan-400"];
    return ["text-slate-400", "text-slate-400"];
}

// One line, as both the live console and the full-log dialog render it. The
// stamp is passed in rather than read from the clock, because a stored line's
// time is the time it happened, not the time it was drawn.
function logLineElement(level, message, stamp, source) {
    const [levelColor, badgeClass] = levelClasses(level);
    const el = document.createElement("div");
    el.className = `log-entry flex items-start gap-2 ${levelColor}`;
    el.innerHTML = `
        <span class="text-slate-600 select-none shrink-0">[${escapeHtml(stamp)}]</span>
        <span class="${badgeClass} shrink-0">[${escapeHtml(level)}]</span>
        ${source ? `<span class="text-slate-600 shrink-0">${escapeHtml(source)}</span>` : ""}
        <span class="text-slate-300 break-words flex-1">${escapeHtml(message)}</span>
    `;
    return el;
}

// Stored timestamps are SQLite's UTC "YYYY-MM-DD HH:MM:SS". Rendered in local
// time, with the date shown only when the line is not from today — a log you
// scroll back through needs to say which day it is talking about.
function formatLogStamp(value) {
    if (!value) return "";
    const parsed = new Date(String(value).replace(" ", "T") + "Z");
    if (isNaN(parsed.getTime())) return String(value);
    const sameDay = parsed.toDateString() === new Date().toDateString();
    return sameDay
        ? parsed.toLocaleTimeString()
        : `${parsed.toLocaleDateString()} ${parsed.toLocaleTimeString()}`;
}

function logToTerminal(level, message) {
    const consoleBox = document.getElementById("terminalLogBox");
    noteConsoleActivity(level);
    consoleBox.appendChild(
        logLineElement(level, message, new Date().toLocaleTimeString(), "")
    );
    while (consoleBox.childElementCount > CONSOLE_VISIBLE_LINES) {
        consoleBox.removeChild(consoleBox.firstElementChild);
    }
    consoleBox.scrollTop = consoleBox.scrollHeight;
}

// The lines the server recorded: the nightly price refresh, the repricer, and
// any push that outlived the page that started it. Without this the console
// could only ever show what this tab had personally witnessed.
async function loadPersistedLogs() {
    const consoleBox = document.getElementById("terminalLogBox");
    if (!consoleBox) return;
    try {
        const res = await fetch(`/api/logs?limit=${CONSOLE_VISIBLE_LINES}`);
        if (!res.ok) return;
        const data = await res.json();
        const entries = (data.entries || []).slice().reverse();
        if (!entries.length) return;

        consoleBox.innerHTML = "";
        entries.forEach(e => consoleBox.appendChild(
            logLineElement(e.level, e.message, formatLogStamp(e.created_at), e.source)
        ));
        const note = document.createElement("div");
        note.className = "text-slate-600 pt-1";
        note.innerText = data.total > entries.length
            ? `[SYSTEM] Showing the last ${entries.length} of ${data.total} recorded lines. Open Full log to scroll further back.`
            : `[SYSTEM] ${entries.length} recorded line(s).`;
        consoleBox.appendChild(note);
        consoleBox.scrollTop = consoleBox.scrollHeight;

        const count = document.getElementById("consoleStoredCount");
        if (count) count.innerText = `${data.total} stored`;
    } catch (err) {
        console.error("persisted logs:", err);
    }
}

function clearConsoleLogs() {
    const consoleBox = document.getElementById("terminalLogBox");
    consoleBox.innerHTML = `<div class="text-slate-500">[SYSTEM] View cleared. The recorded history is kept &mdash; open Full log to read it.</div>`;
    clearConsoleUnread();
}

// -- the full log ----------------------------------------------------------

let fullLogOldestId = null;
let fullLogLevels = "";

function openFullLog() {
    document.getElementById("fullLogModal").classList.remove("hidden");
    syncModalScrollLock();
    fullLogOldestId = null;
    document.getElementById("fullLogBody").innerHTML = "";
    loadFullLogPage();
}

function closeFullLog() {
    document.getElementById("fullLogModal").classList.add("hidden");
    syncModalScrollLock();
}

function setFullLogFilter(levels) {
    fullLogLevels = levels;
    document.querySelectorAll("[data-log-filter]").forEach(button => {
        const active = button.getAttribute("data-log-filter") === levels;
        button.className = "px-2.5 py-1 rounded-lg text-[11px] font-semibold border transition-colors "
            + (active
                ? "bg-brand-600 border-brand-500 text-white"
                : "bg-dark-800 border-slate-700 text-slate-300 hover:bg-slate-700");
    });
    fullLogOldestId = null;
    document.getElementById("fullLogBody").innerHTML = "";
    loadFullLogPage();
}

async function loadFullLogPage() {
    const body = document.getElementById("fullLogBody");
    const more = document.getElementById("fullLogMore");
    const status = document.getElementById("fullLogStatus");
    if (!body) return;

    try {
        const params = new URLSearchParams({ limit: "500" });
        if (fullLogOldestId) params.set("before_id", String(fullLogOldestId));
        if (fullLogLevels) params.set("level", fullLogLevels);

        const res = await fetch(`/api/logs?${params.toString()}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not read the log");

        const entries = data.entries || [];
        // Newest first from the server; newest first on screen too, so
        // "load older" appends downwards and nothing already read jumps.
        entries.forEach(e => body.appendChild(
            logLineElement(e.level, e.message, formatLogStamp(e.created_at), e.source)
        ));
        if (entries.length) fullLogOldestId = data.oldest_id;

        if (status) {
            status.innerText = `${body.childElementCount} line(s) shown of ${data.total} recorded`;
        }
        if (more) more.classList.toggle("hidden", entries.length < 500);
        if (!body.childElementCount) {
            body.innerHTML = `<div class="text-slate-500">Nothing recorded yet at this level.</div>`;
        }
    } catch (err) {
        body.innerHTML = `<div class="text-rose-400">${escapeHtml(err.message)}</div>`;
    }
}

async function clearStoredLogs() {
    if (!confirm("Delete the recorded log history?\n\nThis is the record of what the nightly jobs did to your live listings. The on-screen Clear button does not do this.")) return;
    try {
        const res = await fetch("/api/logs/clear", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not clear the log");
        logToTerminal("WARN", `Recorded log history deleted (${data.removed} line(s)).`);
        fullLogOldestId = null;
        document.getElementById("fullLogBody").innerHTML = "";
        loadFullLogPage();
    } catch (err) {
        logToTerminal("ERROR", err.message);
    }
}

// Deliberately a function declaration, not a const arrow: it is defined at the
// bottom of the file but called from render code far above, and only
// declarations hoist.
function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// Escape closes whichever modal is open, which also releases the scroll lock.
document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const open = openModals();
    if (!open.length) return;
    open[open.length - 1].classList.add("hidden");
    syncModalScrollLock();
});
