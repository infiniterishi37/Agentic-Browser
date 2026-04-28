"""
Multi-Platform Shopping Coordinator
====================================
Runs shopping automation on Amazon, Flipkart, and Blinkit sequentially
for a shared shopping list, then opens all three cart pages simultaneously
after all three shopping runs have completed.

Usage:
    # High-level: Google search → 3 platforms → comparison
    from tools.shopping.coordinator import run_shopping_flow
    asyncio.run(run_shopping_flow("buy milk and bread"))

    # Low-level: known item list → 3 platforms
    from tools.shopping.coordinator import run_all_shops
    asyncio.run(run_all_shops(["milk", "bread", "eggs"]))
"""

import asyncio
from tools.browser import browser_manager
from tools.shopping.amazon import shop_on_amazon
from tools.shopping.flipkart import shop_on_flipkart
from tools.shopping.blinkit import shop_on_blinkit
from tools.shopping.google_items import search_items_on_google
from tools.chat_server import chat_server


CART_URLS = {
    "amazon": "https://www.amazon.in/gp/cart/view.html",
    "flipkart": "https://www.flipkart.com/viewcart",
    "blinkit": "https://blinkit.com/cart",
}


async def run_all_shops(items: list[str]) -> dict:
    """
    Sequentially shops on Amazon → Flipkart → Blinkit for the given item list.
    After ALL three shopping runs are complete, opens all three cart pages in
    separate browser tabs.

    Args:
        items: List of item names to search for and add to cart.

    Returns:
        A summary dict containing results from each platform and a list of
        any items that were unavailable on each platform.
    """
    print("\n" + "═" * 60)
    print("  🛒  Multi-Platform Shopping Coordinator")
    print("═" * 60)
    print(f"  Items: {items}")
    print("═" * 60)
    await chat_server.send_to_browser(
        f"I am starting your shopping review across Amazon, Flipkart, and Blinkit for {len(items)} items.",
        "status",
    )

    context = browser_manager.page.context
    pages = {
        "amazon": browser_manager.page,
        "flipkart": await context.new_page(),
        "blinkit": await context.new_page(),
    }

    print("\n[Coordinator] ▶ Phase 1 — Parallel run on Amazon + Flipkart + Blinkit")
    await chat_server.send_to_browser(
        "Running all three websites in parallel now. I will aggregate the results once all three finish.",
        "status",
    )
    amazon_task = shop_on_amazon(items, page=pages["amazon"])
    flipkart_task = shop_on_flipkart(items, page=pages["flipkart"])
    blinkit_task = shop_on_blinkit(items, page=pages["blinkit"])
    amazon_res, flipkart_res, blinkit_res = await asyncio.gather(
        amazon_task, flipkart_task, blinkit_task
    )
    results = {
        "amazon": amazon_res,
        "flipkart": flipkart_res,
        "blinkit": blinkit_res,
    }

    # ── Phase 4: Open all three carts ────────────────────────────────────
    print("\n[Coordinator] ▶ Phase 2 — Opening all carts")
    await chat_server.send_to_browser(
        "Parallel run is complete. Opening all carts so you can review platform-level outcomes.",
        "status",
    )
    await _open_all_carts()

    # ── Summary Report ───────────────────────────────────────────────────
    _print_summary(results)
    await chat_server.send_to_browser(build_chat_summary(results, items), "agent")

    return results


async def _open_all_carts() -> None:
    """
    Opens Amazon, Flipkart, and Blinkit carts each in a dedicated browser tab.
    The first cart loads in the current tab; subsequent carts open as new tabs.
    """
    context = browser_manager.page.context
    cart_platforms = list(CART_URLS.items())

    # Load the first cart in the existing page
    platform, url = cart_platforms[0]
    print(f"   Opening {platform} cart in current tab...")
    await browser_manager.navigate(url)
    await asyncio.sleep(2)

    # Open remaining carts in new tabs
    for platform, url in cart_platforms[1:]:
        print(f"   Opening {platform} cart in new tab...")
        new_page = await context.new_page()
        await new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)

    print("[Coordinator] ✅ All three carts are open.")


def _print_summary(results: dict) -> None:
    """Prints a formatted summary of the shopping run."""
    print("\n" + "═" * 60)
    print("  📋  Shopping Summary")
    print("═" * 60)

    any_unavailable = False
    for platform, data in results.items():
        added = data.get("added", [])
        unavailable = data.get("unavailable", [])
        symbol = "✅" if not unavailable else "⚠️ "
        print(f"\n  {symbol} {platform.capitalize()}")
        print(f"     Added     : {added if added else '(none)'}")
        if unavailable:
            any_unavailable = True
            print(f"     ❌ Not available: {unavailable}")

    if any_unavailable:
        print(
            "\n  ℹ️  Some items were unavailable. See per-platform breakdown above."
        )
    else:
        print("\n  🎉  All items were successfully added on all platforms!")
    print("═" * 60 + "\n")


def build_chat_summary(results: dict, all_items: list[str]) -> str:
    """Create a concise chat summary with totals and best affordability pick."""
    total_requested = len(all_items)
    lines = [
        "Shopping review complete across 3 websites.",
        f"Total requested items: {total_requested}",
    ]

    scores: dict[str, int] = {}
    for platform, data in results.items():
        added = data.get("added", [])
        unavailable = data.get("unavailable", [])
        scores[platform] = len(added)
        lines.append(
            f"- {platform.capitalize()}: added {len(added)}, unavailable {len(unavailable)}"
        )

    cheapest_platform, cheapest_reason = _choose_cheapest_platform(results, scores)
    lines.append(f"Cheapest option: {cheapest_platform.capitalize()} ({cheapest_reason}).")
    return "\n".join(lines)


def _choose_cheapest_platform(results: dict, scores: dict[str, int]) -> tuple[str, str]:
    """
    Pick cheapest platform if explicit totals are available; otherwise fallback to
    best fulfillment score as proxy and clearly label it as an estimate.
    """
    totals: dict[str, float] = {}
    for platform, data in results.items():
        total = data.get("estimated_total")
        if isinstance(total, (int, float)) and total > 0:
            totals[platform] = float(total)

    if totals:
        platform = min(totals, key=totals.get)
        return platform, f"estimated cart total {totals[platform]:.2f}"

    platform = max(scores, key=scores.get) if scores else "amazon"
    score = scores.get(platform, 0)
    return platform, f"estimated by highest fulfillment score {score}"


def _print_comparison_table(results: dict, all_items: list[str]) -> None:
    """
    Prints a side-by-side comparison table showing which items were
    successfully added on each platform.

    Example:
        Item         | Amazon | Flipkart | Blinkit
        ------------ | ------ | -------- | -------
        milk         |   ✅   |    ✅    |   ✅
        bread        |   ✅   |    ❌    |   ✅
    """
    platforms = list(results.keys())  # ["amazon", "flipkart", "blinkit"]

    # Build lookup: platform → set of added items (lowercase for matching)
    added_sets: dict[str, set] = {}
    for platform, data in results.items():
        added_sets[platform] = {i.lower() for i in data.get("added", [])}

    col_w = 14  # item column width
    plat_w = 10  # per-platform column width

    header = f"  {'Item':<{col_w}}"
    divider = f"  {'-' * col_w}"
    for p in platforms:
        header += f" | {p.capitalize():^{plat_w}}"
        divider += f"-+-{'-' * plat_w}"

    print("\n" + "═" * (col_w + (plat_w + 3) * len(platforms) + 4))
    print("  🛒  Platform Comparison")
    print("═" * (col_w + (plat_w + 3) * len(platforms) + 4))
    print(header)
    print(divider)

    for item in all_items:
        row = f"  {item:<{col_w}}"
        for p in platforms:
            # Match by checking if any word of item is in the added set keyword
            item_words = item.lower().split()
            found = any(
                any(w in added_word for w in item_words)
                for added_word in added_sets[p]
            ) or item.lower() in added_sets[p]
            symbol = "✅" if found else "❌"
            row += f" | {symbol:^{plat_w}}"
        print(row)

    print("═" * (col_w + (plat_w + 3) * len(platforms) + 4) + "\n")


async def run_shopping_flow(
    query: str, llm_provider: str = "", llm_model: str = ""
) -> dict:
    """
    Full automated shopping flow — no LLM required.

    Steps:
        1. Google search for *query* to extract/confirm item list
        2. Run Amazon → Flipkart → Blinkit coordinator
        3. Print a side-by-side comparison table

    Args:
        query: Natural-language shopping request, e.g.
               "buy milk and bread" or "milk, eggs, bread"

    Returns:
        Dict of per-platform results (same structure as run_all_shops).
    """
    print("\n" + "═" * 60)
    print("  🔍  Shopping Flow — Starting")
    print("═" * 60)
    print(f"  Query: {query}")
    print("═" * 60)

    # ── Step 1: Resolve item list ─────────────────────────────────────────
    print("\n[Shopping Flow] ▶ Step 1 — Google search for items")
    await chat_server.send_to_browser(
        "First step: resolving your shopping list from the query before platform execution.",
        "status",
    )
    items = await search_items_on_google(
        query, llm_provider=llm_provider, llm_model=llm_model
    )
    print(f"[Shopping Flow] ✅ Items resolved: {items}")
    await chat_server.send_to_browser(
        f"Item resolution complete. I found {len(items)} items and will start parallel shopping now.",
        "status",
    )

    # ── Step 2: Shop all three platforms ─────────────────────────────────
    results = await run_all_shops(items)

    # ── Step 3: Comparison table ──────────────────────────────────────────
    _print_comparison_table(results, items)
    await chat_server.send_to_browser(
        "Review completed. Sharing final summary with totals and cheapest option now.",
        "status",
    )

    return results
