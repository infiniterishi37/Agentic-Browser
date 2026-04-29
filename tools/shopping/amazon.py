import asyncio
from tools.browser import browser_manager


async def shop_on_amazon(items: list[str], page=None) -> dict:
    """
    Automates shopping on Amazon for a list of items.
    Returns a dict with 'added' and 'unavailable' item lists.
    """
    print(f"\n[Amazon] Starting shopping run for: {items}")
    added_items = []
    unavailable_items = []
    extracted_data = {}  # Store extracted details keyed by item_name

    # 1. Open Amazon
    page = page or browser_manager.page

    async def _goto(url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

    print("[Amazon] Navigating to Amazon.in...")
    await _goto("https://www.amazon.in")
    await asyncio.sleep(2)

    for item_name in items:
        print(f"\n[Amazon] Processing item: {item_name}")
        try:
            # ── Search ──────────────────────────────────────────────────────
            search_box = "input[id='twotabsearchtextbox']"
            await page.fill(search_box, item_name, timeout=5000)
            await page.keyboard.press("Enter")
            await asyncio.sleep(5)

            # ── Click first result ────────────────────────────────────────
            print("   Clicking product link...")
            context = page.context
            initial_count = len(context.pages)

            await page.click(
                "div[data-component-type='s-search-result'] a.a-link-normal",
                timeout=2000,
            )
            await asyncio.sleep(5)

            # ── Handle new tab ───────────────────────────────────────────
            new_tab_opened = False
            target_page = page

            if len(context.pages) > initial_count:
                new_tab_opened = True
                target_page = context.pages[-1]
                await target_page.bring_to_front()
                print("   Switched to new tab...")

            # ── Extract Product Data ─────────────────────────────────────
            product_url = target_page.url
            title = "N/A"
            price = "N/A"
            image_url = ""
            try:
                # Attempt to extract title
                if await target_page.is_visible("#productTitle", timeout=2000):
                    title = await target_page.inner_text("#productTitle")
                    title = title.strip()
                # Attempt to extract price
                price_sel = ".a-price-whole"
                if await target_page.is_visible(price_sel, timeout=2000):
                    price = await target_page.inner_text(price_sel)
                    price = price.strip()
                # Attempt to extract image
                img_sel = "#landingImage"
                if await target_page.is_visible(img_sel, timeout=2000):
                    image_url = await target_page.get_attribute(img_sel, "src")
            except Exception as e:
                print(f"   ⚠️ Could not extract some details: {e}")

            extracted_data[item_name] = {
                "query": item_name,
                "title": title if title != "N/A" else item_name,
                "price": price,
                "image_url": image_url or "",
                "url": product_url
            }

            # ── Add to Cart ──────────────────────────────────────────────
            print("   Attempting 'Add to Cart'...")
            added = False
            selectors = [
                "#add-to-cart-button",
                "input[name='submit.add-to-cart']",
                "#add-to-cart-ubp-button",
                "[name='submit.addToCart']",
                "#av-quantity-form input[type='submit']",
                "span[id='submit.add-to-cart'] input",
                "input[aria-labelledby='submit.add-to-cart-announce']",
                "#freshAddToCartButton input",
                "input[name='submit.add-to-cart-main']",
                ".a-button-input[type='submit'][value='Add to cart']",
            ]

            # Scroll to trigger lazy loading
            await target_page.evaluate("window.scrollBy(0, 400)")
            await asyncio.sleep(2)

            for sel in selectors:
                try:
                    if await target_page.is_visible(sel, timeout=3000):
                        print(f"   Found button: {sel}")
                        await target_page.click(sel, timeout=5000)
                        added = True
                        print(f"   ✅ Added '{item_name}' to Amazon cart")
                        await asyncio.sleep(2)
                        break
                except Exception:
                    continue

            # ── Fallback: See All Buying Options ─────────────────────────
            if not added:
                try:
                    if await target_page.is_visible(
                        "a[href*='/gp/offer-listing']", timeout=3000
                    ):
                        print("   Using 'See All Buying Options' fallback...")
                        await target_page.click(
                            "a[href*='/gp/offer-listing']", timeout=2000
                        )
                        await asyncio.sleep(3)
                        await target_page.click(
                            "input[name='submit.addToCart']", timeout=3000
                        )
                        added = True
                        print(f"   ✅ Added '{item_name}' to Amazon cart (via Options)")
                except Exception:
                    pass

            if added:
                added_items.append(extracted_data[item_name])
            else:
                unavailable_items.append(item_name)
                print(f"   ❌ Item not available / could not add: '{item_name}'")

            # ── Cleanup tab ──────────────────────────────────────────────
            if new_tab_opened:
                await target_page.close()
                await page.bring_to_front()

            await asyncio.sleep(2)

        except Exception as e:
            print(f"   ⚠️ Error processing '{item_name}': {e}")
            unavailable_items.append(item_name)

    # ── Rechecker: verify cart items ─────────────────────────────────────
    print("\n[Amazon] Running cart rechecker...")
    # For rechecker we pass the queries
    expected_queries = [item["query"] for item in added_items]
    verified_result = await _recheck_amazon_cart(expected_queries, page=page)
    
    # Filter added_items based on verified result
    confirmed_items = [item for item in added_items if item["query"] in verified_result["confirmed_in_cart"]]
    
    truly_unavailable = list(
        set(unavailable_items) | set(verified_result["missing_from_cart"])
    )

    result = {
        "platform": "amazon",
        "added": confirmed_items,
        "unavailable": truly_unavailable,
        "cart_url": "https://www.amazon.in/gp/cart/view.html",
    }

    if truly_unavailable:
        print(f"\n[Amazon] ⚠️  Items not available: {truly_unavailable}")
    print(f"[Amazon] ✅ Shopping run complete. Added {len(result['added'])} items.")
    return result


async def _recheck_amazon_cart(expected_items: list[str], page=None) -> dict:
    """
    Navigates to the Amazon cart and verifies each expected item is present.
    Returns confirmed and missing item lists.
    """
    if not expected_items:
        return {"confirmed_in_cart": [], "missing_from_cart": []}

    page = page or browser_manager.page
    try:
        await page.goto(
            "https://www.amazon.in/gp/cart/view.html",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

        cart_content = await page.content()
        cart_text = cart_content.lower()

        confirmed = []
        missing = []
        for item in expected_items:
            # Simple keyword presence check
            keywords = item.lower().split()
            # At least half the keywords must appear
            matches = sum(1 for kw in keywords if kw in cart_text)
            if matches >= max(1, len(keywords) // 2):
                confirmed.append(item)
            else:
                missing.append(item)
                print(f"   [Rechecker] ❌ '{item}' not found in Amazon cart")

        return {"confirmed_in_cart": confirmed, "missing_from_cart": missing}
    except Exception as e:
        print(f"   [Rechecker] ⚠️ Could not verify Amazon cart: {e}")
        return {"confirmed_in_cart": expected_items, "missing_from_cart": []}
