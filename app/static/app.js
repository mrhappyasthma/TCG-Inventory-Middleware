// Application State
let currentUser = null;
let googleClientId = "";
let currentPage = 1;
const pageSize = 20;
let currentSearch = "";
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

function updateAuthUI(data) {
    const btnOpenLogin = document.getElementById("btnOpenLoginModal");
    const userProfileBadge = document.getElementById("userProfileBadge");
    const btnLogout = document.getElementById("btnLogout");
    const btnAdminPanel = document.getElementById("btnAdminPanel");
    const pendingBanner = document.getElementById("pendingApprovalBanner");

    if (data.is_authenticated && data.user) {
        btnOpenLogin.classList.add("hidden");
        userProfileBadge.classList.remove("hidden");
        userProfileBadge.classList.add("flex");
        btnLogout.classList.remove("hidden");
        pendingBanner.classList.add("hidden");

        document.getElementById("userNameText").innerText = data.user.username;
        document.getElementById("userAvatarText").innerText = data.user.username[0].toUpperCase();

        const roleBadge = document.getElementById("userRoleBadge");
        roleBadge.innerText = data.user.role.toUpperCase();
        if (data.user.role === "admin") {
            btnAdminPanel.classList.remove("hidden");
            btnAdminPanel.classList.add("flex");
            roleBadge.className = "text-[10px] uppercase px-1.5 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-800";
        } else {
            btnAdminPanel.classList.add("hidden");
            roleBadge.className = "text-[10px] uppercase px-1.5 py-0.5 rounded bg-indigo-950 text-indigo-300 border border-indigo-800";
        }
    } else {
        btnOpenLogin.classList.remove("hidden");
        userProfileBadge.classList.add("hidden");
        userProfileBadge.classList.remove("flex");
        btnLogout.classList.add("hidden");
        btnAdminPanel.classList.add("hidden");

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
        renderPricingRulesEditor();
        updateTestPricePreview();
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-rose-400">Failed to load rules: ${err.message}</td></tr>`;
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
        closePricingModal();
        logToTerminal("SUCCESS", "Updated tiered pricing rules successfully.");
    } catch (err) {
        alert(err.message);
    }
}

async function resetDefaultPricingRules() {
    if (!confirm("Reset pricing rules to system defaults?")) return;
    try {
        const res = await fetch("/api/pricing-rules/reset", { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        cachedPricingRules = data.rules;
        renderPricingRulesEditor();
        updateTestPricePreview();
        logToTerminal("INFO", "Reset pricing rules to defaults.");
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

function resetListingSettings() {
    document.getElementById("settingSingleThreshold").value = "5.00";
    document.getElementById("settingTitleTemplate").value = "{set_name}: Pick Your Card - {condition} - Complete Your Set";
    document.getElementById("settingGroupBySet").checked = true;
    document.getElementById("settingDescriptorStyle").value = "label_id";
    updateTitlePreview();
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
                            ${u.role}
                        </span>
                    </td>
                    <td class="py-2.5 px-3">
                        <span class="px-2 py-0.5 rounded text-[10px] uppercase font-semibold border ${statusColor}">
                            ${u.status}
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
        tbody.innerHTML = `<tr><td colspan="7" class="py-4 text-center text-rose-400">Failed to load users: ${err.message}</td></tr>`;
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
        logToTerminal("INFO", `[MODULE B] Rebuilding files for ${pendingBatchFile.name} - inventory will not change.`);
        handleBatchUpload(pendingBatchFile, "dry-run");
    });

    document.getElementById("btnForceProcessBatch").addEventListener("click", () => {
        if (!pendingBatchFile) return;
        document.getElementById("batchDuplicateWarning").classList.add("hidden");
        logToTerminal("WARN", `[MODULE B] Force processing ${pendingBatchFile.name} - quantities will be added again.`);
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

    logToTerminal("INFO", `[MODULE A] Uploading ${file.name} for eBay Orders processing...`);

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
        logToTerminal("SUCCESS", `[MODULE A] sortswift_orders_import.csv is ready (${data.converted_count} items). Click to download.`);
    } catch (err) {
        logToTerminal("ERROR", `[MODULE A] ${err.message}`);
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

    const intent = mode === "dry-run"
        ? "rebuilding files only"
        : mode === "force" ? "force processing" : "routing";
    logToTerminal("INFO", `[MODULE B] Uploading ${file.name} (${intent})...`);

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
        const suffix = data.dry_run ? " (inventory unchanged)" : "";
        document.getElementById("batchReadyText").innerText = parts.length
            ? `Files ready \u2014 ${parts.join(", ")}${suffix}`
            : `Nothing to upload${suffix}`;

        logToTerminal(
            "SUCCESS",
            `[MODULE B] Files are ready${parts.length ? " (" + parts.join(", ") + ")" : ""}${suffix}. Click to download.`
        );

        // A download-only rebuild wrote nothing, so there is nothing to refresh.
        if (!data.dry_run) {
            fetchStats();
            fetchInventory();
        }
    } catch (err) {
        logToTerminal("ERROR", `[MODULE B] ${err.message}`);
    }
}

async function handleSyncUpload(file) {
    const formData = new FormData();
    formData.append("file", file);

    logToTerminal("INFO", `[MODULE C] Uploading ${file.name} for eBay Store State synchronization...`);

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
        document.getElementById("syncSummaryText").innerText = `Synced ${data.synced_count} Variations`;

        fetchStats();
        fetchInventory();
    } catch (err) {
        logToTerminal("ERROR", `[MODULE C] ${err.message}`);
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
        fetchInventory();
    } catch (err) {
        alert(err.message);
    }
}

async function fetchStats() {
    try {
        const res = await fetch("/api/stats");
        if (res.ok) {
            const data = await res.json();
            document.getElementById("statTotalCards").innerText = data.total_cards.toLocaleString();
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
    const url = `/api/inventory?search=${encodeURIComponent(currentSearch)}&sort_by=${currentSortBy}&sort_dir=${currentSortDir}&limit=${pageSize}&offset=${offset}`;

    try {
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);

        renderInventoryTable(data.items, data.total, offset);
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="11" class="py-6 text-center text-rose-400">Failed to load inventory: ${err.message}</td></tr>`;
    }
}

function renderInventoryTable(items, total, offset) {
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

        // Our catalogued count vs what eBay last reported. A mismatch is the
        // interesting case, so flag it rather than making them diff by eye.
        const qty = item.quantity ?? 0;
        const drifted = qty !== item.last_known_qty;
        const qtyBadge = drifted
            ? 'bg-amber-950/80 text-amber-300 border border-amber-800'
            : 'bg-slate-900 text-slate-400 border border-slate-800';
        const driftTitle = drifted
            ? `Catalogued ${qty}, eBay reports ${item.last_known_qty}. Run a Module C sync, or revise the listing.`
            : 'Catalogued quantity matches eBay.';

        return `
            <tr class="hover:bg-dark-800/80 transition-colors">
                <td class="py-3 px-4 font-mono font-bold text-accent-cyan">${escapeHtml(item.manifest_id)}</td>
                <td class="py-3 px-4 font-medium text-white">${escapeHtml(item.product_name)}</td>
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
                <td class="py-3 px-4 font-mono text-slate-300">${item.ebay_parent_id ? escapeHtml(item.ebay_parent_id) : '<span class="text-slate-600 italic">Not on eBay</span>'}</td>
                <td class="py-3 px-4 text-center" title="${escapeHtml(driftTitle)}">
                    <span class="inline-block min-w-[28px] px-2 py-0.5 rounded-full text-[11px] font-bold font-mono ${qtyBadge}">
                        ${qty}
                    </span>
                </td>
                <td class="py-3 px-4 text-center" title="${escapeHtml(driftTitle)}">
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
// 4. TERMINAL LOG CONSOLE
// -------------------------------------------------------------------

function logToTerminal(level, message) {
    const consoleBox = document.getElementById("terminalLogBox");
    const now = new Date().toLocaleTimeString();

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
        <span class="${badgeClass}">[${level}]</span>
        <span class="text-slate-300 break-words flex-1">${escapeHtml(message)}</span>
    `;

    consoleBox.appendChild(logEl);
    consoleBox.scrollTop = consoleBox.scrollHeight;
}

function clearConsoleLogs() {
    const consoleBox = document.getElementById("terminalLogBox");
    consoleBox.innerHTML = `<div class="text-slate-500">[SYSTEM] Terminal logs cleared.</div>`;
}

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
