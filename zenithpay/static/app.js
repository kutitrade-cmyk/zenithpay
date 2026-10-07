/* ============================================================
   Zenith Pay — shared member-app helpers.

   Loaded by every member page (dashboard, transfer, send, bills,
   scheduled, activity). Pages keep only their own feature logic
   in their inline script; everything shared lives here:

   - state (user, accounts, payees, schedules)
   - toast system
   - formatting (centsToCurrency, esc, dates)
   - apiFetch + the strict error-envelope detector
   - session bootstrap (user chip) and sign-out
   - safeRun() boot isolation
   ============================================================ */

const API = "/api";
const state = {
    user: null,
    accounts: [],
    payees: [],
    schedules: [],
};

let pendingActionButton = null;

document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    pendingActionButton = button;
    setTimeout(() => {
        if (pendingActionButton === button) pendingActionButton = null;
    }, 0);
}, true);

document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    pendingActionButton =
        event.submitter ||
        form.querySelector('button[type="submit"], button:not([type])');
}, true);

function startButtonLoading(button) {
    if (!button || button.disabled) return () => {};

    const originalContent = button.innerHTML;
    const wasDisabled = button.disabled;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.classList.add("is-loading");
    button.innerHTML =
        '<span class="button-spinner" aria-hidden="true"></span><span>Processing…</span>';

    return () => {
        button.disabled = wasDisabled;
        button.removeAttribute("aria-busy");
        button.classList.remove("is-loading");
        button.innerHTML = originalContent;
    };
}

/* ---------- Toast system ---------- */
function triggerToast(message, type = "info") {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const colors = {
        success: "border-zenith-500/40 text-zenith-400",
        error:   "border-rose-500/40 text-rose-400",
        info:    "border-cyan-500/40 text-cyan-400",
    };
    const icon = {
        success: "fa-circle-check",
        error:   "fa-circle-xmark",
        info:    "fa-circle-info",
    };
    const toast = document.createElement("div");
    toast.className =
        "glass-panel px-4 py-3 rounded-2xl border text-xs font-bold text-white shadow-2xl flex items-center gap-3 pointer-events-auto transition-all";
    toast.classList.add(...(colors[type] || colors.info).split(/\s+/));
    toast.innerHTML =
        `<i class="fa-solid ${icon[type] || icon.info} text-sm"></i> <span>${message}</span>`;
    container.appendChild(toast);
    setTimeout(() => { toast.remove(); }, 3500);
}

/* ---------- Formatting ---------- */
function centsToCurrency(cents, currencyCode = "USD") {
    const value = cents / 100;
    const formatter = new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: currencyCode,
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
    return formatter.format(value);
}

function formatDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleDateString("en-US", {
        year: "numeric", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit",
    });
}

function formatDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso + "T00:00:00Z");
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleDateString("en-US", {
        year: "numeric", month: "short", day: "numeric",
    });
}

function esc(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

/* ---------- Fetch with auth + error handling ---------- */
async function apiFetch(path, options = {}) {
    const button = pendingActionButton;
    pendingActionButton = null;
    const stopLoading = startButtonLoading(button);
    try {
        const res = await fetch(API + path, {
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            ...options,
        });
        if (!res.ok) {
            if (res.status === 401) {
                triggerToast("Session expired — signing you out", "error");
                setTimeout(() => { window.location.href = "index.html"; }, 1200);
                return null;
            }
            let body = null;
            try { body = await res.json(); } catch (_) { body = null; }
            const msg = (body && body.error) ? body.error : `HTTP ${res.status}`;
            const code = (body && body.code) ? body.code : "request_failed";
            triggerToast(`${msg} (${code})`, "error");
            return null;
        }
        // 2xx: normally a normal payload. Only treat the body as an error
        // envelope if it has the exact API error shape ({"error","code"}).
        const data = await res.json();
        if (isErrorEnvelope(data)) {
            triggerToast(`${data.error} (${data.code})`, "error");
            return null;
        }
        return data;
    } finally {
        stopLoading();
    }
}

function isErrorEnvelope(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    if (typeof data.error !== "string" || typeof data.code !== "string") return false;
    // The API's error responses are exactly {"error", "code"} — nothing else.
    const keys = Object.keys(data);
    return keys.length === 2 && keys.includes("error") && keys.includes("code");
}

/* ---------- Session: load the signed-in member ---------- */
async function loadUser() {
    const data = await apiFetch("/auth/me");
    if (!data) return;
    state.user = data.user;
    const chip = document.getElementById("user-chip");
    if (chip) chip.classList.remove("hidden");
    const nameEl = document.getElementById("user-name");
    const emailEl = document.getElementById("user-email");
    const avatarEl = document.getElementById("user-avatar");
    if (nameEl) nameEl.textContent = state.user.name || "Member";
    if (emailEl) emailEl.textContent = state.user.email || "member@zenith.pay";
    if (avatarEl) {
        if (state.user.avatar_data) {
            if (avatarEl instanceof HTMLImageElement) {
                avatarEl.src = state.user.avatar_data;
                avatarEl.hidden = false;
            } else {
                const image = document.createElement("img");
                image.src = state.user.avatar_data;
                image.alt = "";
                image.className = "h-full w-full rounded-full object-cover";
                avatarEl.replaceChildren(image);
            }
        } else {
            if (avatarEl instanceof HTMLImageElement) avatarEl.hidden = true;
            const initial = (state.user.name || "M")[0].toUpperCase();
            if (!(avatarEl instanceof HTMLImageElement)) avatarEl.textContent = initial;
        }
    }
    document.querySelectorAll("#user-name").forEach((el) => {
        el.textContent = state.user.name || "Member";
    });
    document.querySelectorAll("#admin-link").forEach((link) => {
        if (state.user.role === "admin") link.classList.remove("hidden");
    });
}

/* ---------- Accounts ---------- */
async function loadAccounts() {
    const data = await apiFetch("/accounts");
    if (!data) return;
    state.accounts = data.accounts || [];
    state.summary = data.summary || {
        count: state.accounts.length,
        balance_cents_by_currency: {},
    };
    state.scheduler = data.scheduler || { executed: 0, failed: 0 };
}

/* Options HTML for "pick a source account" dropdowns. Loans are not
   spendable sources, so they never appear in these lists. */
function assetAccountOptions(selectedId = "") {
    return '<option value="">Select account</option>' +
        state.accounts
            .filter((a) => a.type !== "loan")
            .map((a) =>
                `<option value="${a.id}" ${String(a.id) === String(selectedId) ? "selected" : ""}>` +
                `${esc(a.nickname || a.type)} · ${a.currency} (${centsToCurrency(a.balance_cents, a.currency)})` +
                `</option>`
            )
            .join("");
}

/* Account id preselected via a quick action link (?from=<id>). */
function requestedSourceId() {
    return new URLSearchParams(window.location.search).get("from") || "";
}

/* ---------- Sign out ---------- */
document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("header nav").forEach((nav) => {
        const sample = nav.querySelector("a");
        const linkClass = sample ? sample.className : "";
        const links = [
            ["crypto.html", "Crypto"],
            ["settings.html", "Settings"],
            ["admin.html", "Admin"],
        ];
        links.forEach(([href, label]) => {
            if (nav.querySelector(`a[href="${href}"]`)) return;
            const link = document.createElement("a");
            link.href = href;
            link.textContent = label;
            link.className = linkClass;
            link.id = href === "admin.html" ? "admin-link" : "";
            if (href === "admin.html") link.classList.add("hidden");
            nav.append(link);
        });
    });

    document.querySelectorAll("header").forEach((header, index) => {
        const navs = [...header.querySelectorAll("nav")];
        if (!navs.length) return;
        const mobileNav =
            navs.find((nav) => nav.classList.contains("md:hidden")) ||
            navs.find((nav) => nav.classList.contains("site-nav")) ||
            navs[0];
        const toggle = document.createElement("button");
        const navId = `mobile-navigation-${index + 1}`;
        toggle.type = "button";
        toggle.className = "mobile-nav-toggle";
        toggle.setAttribute("aria-label", "Open navigation menu");
        toggle.setAttribute("aria-controls", navId);
        toggle.setAttribute("aria-expanded", "false");
        toggle.innerHTML =
            '<span class="hamburger-icon" aria-hidden="true"><span></span><span></span><span></span></span><span>Menu</span>';
        mobileNav.id = mobileNav.id || navId;
        mobileNav.classList.add("mobile-member-nav");
        header.classList.add("has-mobile-menu");

        const actions =
            header.querySelector(".header-actions") ||
            header.querySelector("#btn-signout")?.parentElement ||
            header;
        const signOut = actions.querySelector("#btn-signout");
        actions.insertBefore(toggle, signOut || null);

        toggle.addEventListener("click", () => {
            const isOpen = header.classList.toggle("mobile-menu-open");
            toggle.setAttribute("aria-expanded", String(isOpen));
            toggle.setAttribute(
                "aria-label",
                isOpen ? "Close navigation menu" : "Open navigation menu"
            );
        });
        mobileNav.addEventListener("click", (event) => {
            if (event.target.closest("a")) {
                header.classList.remove("mobile-menu-open");
                toggle.setAttribute("aria-expanded", "false");
                toggle.setAttribute("aria-label", "Open navigation menu");
            }
        });
        header.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            header.classList.remove("mobile-menu-open");
            toggle.setAttribute("aria-expanded", "false");
            toggle.setAttribute("aria-label", "Open navigation menu");
            toggle.focus();
        });
    });

    const btn = document.getElementById("btn-signout");
    if (!btn) return;
    btn.addEventListener("click", async () => {
        try {
            await apiFetch("/auth/logout", { method: "POST" });
        } catch (err) {
            // Network failure still counts as signed-out from the user's view.
            console.error("[signout]", err);
        }
        setTimeout(() => { window.location.href = "index.html"; }, 300);
    });
});

/* ---------- Boot isolation ---------- */
/* Each loader runs in isolation: one failing (missing container, rejected
   fetch, thrown render bug) must not stop the rest of the page from
   loading. Failures surface as a toast instead of a blank page. */
async function safeRun(label, fn) {
    try {
        await fn();
    } catch (err) {
        console.error(`[${label}]`, err);
        triggerToast(`${label} failed to load`, "error");
    }
}
