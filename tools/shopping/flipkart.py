"""
Flipkart shopping automation.

Key design decisions:
- Plain search URL (no filter param) — the in-stock filter was returning
  false "no results" for valid items like bread/eggs.
- Product link: extract href via JS, then navigate with page.goto() so
  Playwright properly waits for the page instead of using JS a.click()
  which returns immediately without awaiting navigation.
- Add-to-Cart detection: text-based only (no CSS class names) because
  Flipkart regenerates class names on every deploy.
"""

import asyncio
import urllib.parse
from tools.browser import browser_manager


async def shop_on_flipkart(items: list[str], page=None) -> dict:
    """
    Automates shopping on Flipkart for a list of items.
    Returns a dict with 'added' and 'unavailable' item lists.
    """
    print(f"\n[Flipkart] Starting shopping run for: {items}")
    page = page or browser_manager.page
    added_items = []
    unavailable_items = []

    print("[Flipkart] Navigating to Flipkart.com...")
    await page.goto("https://www.flipkart.com", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # Dismiss login popup by text
    try:
        close = page.get_by_role("button", name="✕")
        await close.click(timeout=2000)
        await asyncio.sleep(1)
        print("[Flipkart] Dismissed login popup.")
    except Exception:
        pass

    for item_name in items:
        print(f"\n[Flipkart] Processing item: {item_name}")
        try:
            # ── 1. Search ────────────────────────────────────────────────
            query = urllib.parse.quote_plus(item_name)
            search_url = f"https://www.flipkart.com/search?q={query}"
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            # Wait for page to fully settle before reading content
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)

            # ── 2. Check for empty results ────────────────────────────────
            page_text = (await page.content()).lower()
            if "no results found" in page_text or "0 results" in page_text:
                print(f"   ❌ No results for '{item_name}'")
                unavailable_items.append(item_name)
                continue

            # ── 3. Try Add-to-Cart directly from search grid ──────────────
            # Some Flipkart categories (groceries) show ATC on the grid card.
            print("   Trying direct Add-to-Cart from search grid...")
            added = await _click_atc_by_text(page)

            # ── 4. If not added, open product page via goto() ─────────────
            if not added:
                print("   Navigating to first product page...")
                product_href = await _get_first_product_href(page)

                if not product_href:
                    print(f"   ❌ No product link found for '{item_name}'")
                    unavailable_items.append(item_name)
                    continue

                # Navigate properly so Playwright awaits the page load
                print(f"   Opening: {product_href[:80]}...")
                await page.goto(
                    product_href, wait_until="domcontentloaded", timeout=30000
                )
                await asyncio.sleep(4)

                # Check if out of stock on the product page
                pdp_text = (await page.content()).lower()
                if "currently out of stock" in pdp_text or "sold out" in pdp_text:
                    print(f"   ❌ '{item_name}' is out of stock on this listing")
                    unavailable_items.append(item_name)
                    continue

                # Scroll to make lazy-loaded buttons appear
                await page.evaluate("window.scrollBy(0, 500)")
                await asyncio.sleep(2)

                added = await _click_atc_by_text(page)

            if added:
                added_items.append(item_name)
                print(f"   ✅ Added '{item_name}' to Flipkart cart")
            else:
                unavailable_items.append(item_name)
                print(f"   ❌ No 'Add to Cart' button found for '{item_name}'")

            await asyncio.sleep(2)

        except Exception as e:
            print(f"   ⚠️ Error processing '{item_name}': {e}")
            unavailable_items.append(item_name)

    # ── Rechecker ─────────────────────────────────────────────────────────
    print("\n[Flipkart] Running cart rechecker...")
    verified = await _recheck_flipkart_cart(added_items, page=page)
    truly_unavailable = list(set(unavailable_items) | set(verified["missing_from_cart"]))

    result = {
        "platform": "flipkart",
        "added": verified["confirmed_in_cart"],
        "unavailable": truly_unavailable,
        "cart_url": "https://www.flipkart.com/viewcart",
    }

    if truly_unavailable:
        print(f"\n[Flipkart] ⚠️  Items not available: {truly_unavailable}")
    print(f"[Flipkart] ✅ Shopping run complete. Added: {result['added']}")
    return result


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _get_first_product_href(page) -> str | None:
    """
    Extracts the href of the first product link on the search page.
    Uses JS to scan all anchors for ones pointing to product pages
    (/p/ prefix or pid= param), returns the absolute URL string.
    Does NOT click — caller uses page.goto() to navigate properly.
    """
    try:
        href = await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href]'));
            for (const a of links) {
                const href = a.href || '';
                // Product pages contain /p/ in the path or pid= in query
                if ((href.includes('/p/') || href.includes('pid='))
                    && !href.includes('flipkart.com/pages/')
                    && a.offsetParent !== null) {
                    return href;
                }
            }
            return null;
        }""")
        return href
    except Exception:
        return None


async def _click_atc_by_text(page) -> bool:
    """
    Finds and clicks 'Add to Cart' by visible text — immune to class changes.
    Three strategies in order of reliability.
    """
    # Strategy 1: Playwright get_by_role (most reliable)
    for text in ["Add to cart", "ADD TO CART", "Add To Cart"]:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if await btn.first.is_visible(timeout=2000):
                await btn.first.scroll_into_view_if_needed()
                await btn.first.click(timeout=5000)
                await asyncio.sleep(1)
                return True
        except Exception:
            pass

    # Strategy 2: JS scan of all buttons + role=button elements
    try:
        clicked = await page.evaluate("""() => {
            const candidates = [
                ...document.querySelectorAll('button'),
                ...document.querySelectorAll('[role="button"]'),
            ];
            for (const el of candidates) {
                const t = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (t.includes('add to cart')) {
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

    # Strategy 3: Playwright locator by text (catches spans/divs styled as buttons)
    try:
        loc = page.get_by_text("Add to cart", exact=False)
        count = await loc.count()
        for i in range(count):
            try:
                el = loc.nth(i)
                if await el.is_visible(timeout=1000):
                    await el.click(timeout=3000)
                    await asyncio.sleep(1)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


async def _recheck_flipkart_cart(expected_items: list[str], page=None) -> dict:
    """Verifies expected items are present in the Flipkart cart."""
    if not expected_items:
        return {"confirmed_in_cart": [], "missing_from_cart": []}
    page = page or browser_manager.page
    try:
        await page.goto("https://www.flipkart.com/viewcart", wait_until="domcontentloaded", timeout=30000)
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
                print(f"   [Rechecker] ❌ '{item}' not found in Flipkart cart")
        return {"confirmed_in_cart": confirmed, "missing_from_cart": missing}
    except Exception as e:
        print(f"   [Rechecker] ⚠️ Could not verify Flipkart cart: {e}")
        return {"confirmed_in_cart": expected_items, "missing_from_cart": []}
