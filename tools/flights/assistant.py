"""
Flight booking assistant flow for chat-driven interaction.

Collects minimum flight details, opens Ixigo comparison, and then
opens the selected booking partner URL based on user choice.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from difflib import get_close_matches
from urllib.parse import urlencode

from tools.browser import browser_manager
from tools.chat_server import chat_server
from agent.llm_provider import get_llm
from tools.flights.ixigo_automation import proceed_with_first_ixigo_option


def _default_state() -> dict:
    return {
        "active": False,
        "stage": "idle",  # idle | collecting | compared | done
        "origin": "",
        "destination": "",
        "depart_date": "",
        "return_date": "",
        "trip_type": "oneway",
        "adults": 1,
        "auto_book": False,
        "options": [],
    }


def _is_flight_intent(text: str) -> bool:
    t = (text or "").lower()
    if "train" in t or "rail" in t:
        return False

    # Default booking mode is flight unless user explicitly asks for train.
    generic_booking_signals = [
        "book",
        "booking",
        "ticket",
        "tickets",
    ]
    if any(k in t for k in generic_booking_signals):
        return True

    keywords = [
        "flight",
        "flights",
        "ticket",
        "book ticket",
        "book flight",
        "ixigo",
        "airfare",
    ]
    return any(k in t for k in keywords)


def _norm_date(raw: str) -> str:
    raw = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return ""


CITY_TO_IATA = {
    # India (common routes)
    "pune": "PNQ",
    "delhi": "DEL",
    "new delhi": "DEL",
    "mumbai": "BOM",
    "mumbali": "BOM",
    "bombay": "BOM",
    "bangalore": "BLR",
    "bengaluru": "BLR",
    "hyderabad": "HYD",
    "chennai": "MAA",
    "kolkata": "CCU",
    "ahmedabad": "AMD",
    "goa": "GOI",
}

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _norm_place(raw: str) -> str:
    token = (raw or "").strip().lower()
    if re.fullmatch(r"[a-z]{3}", token):
        return token.upper()
    if token in CITY_TO_IATA:
        return CITY_TO_IATA[token]
    # Fuzzy fallback for minor typos like "mumbali" or "delih".
    close = get_close_matches(token, CITY_TO_IATA.keys(), n=1, cutoff=0.78)
    if close:
        return CITY_TO_IATA[close[0]]
    return ""


def _parse_human_date(text: str) -> str:
    """
    Parse formats like '5th May', '5 May', 'May 5', with optional year.
    Returns YYYY-MM-DD or empty string.
    """
    s = (text or "").strip().lower()
    today = date.today()

    patterns = [
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-zA-Z]+)(?:\s+(\d{4}))?\b",
        r"\b([a-zA-Z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?\b",
    ]
    for pat in patterns:
        m = re.search(pat, s, flags=re.IGNORECASE)
        if not m:
            continue
        if pat.startswith(r"\b(\d"):
            day_raw, mon_raw, year_raw = m.group(1), m.group(2), m.group(3)
        else:
            mon_raw, day_raw, year_raw = m.group(1), m.group(2), m.group(3)

        mon = MONTHS.get(mon_raw.lower())
        if not mon:
            continue
        day = int(day_raw)
        year = int(year_raw) if year_raw else today.year
        try:
            parsed = date(year, mon, day)
            # If year omitted and date already passed, roll to next year.
            if not year_raw and parsed < today:
                parsed = date(year + 1, mon, day)
            return parsed.isoformat()
        except ValueError:
            continue
    return ""


def _extract_fields(text: str) -> dict:
    out: dict[str, str | int] = {}
    s = (text or "").strip()

    # Prefer IATA codes for reliability.
    m = re.search(r"\bfrom\s+([A-Za-z]{3})\b", s, flags=re.IGNORECASE)
    if m:
        out["origin"] = m.group(1).upper()
    else:
        m = re.search(r"\bfrom\s+([A-Za-z ]{2,40})\s+\bto\b", s, flags=re.IGNORECASE)
        if m:
            place = _norm_place(m.group(1))
            if place:
                out["origin"] = place
    m = re.search(r"\bto\s+([A-Za-z]{3})\b", s, flags=re.IGNORECASE)
    if m:
        out["destination"] = m.group(1).upper()
    else:
        m = re.search(r"\bto\s+([A-Za-z ]{2,40})(?:\s+\bon\b|\s+\bfor\b|$)", s, flags=re.IGNORECASE)
        if m:
            place = _norm_place(m.group(1))
            if place:
                out["destination"] = place

    m = re.search(r"\b(?:on|depart|departure)\s+(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b", s, flags=re.IGNORECASE)
    if m:
        d = _norm_date(m.group(1))
        if d:
            out["depart_date"] = d
    if not out.get("depart_date"):
        d = _parse_human_date(s)
        if d:
            out["depart_date"] = d

    m = re.search(r"\b(?:return|inbound)\s+(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b", s, flags=re.IGNORECASE)
    if m:
        d = _norm_date(m.group(1))
        if d:
            out["return_date"] = d
            out["trip_type"] = "return"
    if not out.get("return_date"):
        m = re.search(r"\b(?:return|inbound)\b(.+)$", s, flags=re.IGNORECASE)
        if m:
            d = _parse_human_date(m.group(1))
            if d:
                out["return_date"] = d
                out["trip_type"] = "return"

    if re.search(r"\b(one way|oneway)\b", s, flags=re.IGNORECASE):
        out["trip_type"] = "oneway"
        out["return_date"] = ""
    if re.search(r"\b(round trip|return trip|roundtrip)\b", s, flags=re.IGNORECASE):
        out["trip_type"] = "return"

    m = re.search(r"\b(\d{1,2})\s+(adult|adults|passenger|passengers)\b", s, flags=re.IGNORECASE)
    if m:
        out["adults"] = max(1, int(m.group(1)))

    if re.search(r"\b(book it for me|book for me|proceed automatically|auto book|without manual)\b", s, flags=re.IGNORECASE):
        out["auto_book"] = True

    return out


async def _llm_extract_fields(text: str) -> dict:
    """
    LLM fallback parser for natural-language flight booking details.
    Returns normalized partial fields.
    """
    try:
        llm = get_llm()
        prompt = (
            "Extract flight-booking fields from user text.\n"
            "Return ONLY strict JSON with keys:\n"
            "origin, destination, depart_date, return_date, trip_type, adults, auto_book.\n"
            "Rules:\n"
            "- origin/destination must be 3-letter IATA if inferable; else empty string.\n"
            "- depart_date/return_date must be YYYY-MM-DD; infer year reasonably if missing.\n"
            "- trip_type is 'oneway' or 'return'.\n"
            "- adults is integer >=1.\n"
            "- auto_book true if user implies 'book it for me' or automatic flow.\n"
            f"User text: {text}"
        )
        resp = await llm.ainvoke(prompt)
        raw = (resp.content or "").strip()
        data = json.loads(raw)
        out: dict = {}
        origin = str(data.get("origin", "")).strip().upper()
        destination = str(data.get("destination", "")).strip().upper()
        if re.fullmatch(r"[A-Z]{3}", origin):
            out["origin"] = origin
        if re.fullmatch(r"[A-Z]{3}", destination):
            out["destination"] = destination
        depart_date = _norm_date(str(data.get("depart_date", "")).strip())
        if depart_date:
            out["depart_date"] = depart_date
        return_date = _norm_date(str(data.get("return_date", "")).strip())
        if return_date:
            out["return_date"] = return_date
            out["trip_type"] = "return"
        trip_type = str(data.get("trip_type", "")).strip().lower()
        if trip_type in {"oneway", "return"}:
            out["trip_type"] = trip_type
        adults = data.get("adults")
        if isinstance(adults, int) and adults >= 1:
            out["adults"] = adults
        if bool(data.get("auto_book", False)):
            out["auto_book"] = True
        return out
    except Exception:
        return {}


def _missing_fields(state: dict) -> list[str]:
    missing = []
    if not state.get("origin"):
        missing.append("origin")
    if not state.get("destination"):
        missing.append("destination")
    if not state.get("depart_date"):
        missing.append("depart_date")
    if state.get("trip_type") == "return" and not state.get("return_date"):
        missing.append("return_date")
    return missing


def _build_ixigo_url(state: dict) -> str:
    origin = state["origin"].upper()
    destination = state["destination"].upper()
    depart = state["depart_date"]
    base = "https://www.ixigo.com/search/result/flight"
    query = {
        "from": origin,
        "to": destination,
        "date": depart,
        "adults": state.get("adults", 1),
        "children": 0,
        "infants": 0,
        "class": "e",
        "source": "Search Form",
    }
    if state.get("trip_type") == "return" and state.get("return_date"):
        query["returnDate"] = state["return_date"]
    return f"{base}?{urlencode(query)}"


async def _extract_booking_options() -> list[dict]:
    await asyncio.sleep(5)
    page = browser_manager.page
    if not page:
        return []
    options = await page.evaluate(
        """() => {
            const out = [];
            const nodes = Array.from(document.querySelectorAll('a[href]'));
            for (const a of nodes) {
                const href = a.href || '';
                if (!href.startsWith('http')) continue;
                if (href.includes('ixigo.com')) continue;
                const txt = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
                if (!txt) continue;
                if (txt.length < 2) continue;
                if (out.some(x => x.url === href)) continue;
                out.push({label: txt.slice(0, 80), url: href});
                if (out.length >= 5) break;
            }
            return out;
        }"""
    )
    return options or []


async def _open_first_available_flight_offer() -> None:
    """
    Try to open a flight offer card so provider links become visible.
    """
    page = browser_manager.page
    if not page:
        return
    selectors = [
        "button:has-text('Select')",
        "button:has-text('View deal')",
        "button:has-text('Book')",
        "[role='button']:has-text('Select')",
        "a:has-text('Select')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.click(timeout=3000)
                await asyncio.sleep(2.5)
                return
        except Exception:
            continue


def _merge_state(state: dict, update: dict) -> dict:
    for k, v in update.items():
        state[k] = v
    return state


def _parse_choice(text: str, max_n: int) -> int:
    m = re.search(r"\b(?:option|pick|choose|go with)\s*(\d+)\b", text, flags=re.IGNORECASE)
    if not m:
        return 0
    idx = int(m.group(1))
    if 1 <= idx <= max_n:
        return idx
    return 0


def _is_new_flight_request(text: str) -> bool:
    t = (text or "").lower().strip()
    return _is_flight_intent(t) and ("from " in t and " to " in t)


async def handle_flight_chat_message(message: str) -> bool:
    """
    Returns True if the message was handled by flight assistant flow.
    """
    text = (message or "").strip()
    state = chat_server.flight_booking_state or _default_state()
    chat_server.flight_booking_state = state

    # Exit command
    if state.get("active") and text.lower() in {"cancel flight", "cancel booking", "stop flight flow"}:
        chat_server.flight_booking_state = _default_state()
        await chat_server.send_to_browser("Flight booking flow cancelled.", "system")
        return True

    is_intent = _is_flight_intent(text)
    if not state.get("active") and not is_intent:
        return False

    # Reset stale flow on a clearly new flight request.
    if state.get("active") and _is_new_flight_request(text):
        state = _default_state()
        chat_server.flight_booking_state = state

    # Start or continue flow
    if not state.get("active"):
        state["active"] = True
        state["stage"] = "collecting"
        await chat_server.send_to_browser(
            "I can help with flight booking. I will compare on Ixigo first, then open your chosen booking site.",
            "agent",
        )

    state = _merge_state(state, _extract_fields(text))
    missing_after_rules = _missing_fields(state)
    if missing_after_rules:
        # LLM reasoning fallback for ambiguous city/date phrasing.
        llm_fields = await _llm_extract_fields(text)
        if llm_fields:
            state = _merge_state(state, llm_fields)

    # If already compared, check choice
    if state.get("stage") == "compared":
        options = state.get("options", [])
        if state.get("auto_book") and options:
            await chat_server.send_to_browser(
                "Auto-book mode is on. Applying default sorting and proceeding with the first available option.",
                "status",
            )
            result = await proceed_with_first_ixigo_option()
            await chat_server.send_to_browser(
                (
                    "Booking step executed."
                    f" Sorted: {'yes' if result.get('sorted') else 'no'}"
                    f", First option clicked: {'yes' if result.get('clicked') else 'no'}."
                ),
                "agent",
            )
            state["stage"] = "done"
            return True
        choice = _parse_choice(text, len(options))
        direct_url = re.search(r"https?://\\S+", text)
        if choice:
            selected = options[choice - 1]
            await chat_server.send_to_browser(
                f"Opening option {choice}: {selected.get('label', 'selected provider')}", "status"
            )
            await browser_manager.navigate(selected["url"])
            await chat_server.send_to_browser(
                "I opened the provider website. Share passenger details in chat and I will continue booking.",
                "agent",
            )
            state["stage"] = "done"
            return True
        if direct_url:
            await chat_server.send_to_browser("Opening your provided booking URL now.", "status")
            await browser_manager.navigate(direct_url.group(0))
            await chat_server.send_to_browser(
                "Provider site is open. Share passenger details in chat and I will proceed.",
                "agent",
            )
            state["stage"] = "done"
            return True
        await chat_server.send_to_browser(
            "Please choose an option like 'option 1' or paste the booking URL you want.",
            "agent",
        )
        return True

    missing = _missing_fields(state)
    if missing:
        prompts = {
            "origin": "departure airport IATA code (example: DEL)",
            "destination": "destination airport IATA code (example: DXB)",
            "depart_date": "departure date in YYYY-MM-DD",
            "return_date": "return date in YYYY-MM-DD",
        }
        ask = ", ".join(prompts[m] for m in missing)
        await chat_server.send_to_browser(
            f"Please provide: {ask}. You can send in one line, e.g. 'from DEL to DXB on 2026-05-15'.",
            "agent",
        )
        return True

    # Run comparison
    url = _build_ixigo_url(state)
    await chat_server.send_to_browser("Opening Ixigo and loading flight comparison.", "status")
    await browser_manager.navigate(url)
    options = await _extract_booking_options()
    if not options:
        # Try one automatic interaction to expose provider links.
        await _open_first_available_flight_offer()
        options = await _extract_booking_options()
    state["options"] = options
    state["stage"] = "compared"

    summary = [
        "Flight comparison is ready on Ixigo.",
        f"Route: {state['origin']} → {state['destination']}",
        f"Date: {state['depart_date']}" + (f" | Return: {state['return_date']}" if state.get("return_date") else ""),
        f"Passengers: {state.get('adults', 1)} adult(s)",
    ]
    if options:
        summary.append("Top booking options found:")
        for i, opt in enumerate(options, start=1):
            summary.append(f"{i}. {opt.get('label', 'Option')} — {opt.get('url')}")
        summary.append("Reply with 'option 1' (or another number) to continue booking on that website.")
        if state.get("auto_book"):
            summary.append("Auto-book is enabled. I will now sort by default and proceed with first option.")
            await chat_server.send_to_browser("\n".join(summary), "agent")
            await chat_server.send_to_browser(
                "Applying default sorting and proceeding with first option on Ixigo.",
                "status",
            )
            result = await proceed_with_first_ixigo_option()
            await chat_server.send_to_browser(
                (
                    "Auto-book execution complete."
                    f" Sorted: {'yes' if result.get('sorted') else 'no'}"
                    f", First option clicked: {'yes' if result.get('clicked') else 'no'}."
                ),
                "agent",
            )
            state["stage"] = "done"
            return True
    else:
        summary.append("I could not extract provider links automatically. Please pick a result in browser and paste its URL here.")

    await chat_server.send_to_browser("\n".join(summary), "agent")
    return True
