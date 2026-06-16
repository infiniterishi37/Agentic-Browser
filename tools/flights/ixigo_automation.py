"""
Ixigo flight booking automation helpers.

Flow:
0) Navigate DIRECTLY to Ixigo search results URL (primary — most reliable).
1) If results don't load, fall back to homepage form with city-name autocomplete
   and proper calendar navigation.
2) Apply cheapest sort and click first bookable option.
3) Proceed to booking/provider page.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from tools.browser import browser_manager


# ─── IATA ↔ City mapping ──────────────────────────────────────────────────────

IATA_TO_CITY: dict[str, str] = {
    "PNQ": "Pune", "DEL": "Delhi", "BOM": "Mumbai", "BLR": "Bengaluru",
    "HYD": "Hyderabad", "MAA": "Chennai", "CCU": "Kolkata", "AMD": "Ahmedabad",
    "GOI": "Goa", "IXR": "Ranchi", "PAT": "Patna", "LKO": "Lucknow",
    "COK": "Kochi", "CJB": "Coimbatore", "IDR": "Indore", "VNS": "Varanasi",
    "BHO": "Bhopal", "SXR": "Srinagar", "IXC": "Chandigarh", "ATQ": "Amritsar",
    "NAG": "Nagpur", "VTZ": "Visakhapatnam", "TRV": "Thiruvananthapuram",
    "GAU": "Guwahati", "IXA": "Agartala", "IMF": "Imphal", "IXZ": "Port Blair",
    "IXL": "Leh", "IXJ": "Jammu", "UDR": "Udaipur", "JDH": "Jodhpur",
    "DED": "Dehradun", "JAI": "Jaipur", "SLV": "Shimla",
}

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTH_SHORT = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _iata_to_city(iata: str) -> str:
    return IATA_TO_CITY.get((iata or "").upper(), iata)


# ─── Date helpers ─────────────────────────────────────────────────────────────

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
    """Build a direct Ixigo search results URL from booking state."""
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


def _has_ixigo_compact_date_format(url: str) -> bool:
    """Check that Ixigo URL uses DDMMYYYY date format."""
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


# ─── Results wait helper ──────────────────────────────────────────────────────

async def _wait_for_ixigo_results(page, timeout_secs: int = 10) -> bool:
    """Poll until bookable flight buttons appear (Ixigo renders async via React)."""
    for _ in range(timeout_secs):
        await asyncio.sleep(1.0)
        try:
            count = await page.evaluate(
                """() => {
                    const inChat = el => !!el.closest('#agentic-chat-root');
                    const isVisible = el => {
                        if (!el || inChat(el)) return false;
                        const r = el.getBoundingClientRect();
                        const cs = window.getComputedStyle(el);
                        return r.width > 8 && r.height > 8 && cs.display !== 'none' && cs.visibility !== 'hidden';
                    };
                    const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'))
                        .filter(isVisible)
                        .filter(el => {
                            const t = (el.innerText || el.textContent || '').toLowerCase().trim();
                            return t === 'book' || t === 'select' || t.startsWith('book ') || t.includes('view deal');
                        });
                    return btns.length;
                }"""
            )
            if isinstance(count, (int, float)) and count >= 1:
                return True
        except Exception:
            pass
    return False


# ─── Main entry: search on Ixigo ─────────────────────────────────────────────

async def open_ixigo_and_search_from_home(state: dict) -> dict:
    """
    Primary: navigate directly to the Ixigo results URL (fastest & most reliable).
    Fallback: try the homepage form with city-name autocomplete + calendar picker.
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
    page = browser_manager.page
    if not page:
        return result

    # ── Strategy 1: Direct results URL ────────────────────────────────────────
    await browser_manager.navigate(normalized_url)
    result["normalized_date_applied"] = True

    loaded = await _wait_for_ixigo_results(page, timeout_secs=10)
    if loaded:
        result["manual_search_submitted"] = True
        result["results_url"] = page.url
        return result

    # ── Strategy 2: Homepage form with autocomplete + calendar ────────────────
    result["fallback_used"] = True
    await browser_manager.navigate("https://www.ixigo.com/flights")
    result["opened_home"] = True
    await asyncio.sleep(2.5)
    await _dismiss_common_popups(page)
    await _set_trip_type(page, str(state.get("trip_type", "oneway")))

    origin_iata = str(state.get("origin", "")).upper()
    dest_iata = str(state.get("destination", "")).upper()
    depart_date = str(state.get("depart_date", ""))

    result["from_entered"] = await _set_location_field(
        page, "From", origin_iata, _iata_to_city(origin_iata)
    )
    await asyncio.sleep(0.5)
    result["to_entered"] = await _set_location_field(
        page, "To", dest_iata, _iata_to_city(dest_iata)
    )
    await asyncio.sleep(0.5)
    result["date_entered"] = await _set_departure_date(page, depart_date)

    if int(state.get("adults", 1) or 1) > 1:
        await _set_adults_count(page, int(state.get("adults", 1)))

    result["search_clicked"] = await _click_search_button(page)

    if result["search_clicked"]:
        await asyncio.sleep(5)
        result["results_url"] = page.url
        if "/search/result/flight" in (page.url or ""):
            result["manual_search_submitted"] = True
            if not _has_ixigo_compact_date_format(page.url):
                await browser_manager.navigate(normalized_url)
                await asyncio.sleep(3)
                result["normalized_date_applied"] = True
                result["results_url"] = page.url
            # Extra wait for React rendering after form search
            await _wait_for_ixigo_results(page, timeout_secs=6)
            return result

    # Last resort: re-navigate to direct URL
    await browser_manager.navigate(normalized_url)
    await _wait_for_ixigo_results(page, timeout_secs=8)
    result["results_url"] = page.url
    return result


# ─── Popup dismissal ──────────────────────────────────────────────────────────

async def _dismiss_common_popups(page) -> None:
    selectors = [
        "button[aria-label='Close']",
        "button:has-text('Close')",
        "button:has-text('No Thanks')",
        "button:has-text('No thanks')",
        "button:has-text('Later')",
        "[data-testid='close']",
        "[aria-label*='close' i]",
        "[aria-label*='dismiss' i]",
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


# ─── Trip type ────────────────────────────────────────────────────────────────

async def _set_trip_type(page, trip_type: str) -> bool:
    if trip_type != "return":
        return True
    selectors = [
        "button:has-text('Round Trip')",
        "button:has-text('Round trip')",
        "[role='button']:has-text('Round Trip')",
        "text=Round Trip",
        "[role='radio']:has-text('Round Trip')",
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


# ─── Location autocomplete ────────────────────────────────────────────────────

async def _set_location_field(page, field_label: str, iata: str, city_name: str = "") -> bool:
    """
    Open the From/To location field, type the city name (triggers autocomplete),
    then select the first matching dropdown option.
    """
    if not iata:
        return False

    # Prefer city name for autocomplete (Ixigo recognises city names better than raw IATA)
    search_text = city_name or iata

    # Step 1: open the field
    trigger_selectors = [
        f"[placeholder*='{field_label}' i]",
        f"input[placeholder*='city' i]",
        f"input[placeholder*='where' i]",
        f"input[placeholder*='from' i]" if field_label.lower() == "from" else "",
        f"input[placeholder*='to' i]" if field_label.lower() == "to" else "",
        f"button:has-text('{field_label}')",
        f"[role='button']:has-text('{field_label}')",
        f"[aria-label*='{field_label}' i]",
        f"text={field_label}",
    ]

    clicked_field = False
    for sel in [s for s in trigger_selectors if s]:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            if count < 1:
                continue
            for i in range(min(count, 4)):
                cand = loc.nth(i)
                if not await cand.is_visible(timeout=400):
                    continue
                in_chat = await cand.evaluate("el => !!el.closest('#agentic-chat-root')")
                if in_chat:
                    continue
                await cand.click(timeout=2000)
                clicked_field = True
                break
            if clicked_field:
                break
        except Exception:
            continue

    await asyncio.sleep(0.4)

    # Step 2: type city name into the focused input
    typed = await _type_into_any_visible_input(page, search_text)
    if not typed:
        return False

    await asyncio.sleep(0.9)  # wait for autocomplete dropdown

    # Step 3: select from dropdown (try iata code match first, then city name)
    for hint in [iata, city_name, search_text]:
        if not hint:
            continue
        option_selectors = [
            f"[role='option']:has-text('{hint}')",
            f"li:has-text('{hint}')",
            f"[role='listbox'] li:has-text('{hint}')",
            f"ul li:has-text('{hint}')",
            f"[class*='option']:has-text('{hint}')",
            f"[class*='suggestion']:has-text('{hint}')",
        ]
        for sel in option_selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    first = loc.first
                    if await first.is_visible(timeout=600):
                        in_chat = await first.evaluate("el => !!el.closest('#agentic-chat-root')")
                        if not in_chat:
                            await first.click(timeout=1500)
                            await asyncio.sleep(0.4)
                            return True
            except Exception:
                continue

    # Fallback: ArrowDown + Enter to accept first suggestion
    try:
        await page.keyboard.press("ArrowDown")
        await asyncio.sleep(0.2)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.4)
        return True
    except Exception:
        return False


# ─── Date picker ─────────────────────────────────────────────────────────────

async def _set_departure_date(page, depart_iso: str) -> bool:
    """
    Click the departure date field then select the date via the calendar widget.
    Falls back to typing the date if no calendar is found.
    """
    if not depart_iso:
        return False
    try:
        target = datetime.strptime(depart_iso, "%Y-%m-%d")
    except ValueError:
        return False

    # Open the date picker
    trigger_selectors = [
        "button:has-text('Departure')",
        "[role='button']:has-text('Departure')",
        "[aria-label*='departure' i]",
        "[aria-label*='depart' i]",
        "[placeholder*='departure' i]",
        "[placeholder*='depart' i]",
        "[class*='departure' i]",
        "text=Departure",
    ]
    opened = False
    for sel in trigger_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=700):
                in_chat = await loc.evaluate("el => !!el.closest('#agentic-chat-root')")
                if not in_chat:
                    await loc.click(timeout=2000)
                    opened = True
                    await asyncio.sleep(0.6)
                    break
        except Exception:
            continue

    if not opened:
        # Try typing directly into a date input
        typed = await _type_into_any_visible_input(page, _to_display_date(depart_iso))
        if typed:
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.3)
        return typed

    # Navigate the calendar to the right month/day
    return await _navigate_calendar_and_select_day(page, target.year, target.month, target.day)


async def _navigate_calendar_and_select_day(page, year: int, month: int, day: int) -> bool:
    """Navigate the visible calendar widget to `year/month` then click `day`."""
    target_names = {MONTH_NAMES[month].lower(), MONTH_SHORT[month].lower()}

    for _attempt in range(24):  # max 24 month advances
        await asyncio.sleep(0.35)

        # Read the header of the currently visible calendar
        try:
            header_text: str = await page.evaluate(
                """() => {
                    const inChat = el => !!el.closest('#agentic-chat-root');
                    const isVisible = el => {
                        if (!el || inChat(el)) return false;
                        const r = el.getBoundingClientRect();
                        const cs = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && cs.display !== 'none';
                    };
                    const calSels = [
                        '.DayPicker', '[class*="calendar"]', '[class*="Calendar"]',
                        '[class*="datepicker"]', '[class*="DatePicker"]',
                        '[class*="picker"]', '[role="dialog"]'
                    ];
                    let cal = null;
                    for (const s of calSels) {
                        const els = Array.from(document.querySelectorAll(s)).filter(el => isVisible(el));
                        if (els.length) { cal = els[0]; break; }
                    }
                    if (!cal) return '';
                    const hdrs = Array.from(cal.querySelectorAll(
                        '[class*="caption"], [class*="header"], [class*="Header"], [class*="month-header"], caption, .DayPicker-Caption'
                    )).filter(isVisible);
                    return hdrs.map(el => (el.innerText || el.textContent || '').trim()).join(' ');
                }"""
            )
        except Exception:
            header_text = ""

        header_lower = (header_text or "").lower()
        month_ok = any(name in header_lower for name in target_names)
        year_ok = str(year) in header_lower

        if month_ok and year_ok:
            # We're on the right month — click the day
            if await _click_calendar_day(page, day):
                return True
            await asyncio.sleep(0.4)
            return await _click_calendar_day(page, day)

        # Navigate forward one month
        if not await _click_calendar_next_month(page):
            break

    # Last-ditch: try clicking the day wherever we are
    return await _click_calendar_day(page, day)


async def _click_calendar_next_month(page) -> bool:
    """Click the 'next month' navigation arrow in the visible calendar."""
    try:
        clicked: bool = await page.evaluate(
            """() => {
                const inChat = el => !!el.closest('#agentic-chat-root');
                const isVisible = el => {
                    if (!el || inChat(el)) return false;
                    const r = el.getBoundingClientRect();
                    const cs = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && cs.display !== 'none';
                };
                const calSels = [
                    '.DayPicker', '[class*="calendar"]', '[class*="Calendar"]',
                    '[class*="datepicker"]', '[class*="DatePicker"]',
                    '[class*="picker"]', '[role="dialog"]'
                ];
                let cal = null;
                for (const s of calSels) {
                    const els = Array.from(document.querySelectorAll(s)).filter(el => isVisible(el));
                    if (els.length) { cal = els[0]; break; }
                }
                if (!cal) cal = document.body;

                const nextPatterns = [
                    '[aria-label*="next month" i]', '[aria-label*="forward" i]',
                    'button[class*="next" i]', '[class*="next-month"]',
                    '[class*="navNext"]', '[class*="DayPicker-NavButton--next"]',
                    '[class*="nextMonth"]',
                ];
                for (const pat of nextPatterns) {
                    const el = cal.querySelector(pat);
                    if (el && isVisible(el)) { el.click(); return true; }
                }

                // Fallback: buttons whose text is a right-arrow glyph
                const btns = Array.from(cal.querySelectorAll('button')).filter(isVisible);
                for (const btn of btns) {
                    const t = (btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '').trim();
                    if (['>', '›', '→', '▶', '»', 'Next'].includes(t)) { btn.click(); return true; }
                }
                // Last resort: the last visible button in the calendar is usually "next"
                if (btns.length >= 2) { btns[btns.length - 1].click(); return true; }
                return false;
            }"""
        )
        return bool(clicked)
    except Exception:
        return False


async def _click_calendar_day(page, day: int) -> bool:
    """Click the cell for `day` in the currently visible calendar month."""
    try:
        clicked: bool = await page.evaluate(
            """(targetDay) => {
                const inChat = el => !!el.closest('#agentic-chat-root');
                const isVisible = el => {
                    if (!el || inChat(el)) return false;
                    const r = el.getBoundingClientRect();
                    const cs = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
                };
                const calSels = [
                    '.DayPicker', '[class*="calendar"]', '[class*="Calendar"]',
                    '[class*="datepicker"]', '[class*="DatePicker"]',
                    '[class*="picker"]', '[role="dialog"]', 'table'
                ];
                let cal = null;
                for (const s of calSels) {
                    const els = Array.from(document.querySelectorAll(s)).filter(el => isVisible(el));
                    if (els.length) { cal = els[0]; break; }
                }
                if (!cal) cal = document.body;

                const cellSels = [
                    '[role="gridcell"]',
                    '[class*="day"]:not([class*="name"]):not([class*="label"]):not([class*="header"])',
                    '[class*="Day"]:not([class*="NavButton"])',
                    'td', '[class*="date-cell"]',
                ];
                for (const sel of cellSels) {
                    const cells = Array.from(cal.querySelectorAll(sel)).filter(isVisible);
                    for (const cell of cells) {
                        const txt = (cell.innerText || cell.textContent || '').trim();
                        if (parseInt(txt) !== targetDay) continue;
                        const cls = (cell.className || '').toString().toLowerCase();
                        const ariaDisabled = cell.getAttribute('aria-disabled') || '';
                        if (cell.hasAttribute('disabled') || ariaDisabled === 'true') continue;
                        if (cls.includes('disabled') || cls.includes('prev-month') ||
                            cls.includes('next-month') || cls.includes('outside') ||
                            cls.includes('past')) continue;
                        cell.scrollIntoView({ block: 'center' });
                        cell.click();
                        return true;
                    }
                }
                return false;
            }""",
            day,
        )
        return bool(clicked)
    except Exception:
        return False


# ─── Adults count ─────────────────────────────────────────────────────────────

async def _set_adults_count(page, adults: int) -> bool:
    if adults <= 1:
        return True
    opened = False
    open_selectors = [
        "button:has-text('Travellers')",
        "button:has-text('Traveller')",
        "button:has-text('Passengers')",
        "[role='button']:has-text('Travellers')",
        "[role='button']:has-text('Passenger')",
    ]
    for sel in open_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=700):
                await loc.click(timeout=1500)
                await asyncio.sleep(0.3)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return False

    for _ in range(adults - 1):
        plus_selectors = [
            "button[aria-label*='Adult'][aria-label*='increase']",
            "button[aria-label*='adult'][aria-label*='+']",
            "button:has-text('+')",
            "[role='button']:has-text('+')",
        ]
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

    for sel in ["button:has-text('Done')", "button:has-text('Apply')", "[role='button']:has-text('Done')"]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=600):
                await loc.click(timeout=1200)
                break
        except Exception:
            continue
    return True


# ─── Search button ────────────────────────────────────────────────────────────

async def _click_search_button(page) -> bool:
    selectors = [
        "button:has-text('Search')",
        "button:has-text('Search Flights')",
        "[role='button']:has-text('Search')",
        "[role='button']:has-text('Search Flights')",
        "a:has-text('Search')",
        "input[type='submit']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=900):
                in_chat = await loc.evaluate("el => !!el.closest('#agentic-chat-root')")
                if not in_chat:
                    await loc.scroll_into_view_if_needed()
                    await loc.click(timeout=2000)
                    return True
        except Exception:
            continue
    return False


# ─── Generic input typing ─────────────────────────────────────────────────────

async def _type_into_any_visible_input(page, text: str) -> bool:
    if not text:
        return False
    selectors = [
        "input[placeholder*='city' i]",
        "input[placeholder*='airport' i]",
        "input[placeholder*='search' i]",
        "input[placeholder*='depart' i]",
        "input[placeholder*='from' i]",
        "input[placeholder*='to' i]",
        "input[placeholder*='where' i]",
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
                    in_chat = await cand.evaluate("el => !!el.closest('#agentic-chat-root')")
                    if in_chat:
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
    # Last resort: type at keyboard level
    try:
        await page.keyboard.type(text, delay=55)
        return True
    except Exception:
        return False


# ─── Ixigo result automation ──────────────────────────────────────────────────

async def proceed_with_first_ixigo_option() -> dict:
    """Sort results by cheapest and click the first bookable option."""
    page = browser_manager.page
    if not page:
        return {"sorted": False, "clicked": False, "booking_url": ""}

    sorted_applied = await _apply_default_sort(page)
    await asyncio.sleep(1.5)
    clicked = await _click_first_bookable(page)
    await asyncio.sleep(2.5)
    booking_url = page.url if page else ""
    return {"sorted": sorted_applied, "clicked": clicked, "booking_url": booking_url}


async def _apply_default_sort(page) -> bool:
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
    selectors = [
        "button:text-is('Book')",
        "a:text-is('Book')",
        "[role='button']:text-is('Book')",
        "button:has-text('Book')",
        "a:has-text('Book')",
        "[role='button']:has-text('Book')",
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
