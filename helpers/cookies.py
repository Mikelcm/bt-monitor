"""Click through the GDPR / cookie consent banner before scanning.

Without this, scans against any Romanian / EU site are crippled:
  - the banner covers content, hiding real elements
  - clicks on links/buttons under the banner are intercepted
  - screenshots show the banner instead of the page
  - axe-core reports false positives on the banner's own a11y

Ported from flaviuzh/bt-monitor and expanded with more vendor selectors
(Cookiebot, OneTrust, Cookiehub, Google CookieScript, plain Romanian
labels). Order matters — vendor-specific IDs first (faster, deterministic),
then text-matching fallbacks.
"""
from __future__ import annotations

from patchright.async_api import Page

# Selectors we try, in order. First match wins.
# Vendor IDs are most reliable; text matchers catch the long tail.
COOKIE_SELECTORS: list[str] = [
    # Cookiebot
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    # OneTrust
    "#onetrust-accept-btn-handler",
    "#accept-recommended-btn-handler",
    # Cookiehub
    ".ch2-allow-all-btn",
    ".ch2-btn-primary",
    # Google CookieScript
    "#cookiescript_accept",
    # Quantcast Choice
    'button[mode="primary"]',
    # Generic data-attr / class hints
    'button[id*="accept" i]',
    'button[class*="accept" i]',
    'button[id*="consent" i]',
    'button[data-action="accept"]',
    'button[data-testid*="accept" i]',
    # Romanian text labels — banks/government most common
    'button:has-text("Accept toate")',
    'button:has-text("Acceptă toate")',
    'button:has-text("Acceptă")',
    'button:has-text("Sunt de acord")',
    'button:has-text("De acord")',
    'button:has-text("Am înțeles")',
    'button:has-text("Permite toate")',
    # English fallbacks
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'button:has-text("Accept cookies")',
    'button:has-text("I agree")',
    'button:has-text("Got it")',
    'button:has-text("OK, got it")',
]


async def dismiss_cookies(page: Page, settle_ms: int = 250) -> str | None:
    """Try to click through any common consent banner. Returns the matched
    selector if a click landed, None otherwise.

    Safe to call on every page even when there's no banner — silent no-op."""
    for sel in COOKIE_SELECTORS:
        try:
            btn = await page.query_selector(sel)
        except Exception:
            continue
        if btn is None:
            continue
        try:
            visible = await btn.is_visible()
        except Exception:
            visible = True   # if we can't tell, try anyway
        if not visible:
            continue
        try:
            await btn.click(timeout=2000)
            await page.wait_for_timeout(settle_ms)
            return sel
        except Exception:
            continue
    return None
