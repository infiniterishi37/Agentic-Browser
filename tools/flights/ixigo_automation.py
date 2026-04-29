"""
Ixigo flight booking automation helpers.

Flow:
1) Apply default sort preference (cheapest when available).
2) Click first visible flight option.
3) Proceed to booking/provider page.
"""

from __future__ import annotations

import asyncio

from tools.browser import browser_manager


async def proceed_with_first_ixigo_option() -> dict:
    """
    Attempts to sort results and proceed with the first bookable option.

    Returns:
        {
          "sorted": bool,
          "clicked": bool,
          "booking_url": str
        }
    """
    page = browser_manager.page
    if not page:
        return {"sorted": False, "clicked": False, "booking_url": ""}

    sorted_applied = await _apply_default_sort(page)
    clicked = await _click_first_bookable(page)
    await asyncio.sleep(2)
    booking_url = page.url if page else ""
    return {"sorted": sorted_applied, "clicked": clicked, "booking_url": booking_url}


async def _apply_default_sort(page) -> bool:
    # Prefer cheapest sort, fallback to any default sort control.
    selectors = [
        "[role='button']:has-text('Cheapest')",
        "button:has-text('Cheapest')",
        "[role='button']:has-text('Price')",
        "button:has-text('Price')",
        "[role='tab']:has-text('Cheapest')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                await loc.click(timeout=2500)
                await asyncio.sleep(1.5)
                return True
        except Exception:
            continue
    return False


async def _click_first_bookable(page) -> bool:
    # Ixigo and partner UIs vary, so keep a broad-but-prioritized selector list.
    selectors = [
        "button:has-text('Book')",
        "button:has-text('Select')",
        "a:has-text('Book')",
        "a:has-text('Select')",
        "[role='button']:has-text('Book')",
        "[role='button']:has-text('Select')",
        "button:has-text('View Deal')",
        "a:has-text('View Deal')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1200):
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=3000)
                return True
        except Exception:
            continue
    return False
