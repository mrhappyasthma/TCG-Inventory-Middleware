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
let storedGeneratedCSVs = {
    orders: null,
    revise: null,
    add: null
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
        closePricingModal();
        logToTerminal("SUCCESS", "Saved your tiered pricing rules. Other users are unaffected.");
    } catch (err) {
        alert(err.message);
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

    document.getElementById("btnDownloadRevise").addEventListener("click", () => {
        if (storedGeneratedCSVs.revise) {
            triggerBrowserDownload(storedGeneratedCSVs.revise, "ebay_inventory_updates.csv");
        }
    });

    document.getElementById("btnDownloadOnlyBatch").addEventListener("click", () => {
        if (!pendingBatchFile) return;
        document.getElementById("batchDuplicateWarning").classList.add("hidden");
        logToTerminal("INFO", `[MODULE A] Rebuilding files for ${pendingBatchFile.name} - inventory will not change.`);
        handleBatchUpload(pendingBatchFile, "dry-run");
    });

    document.getElementById("btnForceProcessBatch").addEventListener("click", () => {
        if (!pendingBatchFile) return;
        document.getElementById("batchDuplicateWarning").classList.add("hidden");
        logToTerminal("WARN", `[MODULE A] Force processing ${pendingBatchFile.name} - quantities will be added again.`);
        handleBatchUpload(pendingBatchFile, "force");
    });

    document.getElementById("btnDownloadAdd").addEventListener("click", () => {
        if (storedGeneratedCSVs.add) {
            triggerBrowserDownload(storedGeneratedCSVs.add, "ebay_new_additions.csv");
        }
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
            ? "Cards live on eBay but missing from a full dump are revised down to 0."
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

        storedGeneratedCSVs.revise = data.revise_csv;
        storedGeneratedCSVs.add = data.add_csv;

        const resultBox = document.getElementById("resultBoxBatch");
        resultBox.classList.remove("hidden");

        const btnRev = document.getElementById("btnDownloadRevise");
        const btnAdd = document.getElementById("btnDownloadAdd");

        btnRev.style.display = data.revise_count > 0 ? "flex" : "none";
        btnAdd.style.display = data.add_count > 0 ? "flex" : "none";

        const parts = [];
        if (data.add_count > 0) parts.push(`${data.add_count} to add`);
        if (data.revise_count > 0) parts.push(`${data.revise_count} to revise`);
        if (data.skipped_count > 0) parts.push(`${data.skipped_count} skipped`);
        // Worth calling out separately: these are cards being pulled from
        // sale, not routine revisions.
        if (data.zeroed_count > 0) parts.push(`${data.zeroed_count} sold out → 0`);
        if (data.unchanged_count > 0) parts.push(`${data.unchanged_count} unchanged`);
        const suffix = data.dry_run ? " (inventory unchanged)" : "";
        // "Nothing to upload" on its own reads like a failure. When the
        // reason is that everything already matches eBay, say so.
        const nothingToSend = data.revise_count === 0 && data.add_count === 0;
        document.getElementById("batchReadyText").innerText = nothingToSend
            ? (data.unchanged_count > 0
                ? `Nothing to upload \u2014 all ${data.unchanged_count} card(s) already match eBay${suffix}`
                : `Nothing to upload${suffix}`)
            : `Files ready \u2014 ${parts.join(", ")}${suffix}`;

        logToTerminal(
            "SUCCESS",
            `[MODULE A] Files are ready${parts.length ? " (" + parts.join(", ") + ")" : ""}${suffix}. Click to download.`
        );

        if (data.unchanged_count > 0 && data.revise_count === 0) {
            logToTerminal(
                "INFO",
                `[MODULE A] No Revise file needed: all ${data.unchanged_count} linked card(s) already match eBay on quantity and price.`
            );
        }

        if (data.zeroed_count > 0) {
            logToTerminal(
                "WARN",
                `[MODULE A] ${data.zeroed_count} card(s) live on eBay were absent from this dump and are revised to quantity 0. See the rows above for which.`
            );
        }

        // A download-only rebuild wrote nothing, so there is nothing to refresh.
        if (!data.dry_run) {
            fetchStats();
            fetchSetFilter();
            fetchInventory();
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
        tbody.innerHTML = `<tr><td colspan="11" class="py-6 text-center text-rose-400">Failed to load inventory: ${escapeHtml(err.message)}</td></tr>`;
    }
}

// The quantity dialog needs the row it was opened from.
let lastInventoryItems = [];

function renderInventoryTable(items, total, offset) {
    lastInventoryItems = items || [];
    const tbody = document.getElementById("inventoryTableBody");

    if (!items || items.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="11" class="py-8 text-center text-slate-500">
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

        // What Module A last asked eBay for. eBay has not been told until the
        // Revise file is uploaded and a sync run, so showing this separately
        // is what makes the gap explicable instead of mysterious.
        const pending = item.pending_qty;
        const hasPending = pending !== null && pending !== undefined
            && pending !== item.last_known_qty;
        const driftTitle = hasPending
            ? `You hold ${qty}. eBay reports ${item.last_known_qty}. The generated Revise file asks eBay for ${pending} \u2014 upload it to eBay, then run a Module B sync.`
            : drifted
            ? `You hold ${qty}, eBay reports ${item.last_known_qty}. Run Module A to generate a Revise file, or Module B to re-sync.`
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
                    ${hasPending ? `<span class="ml-1 text-[10px] font-mono text-amber-400" title="Pending: the Revise file asks for ${pending}">&rarr;${pending}</span>` : ""}
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

        triggerBrowserDownload(data.csv_content, `ebay_cover_photo_${id}.csv`);
        logToTerminal("SUCCESS", `Cover photo recorded for eBay #${id}.`);
        logToTerminal("INFO",
            `Upload ebay_cover_photo_${id}.csv to Seller Hub to apply it. `
            + "eBay replaces the listing's whole picture set on revision.");

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
                        <button onclick="openCoverModal('${escapeHtml(l.ebay_parent_id)}')" class="flex items-center gap-2 text-left group/cover" title="${l.cover_image_url ? escapeHtml(l.cover_image_url) : "No cover photo recorded. Click to set one."}">
                            ${l.cover_image_url
                                ? `<img src="${escapeHtml(l.cover_image_url)}" alt="" class="w-8 h-8 rounded object-cover border border-slate-700 bg-dark-900" onerror="this.style.display='none'">`
                                : `<span class="w-8 h-8 rounded border border-dashed border-slate-700 flex items-center justify-center text-slate-600 text-[10px]">?</span>`}
                            <span class="text-[11px] ${l.cover_image_url ? "text-slate-400" : "text-slate-600 italic"} group-hover/cover:text-accent-cyan underline decoration-dotted">
                                ${l.cover_image_url ? "Change" : "Set cover"}
                            </span>
                        </button>
                    </td>
                    <td class="py-3 px-4 text-slate-500 text-[11px] font-mono">${escapeHtml(l.last_synced || "-")}</td>
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
// 4. TERMINAL LOG CONSOLE
// -------------------------------------------------------------------

function logToTerminal(level, message) {
    const consoleBox = document.getElementById("terminalLogBox");
    const now = new Date().toLocaleTimeString();
    noteConsoleActivity(level);

    let levelColor = "text-slate-400";
    let badgeClass = "text-slate-400";

    if (level === "SUCCESS") {
        levelColor = "text-emerald-400";
        badgeClass = "text-emerald-400 font-bold";
    } else if (level === "WARN") {
        levelColor = "text-amber-400";
        badgeClass = "text-amber-400 font-bold";
    } else if (level === "ERROR") {
        levelColor = "text-rose-400";
        badgeClass = "text-rose-400 font-bold";
    } else if (level === "INFO") {
        levelColor = "text-cyan-400";
        badgeClass = "text-cyan-400";
    }

    const logEl = document.createElement("div");
    logEl.className = `log-entry flex items-start gap-2 ${levelColor}`;
    logEl.innerHTML = `
        <span class="text-slate-600 select-none">[${now}]</span>
        <span class="${badgeClass}">[${escapeHtml(level)}]</span>
        <span class="text-slate-300 break-words flex-1">${escapeHtml(message)}</span>
    `;

    consoleBox.appendChild(logEl);
    consoleBox.scrollTop = consoleBox.scrollHeight;
}

function clearConsoleLogs() {
    const consoleBox = document.getElementById("terminalLogBox");
    consoleBox.innerHTML = `<div class="text-slate-500">[SYSTEM] Terminal logs cleared.</div>`;
    clearConsoleUnread();
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
