"""
Ixigo flight booking automation helpers.

Flow:
0) Open Ixigo flights homepage and run manual form search.
1) Apply default sort preference (cheapest when available).
2) Click first visible flight option.
3) Proceed to booking/provider page.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from tools.browser import browser_manager


def _to_ixigo_compact_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to DDMMYYYY for Ixigo query URLs."""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d%m%Y")
    except Exception:
        return (iso_date or "").replace("-", "")


def _to_display_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to DD/MM/YYYY for UI typing attempts."""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return iso_date


def build_ixigo_results_url(state: dict) -> str:
    """Fallback URL format requested by user (date as DDMMYYYY)."""
    origin = str(state.get("origin", "")).upper()
    destination = str(state.get("destination", "")).upper()
    depart = _to_ixigo_compact_date(str(state.get("depart_date", "")))
    url = (
        "https://www.ixigo.com/search/result/flight"
        f"?from={origin}&to={destination}&date={depart}"
        f"&adults={int(state.get('adults', 1) or 1)}"
        "&children=0&infants=0&class=e&source=Search+Form"
    )
    if state.get("trip_type") == "return" and state.get("return_date"):
        url += f"&returnDate={_to_ixigo_compact_date(str(state.get('return_date', '')))}"
    return url


async def open_ixigo_and_search_from_home(state: dict) -> dict:
    """
    Open ixigo.com flights page and try filling the form manually.
    Falls back to formatted search URL if dynamic form interaction fails.
    """
    result = {
        "opened_home": False,
        "from_entered": False,
        "to_entered": False,
        "date_entered": False,
        "search_clicked": False,
        "manual_search_submitted": False,
        "fallback_used": False,
        "normalized_date_applied": False,
        "results_url": "",
    }
    normalized_url = build_ixigo_results_url(state)

    await browser_manager.navigate("https://www.ixigo.com/flights")
    page = browser_manager.page
    if not page:
        return result
    result["opened_home"] = True

    await asyncio.sleep(2.2)
    await _dismiss_common_popups(page)
    await _set_trip_type(page, str(state.get("trip_type", "oneway")))

    origin = str(state.get("origin", "")).upper()
    destination = str(state.get("destination", "")).upper()
    depart_date = str(state.get("depart_date", ""))

    result["from_entered"] = await _set_location_field(page, "From", origin)
    result["to_entered"] = await _set_location_field(page, "To", destination)
    result["date_entered"] = await _set_departure_date(page, depart_date)

    if int(state.get("adults", 1) or 1) > 1:
        await _set_adults_count(page, int(state.get("adults", 1)))

    result["search_clicked"] = await _click_search_button(page)
    if result["search_clicked"]:
        await asyncio.sleep(4)
        result["results_url"] = page.url
        if "/search/result/flight" in (page.url or ""):
            result["manual_search_submitted"] = True
            if not _has_ixigo_compact_date_format(page.url):
                await browser_manager.navigate(normalized_url)
                await asyncio.sleep(2)
                result["normalized_date_applied"] = True
                result["results_url"] = browser_manager.page.url if browser_manager.page else normalized_url
            return result

    await browser_manager.navigate(normalized_url)
    await asyncio.sleep(2)
    result["fallback_used"] = True
    result["results_url"] = browser_manager.page.url if browser_manager.page else normalized_url
    return result


def _has_ixigo_compact_date_format(url: str) -> bool:
    """
    Validate Ixigo query date style:
    - date=DDMMYYYY
    - returnDate=DDMMYYYY (if present)
    """
    try:
        qs = parse_qs(urlparse(url).query)
    except Exception:
        return False

    date_val = (qs.get("date") or [""])[0]
    if not date_val or not date_val.isdigit() or len(date_val) != 8:
        return False

    return_val = (qs.get("returnDate") or [""])[0]
    if return_val and (not return_val.isdigit() or len(return_val) != 8):
        return False

    return True


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


async def _dismiss_common_popups(page) -> None:
    selectors = [
        "button[aria-label='Close']",
        "button:has-text('Close')",
        "button:has-text('No Thanks')",
        "button:has-text('No thanks')",
        "button:has-text('Later')",
        "[data-testid='close']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                await loc.click(timeout=1500)
                await asyncio.sleep(0.2)
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _set_trip_type(page, trip_type: str) -> bool:
    if trip_type != "return":
        return True
    selectors = [
        "button:has-text('Round Trip')",
        "button:has-text('Round trip')",
        "[role='button']:has-text('Round Trip')",
        "text=Round Trip",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                await loc.click(timeout=1800)
                await asyncio.sleep(0.4)
                return True
        except Exception:
            continue
    return False


async def _set_location_field(page, field_label: str, iata: str) -> bool:
    if not iata:
        return False
    trigger_selectors = [
        f"button:has-text('{field_label}')",
        f"[role='button']:has-text('{field_label}')",
        f"text={field_label}",
    ]
    for sel in trigger_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                await loc.click(timeout=1800)
                await asyncio.sleep(0.35)
                break
        except Exception:
            continue

    typed = await _type_into_any_visible_input(page, iata)
    if not typed:
        return False

    await asyncio.sleep(0.7)
    option_selectors = [
        f"[role='option']:has-text('{iata}')",
        f"li:has-text('{iata}')",
        f"div:has-text('{iata}')",
    ]
    for sel in option_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                await loc.click(timeout=1500)
                await asyncio.sleep(0.35)
                return True
        except Exception:
            continue

    try:
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.35)
        return True
    except Exception:
        return False


async def _set_departure_date(page, depart_iso: str) -> bool:
    if not depart_iso:
        return False
    display_date = _to_display_date(depart_iso)

    trigger_selectors = [
        "button:has-text('Departure')",
        "button:has-text('Depart')",
        "[role='button']:has-text('Departure')",
        "text=Departure",
    ]
    for sel in trigger_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=700):
                await loc.click(timeout=1600)
                await asyncio.sleep(0.35)
                break
        except Exception:
            continue

    typed = await _type_into_any_visible_input(page, display_date)
    if typed:
        try:
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.35)
            return True
        except Exception:
            return True

    try:
        day = int(datetime.strptime(depart_iso, "%Y-%m-%d").strftime("%d"))
        day_selectors = [
            f"button:has-text('{day}')",
            f"[aria-label*='{day}']",
            f"[role='gridcell']:has-text('{day}')",
        ]
        for sel in day_selectors:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=700):
                await loc.click(timeout=1500)
                await asyncio.sleep(0.35)
                return True
    except Exception:
        pass

    return False


async def _set_adults_count(page, adults: int) -> bool:
    if adults <= 1:
        return True
    opened = False
    open_selectors = [
        "button:has-text('Travellers')",
        "button:has-text('Traveller')",
        "button:has-text('Passengers')",
        "[role='button']:has-text('Travellers')",
    ]
    for sel in open_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=700):
                await loc.click(timeout=1500)
                await asyncio.sleep(0.25)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return False

    increments_needed = adults - 1
    plus_selectors = [
        "button[aria-label*='Adult'][aria-label*='increase']",
        "button:has-text('+')",
        "[role='button']:has-text('+')",
    ]
    for _ in range(increments_needed):
        clicked = False
        for sel in plus_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=600):
                    await loc.click(timeout=1200)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            break
        await asyncio.sleep(0.1)

    done_selectors = [
        "button:has-text('Done')",
        "button:has-text('Apply')",
        "[role='button']:has-text('Done')",
    ]
    for sel in done_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=600):
                await loc.click(timeout=1200)
                break
        except Exception:
            continue
    return True


async def _click_search_button(page) -> bool:
    selectors = [
        "button:has-text('Search')",
        "button:has-text('Search Flights')",
        "[role='button']:has-text('Search')",
        "a:has-text('Search')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=900):
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


async def _type_into_any_visible_input(page, text: str) -> bool:
    if not text:
        return False
    selectors = [
        "input[placeholder*='city' i]",
        "input[placeholder*='airport' i]",
        "input[placeholder*='search' i]",
        "input[placeholder*='depart' i]",
        "input[type='search']",
        "input[type='text']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            if count < 1:
                continue
            for i in range(min(count, 8)):
                cand = loc.nth(i)
                try:
                    if not await cand.is_visible(timeout=300):
                        continue
                    parent_id = await cand.evaluate(
                        """(el) => {
                            const root = el.closest('#agentic-chat-root');
                            return root ? root.id : '';
                        }"""
                    )
                    if parent_id:
                        continue
                    await cand.click(timeout=900)
                    try:
                        await cand.fill("")
                    except Exception:
                        pass
                    await cand.type(text, delay=55)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    try:
        await page.keyboard.type(text, delay=55)
        return True
    except Exception:
        return False


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
    # Prefer real "Book" actions and avoid "Lock Price" CTA.
    selectors = [
        "button:text-is('Book')",
        "a:text-is('Book')",
        "[role='button']:text-is('Book')",
        "button:has-text('Book')",
        "a:has-text('Book')",
        "[role='button']:has-text('Book')",
        # Fallbacks when a provider uses non-book phrasing.
        "button:text-is('Select')",
        "a:text-is('Select')",
        "[role='button']:text-is('Select')",
        "button:has-text('View Deal')",
        "a:has-text('View Deal')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            if count < 1:
                continue
            for i in range(min(count, 8)):
                cand = loc.nth(i)
                if not await cand.is_visible(timeout=600):
                    continue
                txt = (await cand.inner_text(timeout=600)).strip().lower()
                if "lock price" in txt:
                    continue
                await cand.scroll_into_view_if_needed()
                await cand.click(timeout=3000)
                return True
        except Exception:
            continue
    return False
