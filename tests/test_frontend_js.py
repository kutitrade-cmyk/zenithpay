"""The inline JavaScript on each page must parse, and its helpers must hold.

These guard two bugs that shipped once already:

- a mangled function definition (a whole body folded into a comment) that
  made the page throw on load;
- a 2xx payload that merely carried an ``error`` key being mistaken for the
  API's error envelope and swallowed.

Node is optional: without it the suite degrades to static checks only.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "zenithpay" / "static"
PAGES = (
    "index.html", "login.html", "register.html",
    "dashboard.html", "transfer.html", "send.html",
    "bills.html", "scheduled.html", "activity.html",
)
MEMBER_PAGES = (
    "dashboard.html", "transfer.html", "send.html",
    "bills.html", "scheduled.html", "activity.html",
)
# Which boot loaders each member page isolates.
PAGE_BOOT_LABELS = {
    "dashboard.html": ("Session", "Accounts", "Activity"),
    "transfer.html": ("Session", "Accounts"),
    "send.html": ("Session", "Accounts"),
    "bills.html": ("Session", "Accounts", "Payees"),
    "scheduled.html": ("Session", "Accounts", "Payees", "Scheduled payments"),
    "activity.html": ("Session", "Accounts", "Activity"),
}

NODE = shutil.which("node")


def _inline_scripts(name: str) -> list[str]:
    html = (STATIC / name).read_text(encoding="utf-8")
    # Only <script> tags with no src= — the CDN ones are external.
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def _function_source(name: str, fn_name: str) -> str:
    """Extract a top-level ``function fn_name(...) {...}`` by brace matching."""
    html = (STATIC / name).read_text(encoding="utf-8")
    start = html.find(f"function {fn_name}(")
    assert start != -1, f"{fn_name} not found in {name}"
    brace = html.find("{", start)
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {fn_name} from {name}")


def test_all_inline_scripts_parse():
    """Every inline <script> block must be syntactically valid JavaScript."""
    if not NODE:
        pytest.skip("node not available")
    for page in PAGES:
        for index, body in enumerate(_inline_scripts(page)):
            proc = subprocess.run(
                [NODE, "--check", "-"],
                input=body,
                text=True,
                capture_output=True,
            )
            assert proc.returncode == 0, (
                f"{page} script #{index} failed to parse:\n{proc.stderr}"
            )


def test_pages_declare_doctype_and_charset():
    """The shell pages are complete documents, not fragments."""
    for page in PAGES:
        html = (STATIC / page).read_text(encoding="utf-8")
        assert html.lstrip().lower().startswith("<!doctype html>"), page
        assert '<meta charset="UTF-8">' in html, page
        assert 'name="viewport"' in html, page


@pytest.mark.skipif(not NODE, reason="node not available")
def test_error_envelope_detector_is_precise():
    """Only the exact API envelope counts as an error — not any payload with an error key."""
    src = _function_source("app.js", "isErrorEnvelope")
    probe = (
        src
        + "\n"
        + """
const cases = [
  [{error: "insufficient funds", code: "insufficient_funds"}, true],
  [{error: "not found", code: "not_found"}, true],
  // Legitimate payloads that happen to carry an "error" key must NOT match.
  [{error: "something", code: "x", accounts: []}, false],
  [{error: 42, code: "x"}, false],
  [{error: "msg"}, false],
  [{code: "x"}, false],
  [{}, false],
  [null, false],
  [[], false],
  ["insufficient funds", false],
];
let bad = 0;
for (const [input, want] of cases) {
  const got = isErrorEnvelope(input);
  if (got !== want) {
    bad++;
    console.log("FAIL", JSON.stringify(input), "got", got, "want", want);
  }
}
process.exit(bad === 0 ? 0 : 1);
"""
    )
    proc = subprocess.run([NODE, "-"], input=probe, text=True, capture_output=True)
    assert proc.returncode == 0, f"envelope detector wrong:\n{proc.stdout}{proc.stderr}"


@pytest.mark.skipif(not NODE, reason="node not available")
def test_money_formatting_from_cents():
    """Amounts are integer cents and must render with two decimals."""
    src = _function_source("app.js", "centsToCurrency")
    probe = (
        src
        + "\n"
        + """
const cases = [
  [0, "USD", "$0.00"],
  [29, "USD", "$0.29"],
  [1999, "USD", "$19.99"],
  [-500, "USD", "-$5.00"],
  [100000, "USD", "$1,000.00"],
  [125000, "GBP", "\\u00a31,250.00"],
];
let bad = 0;
for (const [cents, cur, want] of cases) {
  const got = centsToCurrency(cents, cur);
  if (got !== want) {
    bad++;
    console.log("FAIL", cents, cur, "got", got, "want", want);
  }
}
process.exit(bad === 0 ? 0 : 1);
"""
    )
    proc = subprocess.run([NODE, "-"], input=probe, text=True, capture_output=True)
    assert proc.returncode == 0, f"money formatting wrong:\n{proc.stdout}{proc.stderr}"


@pytest.mark.skipif(not NODE, reason="node not available")
def test_html_escaper_handles_quotes_and_angles():
    """User-controlled text (payee names, descriptions) is escaped before render."""
    src = _function_source("app.js", "esc")
    probe = (
        src
        + "\n"
        + """
const cases = [
  ["<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"],
  ['a & b', "a &amp; b"],
  ['say "hi"', "say &quot;hi&quot;"],
  [null, ""],
  [undefined, ""],
  [0, "0"],
];
let bad = 0;
for (const [input, want] of cases) {
  const got = esc(input);
  if (got !== want) {
    bad++;
    console.log("FAIL", JSON.stringify(input), "got", JSON.stringify(got), "want", JSON.stringify(want));
  }
}
process.exit(bad === 0 ? 0 : 1);
"""
    )
    proc = subprocess.run([NODE, "-"], input=probe, text=True, capture_output=True)
    assert proc.returncode == 0, f"escaper wrong:\n{proc.stdout}{proc.stderr}"


@pytest.mark.skipif(not NODE, reason="node not available")
def test_toast_adds_color_classes_without_domtokenlist_error():
    """Color styles are separate class tokens; a toast must not abort form success."""
    src = _function_source("app.js", "triggerToast")
    probe = (
        src
        + "\n"
        + """
const added = [];
const toast = {
  className: "",
  classList: { add(...tokens) {
    if (tokens.some(token => token.includes(" "))) throw new Error("invalid class token");
    added.push(...tokens);
  }},
};
global.document = {
  getElementById: () => ({appendChild() {}}),
  createElement: () => toast,
};
global.setTimeout = () => {};
triggerToast("Account opened", "success");
if (!added.includes("border-zenith-500/40") || !added.includes("text-zenith-400")) {
  process.exit(1);
}
"""
    )
    proc = subprocess.run([NODE, "-"], input=probe, text=True, capture_output=True)
    assert proc.returncode == 0, f"toast class handling failed:\n{proc.stdout}{proc.stderr}"


def test_member_actions_show_loading_state_and_reduced_motion_spinner():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "site.css").read_text(encoding="utf-8")
    assert "function startButtonLoading(button)" in app_js
    assert 'button.setAttribute("aria-busy", "true")' in app_js
    assert "finally {\n        stopLoading();" in app_js
    assert ".button-spinner" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    for page in ("send.html", "transfer.html", "crypto.html", "bills.html", "scheduled.html", "admin.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        assert '<script src="app.js"></script>' in html, page


def test_interpage_links_stay_relative():
    """Pages link to siblings relatively so they also open from the filesystem."""
    for page in PAGES:
        html = (STATIC / page).read_text(encoding="utf-8")
        for target in ("index.html", "login.html", "register.html", "dashboard.html"):
            assert f'href="/{target}"' not in html, f"{page} links absolutely to {target}"


def test_sign_in_posts_to_the_real_auth_api():
    """Sign-in must hit /api/auth/login — a simulated form never creates a session."""
    html = (STATIC / "login.html").read_text(encoding="utf-8")
    assert '"/api/auth/login"' in html, "login page never calls the auth API"
    assert 'credentials: "same-origin"' in html, "login POST must send the session cookie"


def test_sign_up_posts_to_the_real_auth_api():
    html = (STATIC / "register.html").read_text(encoding="utf-8")
    assert '"/api/auth/register"' in html, "register page never calls the auth API"
    assert 'credentials: "same-origin"' in html, "register POST must send the session cookie"


def test_post_auth_redirect_targets_member_or_admin_dashboard():
    """Members land on their dashboard; configured admins land on the admin page."""
    for page in ("login.html", "register.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        assert 'function postAuthTarget()' in html, page
        if page == "login.html":
            assert 'window.authenticatedRole === "admin" ? "admin.html" : "dashboard.html"' in html
        else:
            assert 'return "dashboard.html";' in html, f"{page} does not target the dashboard"
        assert 'window.location.href = postAuthTarget();' in html, (
            f"{page} does not navigate through postAuthTarget()"
        )


def test_dashboard_boots_defensively():
    """Each page's boot() loader is isolated — one failure must not stop the page."""
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "async function safeRun" in app_js, "safeRun must live in the shared app.js"
    for page, labels in PAGE_BOOT_LABELS.items():
        html = (STATIC / page).read_text(encoding="utf-8")
        for label in labels:
            assert f'safeRun("{label}"' in html, (
                f"{page} boot() does not isolate the {label} loader"
            )


def test_dashboard_activity_handler_binds_to_existing_list():
    """A missing event-handler variable must not abort the dashboard boot script."""
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")
    assert 'document.getElementById("activity-list").addEventListener("click"' in html
    assert '    list.addEventListener("click"' not in html


def test_all_page_scripts_parse_including_shared_app_js():
    """The shared app.js file must also be syntactically valid JavaScript."""
    if not NODE:
        pytest.skip("node not available")
    proc = subprocess.run(
        [NODE, "--check", str(STATIC / "app.js")],
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, f"app.js failed to parse:\n{proc.stderr}"


def test_member_pages_share_the_app_shell():
    """Every member page loads app.js and has the toast + sign-out shell."""
    for page in MEMBER_PAGES:
        html = (STATIC / page).read_text(encoding="utf-8")
        assert '<script src="app.js"></script>' in html, page
        assert 'id="toast-container"' in html, page
        assert 'id="btn-signout"' in html, page


def test_member_navigation_has_accessible_hamburger_menu():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "site.css").read_text(encoding="utf-8")
    assert 'toggle.setAttribute("aria-expanded", "false")' in app_js
    assert 'toggle.setAttribute("aria-controls", navId)' in app_js
    assert 'event.key !== "Escape"' in app_js
    assert "header.has-mobile-menu.mobile-menu-open .mobile-member-nav" in css
    assert "@media (max-width: 767px)" in css


def test_dashboard_balance_carousel():
    """The dashboard shows ONE swipeable hero card, not a tile grid."""
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")

    # Carousel shell exists...
    for fragment in (
        'id="balance-carousel"',
        'id="balance-track"',
        'id="balance-dots"',
        'aria-roledescription="carousel"',
        'class="balance-slide"',
        'class="balance-card"',
        'class="balance-dot',
    ):
        assert fragment in html, f"missing {fragment}"

    # ...and the old tile grid is fully gone.
    assert 'id="balance-tiles"' not in html
    assert "renderBalanceTiles" not in html

    # Swipe machinery: pointer gestures + snap + keyboard fallback.
    for fragment in (
        'addEventListener("pointerdown"',
        'addEventListener("pointermove"',
        "pointercancel",
        "no-transition",
        "ArrowLeft",
        "ArrowRight",
        "gotoBalance",
        "setCarouselPosition",
        "prefers-reduced-motion: reduce",
    ):
        assert fragment in html, f"missing swipe/a11y piece {fragment}"

    # Balances come from live state — never hardcoded currency rows.
    assert "balance_cents_by_currency" in html


def test_dashboard_phase_two_polish():
    """Quick actions + activity: staggered entrances, press states, empty state."""
    html = (STATIC / "dashboard.html").read_text(encoding="utf-8")

    # Quick actions: 2x2 mobile grid, exactly four, with per-icon motion.
    assert "grid-cols-2" in html
    for href in ("transfer.html", "send.html", "bills.html", "scheduled.html"):
        assert f'href="{href}"' in html
    for icon_class in ("qa-i-transfer", "qa-i-send", "qa-i-bills", "qa-i-scheduled"):
        assert icon_class in html, f"missing icon motion class {icon_class}"
    assert "qa-card:active" in html and "qa-card:hover" in html

    # Staggered entrance choreography (heading -> cards -> activity panel).
    assert "animation-delay:100ms" in html
    assert "animation-delay:150ms" in html
    assert "animation-delay:200ms" in html
    assert "animation-delay:250ms" in html
    assert "animation-delay:300ms" in html
    assert "animation-delay:350ms" in html

    # Activity rows: staggered reveal, running balance, clean empty state.
    assert "tx-in" in html
    assert "balance_after_cents" in html
    assert "No recent activity" in html
    assert "Your latest transactions will appear here." in html

    # View-all arrow nudge without changing navigation.
    assert 'class="view-all' in html
    assert 'href="activity.html" class="view-all' in html

    # Reduced motion covers the new animations too.
    assert ".qa-reveal" in html
    assert ".tx-in" in html
