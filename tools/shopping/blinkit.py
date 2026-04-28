"""
Blinkit shopping automation.

On first load Blinkit shows a location modal — we type the address and pick
the first suggestion (confirmed selector from live DOM inspection):
  div[class*='LocationSearchList__LocationDetailContainer']
"""

import asyncio
import urllib.parse
from tools.browser import browser_manager

# ── Delivery address used for Blinkit ────────────────────────────────────────
BLINKIT_DELIVERY_ADDRESS = "AIT College Pune"


async def shop_on_blinkit(items: list[str], page=None) -> dict:
    """
    Automates shopping on Blinkit for a list of items.
    Returns a dict with 'added' and 'unavailable' item lists.
    """
    print(f"\n[Blinkit] Starting shopping run for: {items}")
    page = page or browser_manager.page
    added_items = []
    unavailable_items = []

    print("[Blinkit] Navigating to blinkit.com...")
    await page.goto("https://blinkit.com", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(4)

    # Set delivery location before doing anything else
    await _set_delivery_location(BLINKIT_DELIVERY_ADDRESS, page=page)

    for item_name in items:
        print(f"\n[Blinkit] Processing item: {item_name}")
        try:
            # Direct search URL
            query = urllib.parse.quote_plus(item_name)
            search_url = f"https://blinkit.com/s/?q={query}"
            print(f"   Searching: {search_url}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)

            # If location modal reappears after navigation, set it again
            if await _location_modal_visible(page=page):
                print("   Location modal reappeared — setting address again...")
                await _set_delivery_location(BLINKIT_DELIVERY_ADDRESS, page=page)
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(3)

            # Check for no results
            page_text = (await page.content()).lower()
            no_result_phrases = [
                "no products found", "no results", "we couldn't find",
                "not available in your area",
            ]
            if any(phrase in page_text for phrase in no_result_phrases):
                print(f"   ❌ No results for '{item_name}' on Blinkit")
                unavailable_items.append(item_name)
                continue

            # Scroll so product cards render
            await page.evaluate("window.scrollBy(0, 300)")
            await asyncio.sleep(1.5)

            # Try clicking the ADD / + button on the first product card
            added = await _click_first_add_button(page)

            if added:
                added_items.append(item_name)
                print(f"   ✅ Added '{item_name}' to Blinkit cart")
            else:
                unavailable_items.append(item_name)
                print(f"   ❌ Item not available or no Add button found: '{item_name}'")

            await asyncio.sleep(2)

        except Exception as e:
            print(f"   ⚠️ Error processing '{item_name}': {e}")
            unavailable_items.append(item_name)

    # ── Click cart icon (top-right) to open the cart panel ──────────────
    print("\n[Blinkit] Opening cart panel...")
    await _click_blinkit_cart_icon(page=page)

    # ── Rechecker ─────────────────────────────────────────────────────────
    print("\n[Blinkit] Running cart rechecker...")
    verified = await _recheck_blinkit_cart(added_items, page=page)
    truly_unavailable = list(set(unavailable_items) | set(verified["missing_from_cart"]))

    result = {
        "platform": "blinkit",
        "added": verified["confirmed_in_cart"],
        "unavailable": truly_unavailable,
        "cart_url": "https://blinkit.com/cart",
    }

    if truly_unavailable:
        print(f"\n[Blinkit] ⚠️  Items not available: {truly_unavailable}")
    print(f"[Blinkit] ✅ Shopping run complete. Added: {result['added']}")
    return result


# ─── Cart Icon ───────────────────────────────────────────────────────────────

async def _click_blinkit_cart_icon(page=None) -> None:
    """
    Clicks the cart / basket button in the Blinkit header (top-right).
    Tries several selectors in order; falls back to navigating to /cart.
    """
    page = page or browser_manager.page
    cart_selectors = [
        # Playwright semantic locators
        ("role", "link",     "Cart"),
        ("role", "button",   "Cart"),
        ("role", "link",     "cart"),
        ("role", "button",   "My Cart"),
    ]

    # Try semantic locators first
    for kind, role, name in cart_selectors:
        try:
            loc = page.get_by_role(role, name=name)
            if await loc.first.is_visible(timeout=1500):
                await loc.first.click(timeout=3000)
                await asyncio.sleep(2)
                print("   ✅ Opened cart panel via header icon")
                return
        except Exception:
            continue

    # CSS / attribute selectors
    for sel in [
        "a[href='/cart']",
        "a[href*='cart']",
        "button[class*='cart']",
        "div[class*='cart'][role='button']",
        "div[class*='Cart'][role='button']",
        "[data-testid*='cart']",
        "[aria-label*='cart']",
        "[aria-label*='Cart']",
    ]:
        try:
            if await page.is_visible(sel, timeout=1500):
                await page.click(sel, timeout=3000)
                await asyncio.sleep(2)
                print("   ✅ Opened cart panel via CSS selector")
                return
        except Exception:
            continue

    # JS fallback: click first visible link/button whose text contains "cart"
    try:
        clicked = await page.evaluate("""() => {
            const candidates = [
                ...document.querySelectorAll('a, button, [role="button"]'),
            ];
            for (const el of candidates) {
                const txt = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
                const href = el.href || '';
                if (txt.includes('cart') || href.includes('cart')) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) { el.click(); return true; }
                }
            }
            return false;
        }""")
        if clicked:
            await asyncio.sleep(2)
            print("   ✅ Opened cart via JS scan")
            return
    except Exception:
        pass

    # Final fallback: navigate directly
    print("   ⚠️ Could not click cart icon — navigating to /cart")
    await page.goto("https://blinkit.com/cart", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)


# ─── Location Setup ──────────────────────────────────────────────────────────

async def _location_modal_visible(page=None) -> bool:
    """Returns True if the Blinkit location/address modal is currently shown."""
    page = page or browser_manager.page
    indicators = [
        "text=Please provide your delivery location",
        "[placeholder*='search delivery location']",
        "input[class*='LocationSearchBox']",
        "text=Detect my location",
    ]
    for sel in indicators:
        try:
            if await page.locator(sel).first.is_visible(timeout=1500):
                return True
        except Exception:
            pass
    return False


async def _set_delivery_location(address: str, page=None) -> None:
    """
    Types the address into Blinkit's location search field and selects
    the first dropdown suggestion.

    Primary selector confirmed from live DOM inspection:
        div[class*='LocationSearchList__LocationDetailContainer']
    """
    page = page or browser_manager.page
    print(f"[Blinkit] Setting delivery location to: '{address}'")

    # ── 1. Find and click the search input ───────────────────────────────
    input_selectors = [
        "input[class*='LocationSearchBox__InputSelect']",
        "[placeholder*='search delivery location']",
        "[placeholder*='Search delivery']",
        "[placeholder*='delivery location']",
        "input[type='search']",
        "input[type='text']",
    ]

    typed = False
    for sel in input_selectors:
        try:
            loc = page.locator(sel)
            if await loc.first.is_visible(timeout=2000):
                await loc.first.click(timeout=2000)
                await asyncio.sleep(0.5)
                # keyboard.type() fires real JS input events → triggers autocomplete
                # page.fill() bypasses these events and breaks autocomplete
                await page.keyboard.type(address, delay=80)
                typed = True
                print(f"   Typed address into: {sel}")
                break
        except Exception:
            continue

    if not typed:
        print("   ⚠️ Could not find address input — skipping location set")
        return

    # ── 2. Wait for dropdown, then click confirmed selector ───────────────
    SUGGESTION_SEL = "div[class*='LocationSearchList__LocationDetailContainer']"

    try:
        await page.wait_for_selector(SUGGESTION_SEL, timeout=6000)
        first_item = page.locator(SUGGESTION_SEL).first
        if await first_item.is_visible(timeout=3000):
            await first_item.click(timeout=3000)
            print("   ✅ Selected first location suggestion")
            await asyncio.sleep(3)
            return
    except Exception as e:
        print(f"   [Location] Primary selector failed: {e} — trying fallbacks...")

    # ── 3. Fallback: JS scan for any visible LocationSearchList element ───
    try:
        clicked = await page.evaluate("""() => {
            const candidates = [
                ...document.querySelectorAll('[class*="LocationSearchList"]'),
                ...document.querySelectorAll('[class*="LocationDetail"]'),
                ...document.querySelectorAll('[role="option"]'),
                ...document.querySelectorAll('[role="listbox"] > *'),
                ...document.querySelectorAll('ul li'),
            ];
            for (const el of candidates) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0 && el.innerText?.trim()) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""")
        if clicked:
            await asyncio.sleep(3)
            print("   ✅ Selected suggestion via JS scan")
            return
    except Exception:
        pass

    # ── 4. Last resort: ArrowDown + Enter ────────────────────────────────
    try:
        await page.keyboard.press("ArrowDown")
        await asyncio.sleep(0.4)
        await page.keyboard.press("Enter")
        await asyncio.sleep(3)
        print("   Selected suggestion via ArrowDown+Enter (last resort)")
    except Exception:
        pass


# ─── Add-to-Cart ─────────────────────────────────────────────────────────────

async def _click_first_add_button(page) -> bool:
    """
    Finds and clicks the first visible ADD button on a Blinkit search result page.

    CONFIRMED from live DOM inspection:
    - Blinkit uses <div role="button"> NOT <button> elements
    - The text is exactly "ADD" (all caps)
    """
    # Strategy 1: Playwright locator — div role=button with text ADD (most reliable)
    try:
        btn = page.get_by_role("button", name="ADD", exact=True)
        count = await btn.count()
        if count > 0 and await btn.first.is_visible(timeout=3000):
            await btn.first.click(timeout=5000)
            await asyncio.sleep(1)
            return True
    except Exception:
        pass

    # Strategy 2: JS scan — all div[role="button"] whose text is exactly "ADD"
    try:
        clicked = await page.evaluate("""() => {
            const candidates = [
                ...document.querySelectorAll('div[role="button"]'),
                ...document.querySelectorAll('button'),
            ];
            for (const el of candidates) {
                const txt = (el.innerText || el.textContent || '').trim();
                if (txt === 'ADD' || txt === 'Add') {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        el.click();
                        return true;
                    }
                }
            }
            return false;
        }""")
        if clicked:
            await asyncio.sleep(1)
            return True
    except Exception:
        pass

    # Strategy 3: Playwright get_by_text fallback (catches any casing)
    for text in ["ADD", "Add", "add to cart", "ADD TO CART"]:
        try:
            loc = page.get_by_text(text, exact=True)
            if await loc.first.is_visible(timeout=1500):
                await loc.first.click(timeout=3000)
                await asyncio.sleep(1)
                return True
        except Exception:
            continue

    return False


# ─── Rechecker ───────────────────────────────────────────────────────────────

async def _recheck_blinkit_cart(expected_items: list[str], page=None) -> dict:
    """Verifies expected items are present in the Blinkit cart."""
    if not expected_items:
        return {"confirmed_in_cart": [], "missing_from_cart": []}
    page = page or browser_manager.page
    try:
        await page.goto("https://blinkit.com/cart", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        cart_text = (await page.content()).lower()
        confirmed, missing = [], []
        for item in expected_items:
            keywords = item.lower().split()
            matches = sum(1 for kw in keywords if kw in cart_text)
            if matches >= max(1, len(keywords) // 2):
                confirmed.append(item)
            else:
                missing.append(item)
                print(f"   [Rechecker] ❌ '{item}' not found in Blinkit cart")
        return {"confirmed_in_cart": confirmed, "missing_from_cart": missing}
    except Exception as e:
        print(f"   [Rechecker] ⚠️ Could not verify Blinkit cart: {e}")
        return {"confirmed_in_cart": expected_items, "missing_from_cart": []}
