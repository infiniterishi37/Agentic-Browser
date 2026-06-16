"""
Universal Booking Assistant

Works with any booking website (flights, trains, buses, hotels, events).
Uses LLM-guided form filling so it adapts to any site's structure.

Flow:
  idle → collecting_travel → collecting_passenger
       → navigating → comparing → booking → done
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, timedelta
from difflib import get_close_matches

from tools.browser import browser_manager
from tools.chat_server import chat_server
from agent.llm_provider import get_llm

# ─── Site Registry ────────────────────────────────────────────────────────────

SITE_REGISTRY: dict[str, dict] = {
    "makemytrip": {"url": "https://www.makemytrip.com/flights/", "type": "flight", "name": "MakeMyTrip"},
    "mmt": {"url": "https://www.makemytrip.com/flights/", "type": "flight", "name": "MakeMyTrip"},
    "goibibo": {"url": "https://www.goibibo.com/flights/", "type": "flight", "name": "Goibibo"},
    "cleartrip": {"url": "https://www.cleartrip.com/flights/", "type": "flight", "name": "Cleartrip"},
    "yatra": {"url": "https://www.yatra.com/flights", "type": "flight", "name": "Yatra"},
    "indigo": {"url": "https://www.goindigo.in", "type": "flight", "name": "IndiGo"},
    "air india": {"url": "https://www.airindia.com", "type": "flight", "name": "Air India"},
    "airindia": {"url": "https://www.airindia.com", "type": "flight", "name": "Air India"},
    "spicejet": {"url": "https://www.spicejet.com", "type": "flight", "name": "SpiceJet"},
    "akasa": {"url": "https://www.akasaair.com", "type": "flight", "name": "Akasa Air"},
    "easemytrip": {"url": "https://www.easemytrip.com/flights", "type": "flight", "name": "EaseMyTrip"},
    "paytm": {"url": "https://paytm.com/travel/flights", "type": "flight", "name": "Paytm"},
    "irctc": {"url": "https://www.irctc.co.in/nget/train-search", "type": "train", "name": "IRCTC"},
    "redbus": {"url": "https://www.redbus.in", "type": "bus", "name": "RedBus"},
    "abhibus": {"url": "https://www.abhibus.com", "type": "bus", "name": "AbhiBus"},
    "bookmyshow": {"url": "https://in.bookmyshow.com", "type": "event", "name": "BookMyShow"},
    "bms": {"url": "https://in.bookmyshow.com", "type": "event", "name": "BookMyShow"},
    "oyo": {"url": "https://www.oyorooms.com", "type": "hotel", "name": "OYO"},
    "airbnb": {"url": "https://www.airbnb.com", "type": "hotel", "name": "Airbnb"},
    "trivago": {"url": "https://www.trivago.in", "type": "hotel", "name": "Trivago"},
    "travolook": {"url": "https://www.travolook.in", "type": "bus", "name": "Travolook"},
    "railyatri": {"url": "https://www.railyatri.in", "type": "train", "name": "RailYatri"},
}

BOOKING_TYPE_KEYWORDS: dict[str, list[str]] = {
    "flight": ["flight", "flights", "airfare", "fly", "airline", "plane", "air ticket"],
    "train": ["train", "trains", "rail", "railway", "express", "shatabdi", "rajdhani", "duronto"],
    "bus": ["bus", "buses", "coach", "volvo", "sleeper bus"],
    "hotel": ["hotel", "hotels", "stay", "accommodation", "hostel", "resort", "room"],
    "event": ["movie", "film", "cinema", "concert", "show", "match", "game", "event ticket"],
}

CITY_TO_IATA = {
    "pune": "PNQ", "delhi": "DEL", "new delhi": "DEL", "mumbai": "BOM",
    "bombay": "BOM", "bangalore": "BLR", "bengaluru": "BLR", "hyderabad": "HYD",
    "chennai": "MAA", "madras": "MAA", "kolkata": "CCU", "calcutta": "CCU",
    "ahmedabad": "AMD", "goa": "GOI", "ranchi": "IXR", "jaipur": "JAI",
    "lucknow": "LKO", "kochi": "COK", "cochin": "COK", "coimbatore": "CJB",
    "indore": "IDR", "patna": "PAT", "varanasi": "VNS", "bhopal": "BHO",
    "srinagar": "SXR", "chandigarh": "IXC", "amritsar": "ATQ", "nagpur": "NAG",
    "visakhapatnam": "VTZ", "vizag": "VTZ", "thiruvananthapuram": "TRV",
    "trivandrum": "TRV", "guwahati": "GAU", "agartala": "IXA", "imphal": "IMF",
    "port blair": "IXZ", "leh": "IXL", "jammu": "IXJ", "shimla": "SLV",
    "udaipur": "UDR", "jodhpur": "JDH", "dehradun": "DED",
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Fields required for each booking type
TRAVEL_FIELDS_REQUIRED: dict[str, list[str]] = {
    "flight": ["origin", "destination", "depart_date"],
    "train": ["origin", "destination", "depart_date"],
    "bus": ["origin", "destination", "depart_date"],
    "hotel": ["destination", "checkin_date", "checkout_date"],
    "event": ["event_name", "city"],
}

TRAVEL_FIELD_PROMPTS: dict[str, str] = {
    "origin": "departure city",
    "destination": "destination city",
    "depart_date": "travel date (e.g. 25 June)",
    "checkin_date": "check-in date",
    "checkout_date": "check-out date",
    "event_name": "movie or event name",
    "city": "city",
}

PASSENGER_FIELD_PROMPTS: dict[str, str] = {
    "full_name": "full name",
    "email": "email address",
    "phone": "mobile number",
}

# ─── State ────────────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "active": False,
        "booking_type": "",
        "target_site": "",
        "target_url": "",
        "target_name": "",
        # stages: idle | collecting_travel | collecting_passenger | navigating | comparing | booking | done
        "stage": "idle",
        # travel
        "origin": "",
        "destination": "",
        "depart_date": "",
        "return_date": "",
        "trip_type": "oneway",
        "adults": 1,
        "event_name": "",
        "city": "",
        "checkin_date": "",
        "checkout_date": "",
        # passenger
        "passenger_details": {},
        "passenger_collected": False,
        # flow
        "options": [],
        "selected_option": {},
        "login_prompted": False,
        "post_login_action": "",
        "cancellation_prompted": False,
        "cancellation_choice": "no",
        "cancellation_applied": False,
        "search_attempts": 0,
    }


# ─── Detection helpers ────────────────────────────────────────────────────────

def _detect_site(text: str) -> tuple[str, str, str, str]:
    """Returns (site_key, url, booking_type, display_name)."""
    t = text.lower()
    for site_key, info in SITE_REGISTRY.items():
        if site_key in t:
            return site_key, info["url"], info["type"], info["name"]
    return "", "", "", ""


def _detect_booking_type(text: str) -> str:
    t = text.lower()
    for btype, keywords in BOOKING_TYPE_KEYWORDS.items():
        if any(k in t for k in keywords):
            return btype
    return ""


def _is_booking_intent(text: str) -> bool:
    t = (text or "").lower()
    booking_words = ["book", "booking", "ticket", "tickets", "reserve", "reservation"]
    has_booking = any(w in t for w in booking_words)
    has_site = any(s in t for s in SITE_REGISTRY)
    return has_booking or has_site


def _should_handle_universally(text: str) -> bool:
    """Return True if this belongs to the universal handler (not the Ixigo flight handler)."""
    t = text.lower()

    # If a specific non-Ixigo site is mentioned
    for site_key, info in SITE_REGISTRY.items():
        if site_key in t:
            return True  # Universal handles ALL explicit site mentions

    # Non-flight booking types
    for btype in ["train", "bus", "hotel", "event"]:
        if any(k in t for k in BOOKING_TYPE_KEYWORDS.get(btype, [])):
            return True

    return False


# ─── Field extraction ─────────────────────────────────────────────────────────

def _norm_place(raw: str) -> str:
    """Return IATA code if recognized city/code, else the cleaned title-case name."""
    token = re.sub(r"[^a-z ]", " ", (raw or "").strip().lower())
    token = re.sub(r"\s+", " ", token).strip()
    if not token:
        return ""
    if re.fullmatch(r"[a-z]{3}", token):
        return token.upper()
    if token in CITY_TO_IATA:
        return CITY_TO_IATA[token]
    close = get_close_matches(token, CITY_TO_IATA.keys(), n=1, cutoff=0.80)
    if close:
        return CITY_TO_IATA[close[0]]
    return ""  # Don't return garbage; caller handles empty


def _norm_place_strict(raw: str) -> str:
    """Like _norm_place but tries progressively shorter prefixes to find a match."""
    if not raw:
        return ""
    words = re.sub(r"[^a-zA-Z ]", " ", raw).split()
    for take in range(min(4, len(words)), 0, -1):
        code = _norm_place(" ".join(words[:take]))
        if code:
            return code
    # Return single-word title-case if no IATA found (for non-Indian cities)
    if len(words) == 1:
        return words[0].title()
    return ""


def _parse_human_date(text: str) -> str:
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
            if not year_raw and parsed < today:
                parsed = date(year + 1, mon, day)
            return parsed.isoformat()
        except ValueError:
            continue
    return ""


def _parse_relative_date(text: str) -> str:
    s = (text or "").strip().lower()
    today = date.today()
    if re.search(r"\b(day after tomorrow)\b", s):
        return (today + timedelta(days=2)).isoformat()
    if re.search(r"\b(tomorrow|tommorow|tmrw|tmr)\b", s):
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"\b(today|todays)\b", s):
        return today.isoformat()
    return ""


def _norm_date(raw: str) -> str:
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return ""


def _extract_date_from_text(text: str) -> str:
    date_pat = r"\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4}|\d{8}"
    m = re.search(date_pat, text)
    if m:
        d = _norm_date(m.group(0))
        if d:
            return d
    d = _parse_relative_date(text)
    if d:
        return d
    return _parse_human_date(text)


def _extract_travel_fields(text: str, booking_type: str = "flight") -> dict:
    out: dict = {}
    # Strip email addresses before processing so they don't confuse city/name extraction
    s = re.sub(r"\S+@\S+\.\S+", "", text or "").strip()
    t = s.lower()

    # Origin / destination for travel bookings
    if booking_type in ("flight", "train", "bus"):
        # "from X to Y" pattern
        m = re.search(r"\bfrom\s+([A-Za-z ]{2,40})\s+to\s+([A-Za-z ]{2,40})\b", s, re.IGNORECASE)
        if m:
            origin = _norm_place_strict(m.group(1).strip())
            dest = _norm_place_strict(m.group(2).strip())
            if origin:
                out["origin"] = origin
            if dest:
                out["destination"] = dest
        else:
            # "X to Y" pattern — be strict so we don't grab trailing words
            m2 = re.search(
                r"\b([A-Za-z]{2,20}(?:\s+[A-Za-z]{2,20}){0,2})\s+to\s+([A-Za-z]{2,20}(?:\s+[A-Za-z]{2,20}){0,2})\b",
                s, re.IGNORECASE
            )
            if m2:
                # Use strict matching — only accept known city/IATA
                origin = _norm_place_strict(m2.group(1).strip())
                dest = _norm_place_strict(m2.group(2).strip())
                if origin:
                    out["origin"] = origin
                if dest:
                    out["destination"] = dest

    # Destination for hotels/events
    if booking_type in ("hotel",):
        m = re.search(r"\b(?:in|at|to|near)\s+([A-Za-z ]{2,40})\b", s, re.IGNORECASE)
        if m:
            out["destination"] = m.group(1).strip().title()
        if not out.get("destination"):
            m2 = re.search(r"\b(?:hotel|stay|room)\s+(?:in|at)?\s*([A-Za-z ]{2,40})\b", s, re.IGNORECASE)
            if m2:
                out["destination"] = m2.group(1).strip().title()

    # City for events
    if booking_type == "event":
        m = re.search(r"\b(?:in|at)\s+([A-Za-z ]{2,30})\b", s, re.IGNORECASE)
        if m:
            out["city"] = m.group(1).strip().title()
        # Event/movie name
        m2 = re.search(r"\b(?:movie|film|show|event|match)\s+['\"]?([A-Za-z0-9 ]{2,60})['\"]?\b", s, re.IGNORECASE)
        if m2:
            out["event_name"] = m2.group(1).strip().title()

    # Departure / travel date
    d = _extract_date_from_text(s)
    if d:
        if booking_type in ("flight", "train", "bus"):
            out["depart_date"] = d
        elif booking_type == "hotel":
            out["checkin_date"] = d

    # Return date
    m = re.search(r"\b(?:return|back|checkout|check.?out)\s+(?:on\s+)?(.{3,20})\b", s, re.IGNORECASE)
    if m:
        d2 = _extract_date_from_text(m.group(1))
        if d2:
            if booking_type in ("flight", "train", "bus"):
                out["return_date"] = d2
                out["trip_type"] = "return"
            elif booking_type == "hotel":
                out["checkout_date"] = d2

    # Adults / passengers
    m = re.search(r"\b(\d{1,2})\s+(?:adult|adults|passenger|passengers|pax|person|people)\b", s, re.IGNORECASE)
    if m:
        out["adults"] = max(1, int(m.group(1)))

    # Trip type
    if re.search(r"\b(oneway|one.?way)\b", s, re.IGNORECASE):
        out["trip_type"] = "oneway"
        out["return_date"] = ""
    if re.search(r"\b(round.?trip|return.?trip|roundtrip)\b", s, re.IGNORECASE):
        out["trip_type"] = "return"

    return out


def _extract_passenger_details(text: str) -> dict:
    out: dict = {}
    s = text or ""

    # Extract email first (before stripping it, so we keep it)
    m_email = re.search(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", s)
    if m_email:
        out["email"] = m_email.group(1).strip()

    # Strip email from text before name extraction to avoid "Rishi Sharma rishi" matches
    s_no_email = re.sub(r"\S+@\S+\.\S+", "", s).strip()

    # Try explicit "name is/:" pattern first
    name_explicit = re.search(
        r"\b(?:name\s+is|my\s+name\s+is|i\s+am|passenger\s+name)[:\s]+([A-Za-z]+(?:\s+[A-Za-z]+){1,3})\b",
        s_no_email, re.IGNORECASE
    )
    if name_explicit:
        out["full_name"] = " ".join(w.capitalize() for w in name_explicit.group(1).split())
    else:
        # Extract name (must be 2+ words, all alpha)
        m = re.search(r"\b([A-Za-z]+(?:\s+[A-Za-z]+){1,3})\b", s_no_email)
        if m:
            candidate = m.group(1).strip()
            words = candidate.split()
            # Filter out common non-name words
            skip_words = {
                "male", "female", "mr", "mrs", "ms", "miss", "dr", "book", "ticket",
                "name", "email", "phone", "mobile", "passenger", "adult", "my", "i",
                "am", "is", "the", "on", "at", "in", "from", "to", "for", "and",
                "please", "share", "provide", "enter", "fill", "want", "need",
            }
            words = [w for w in words if w.lower() not in skip_words]
            if len(words) >= 2:
                out["full_name"] = " ".join(w.capitalize() for w in words[:4])

    # Phone
    m = re.search(r"\b(?:\+?91[\s-]?)?([6-9]\d{9})\b", s)
    if m:
        out["phone"] = m.group(1).strip()

    # Gender
    if re.search(r"\b(male|man|mr)\b", s, re.IGNORECASE) and not re.search(r"\bfemale\b", s, re.IGNORECASE):
        out["gender"] = "male"
    elif re.search(r"\b(female|woman|ms|mrs)\b", s, re.IGNORECASE):
        out["gender"] = "female"

    # Date of birth
    m = re.search(r"\b(?:dob|born|date of birth)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", s, re.IGNORECASE)
    if m:
        out["dob"] = m.group(1).strip()

    return out


def _missing_travel_fields(state: dict) -> list[str]:
    btype = state.get("booking_type", "flight")
    required = TRAVEL_FIELDS_REQUIRED.get(btype, [])
    missing = []
    for field in required:
        if field == "origin" and not state.get("origin"):
            missing.append("origin")
        elif field == "destination" and not state.get("destination"):
            missing.append("destination")
        elif field == "depart_date" and not state.get("depart_date"):
            missing.append("depart_date")
        elif field == "checkin_date" and not state.get("checkin_date"):
            missing.append("checkin_date")
        elif field == "checkout_date" and not state.get("checkout_date"):
            missing.append("checkout_date")
        elif field == "event_name" and not state.get("event_name"):
            missing.append("event_name")
        elif field == "city" and not state.get("city"):
            missing.append("city")
    return missing


def _missing_passenger_fields(state: dict) -> list[str]:
    details = state.get("passenger_details") or {}
    missing = []
    for field in ["full_name", "email", "phone"]:
        if not details.get(field):
            missing.append(field)
    return missing


def _looks_like_passenger_input(text: str) -> bool:
    s = (text or "").lower()
    signals = ["name", "email", "phone", "mobile", "passenger", "male", "female", "@"]
    if any(k in s for k in signals):
        return True
    return bool(re.search(r"\b[6-9]\d{9}\b", s))


# ─── Page inspection ──────────────────────────────────────────────────────────

async def _get_page_snapshot() -> dict:
    page = browser_manager.page
    if not page:
        return {"url": "", "title": "", "inputs": [], "buttons": [], "excerpt": ""}
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        snap = await page.evaluate(
            """() => {
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const cleaned = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
                const inChat = (el) => !!el.closest('#agentic-chat-root');

                const inputs = Array.from(document.querySelectorAll('input, textarea, select'))
                    .filter((el) => isVisible(el) && !inChat(el))
                    .slice(0, 30)
                    .map((el) => ({
                        type: cleaned(el.getAttribute('type') || el.tagName.toLowerCase()),
                        name: cleaned(el.getAttribute('name')),
                        id: cleaned(el.getAttribute('id')),
                        placeholder: cleaned(el.getAttribute('placeholder')),
                        aria: cleaned(el.getAttribute('aria-label')),
                        value: cleaned(el.value),
                        className: cleaned(el.className).slice(0, 80),
                    }));

                const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], [role="radio"], label'))
                    .filter((el) => isVisible(el) && !inChat(el))
                    .map((el) => cleaned(el.innerText || el.textContent || el.getAttribute('aria-label')))
                    .filter(Boolean)
                    .slice(0, 50);

                const bodyText = cleaned(document.body ? (document.body.innerText || '') : '');
                return { inputs, buttons, excerpt: bodyText.slice(0, 3000) };
            }"""
        )
    except Exception:
        snap = {"inputs": [], "buttons": [], "excerpt": ""}
    return {
        "url": url,
        "title": title,
        "inputs": snap.get("inputs", []),
        "buttons": snap.get("buttons", []),
        "excerpt": snap.get("excerpt", ""),
    }


async def _is_login_gate_visible() -> bool:
    page = browser_manager.page
    if not page:
        return False
    try:
        return bool(await page.evaluate(
            """() => {
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const body = (document.body?.innerText || '').toLowerCase();
                const loginCopy = body.includes('log in to continue') || body.includes('login to continue') ||
                    body.includes('sign in to continue') || body.includes('continue with phone') ||
                    body.includes('enter mobile number') || body.includes('enter otp') ||
                    body.includes('verify otp') || body.includes('enter password') ||
                    body.includes('please login') || body.includes('please sign in');
                const pw = Array.from(document.querySelectorAll("input[type='password']")).some(isVisible);
                const otp = Array.from(document.querySelectorAll("input[name*='otp' i], input[id*='otp' i]")).some(isVisible);
                return loginCopy || pw || otp;
            }"""
        ))
    except Exception:
        return False


# ─── LLM-guided form filling ──────────────────────────────────────────────────

async def _llm_fill_search_form(state: dict) -> bool:
    """
    Ask LLM to generate + execute JavaScript that fills the search form on the current page.
    Returns True if the search was likely submitted.
    """
    page = browser_manager.page
    if not page:
        return False

    snapshot = await _get_page_snapshot()
    btype = state.get("booking_type", "flight")

    # Build booking context
    context_parts = [f"Booking type: {btype}"]
    if state.get("origin"):
        context_parts.append(f"Origin city/code: {state['origin']}")
    if state.get("destination"):
        context_parts.append(f"Destination city/code: {state['destination']}")
    if state.get("depart_date"):
        context_parts.append(f"Departure date (YYYY-MM-DD): {state['depart_date']}")
    if state.get("return_date"):
        context_parts.append(f"Return date (YYYY-MM-DD): {state['return_date']}")
    if state.get("trip_type"):
        context_parts.append(f"Trip type: {state['trip_type']}")
    if state.get("adults"):
        context_parts.append(f"Adults: {state['adults']}")
    if state.get("checkin_date"):
        context_parts.append(f"Check-in date: {state['checkin_date']}")
    if state.get("checkout_date"):
        context_parts.append(f"Check-out date: {state['checkout_date']}")
    if state.get("event_name"):
        context_parts.append(f"Event/movie name: {state['event_name']}")
    if state.get("city"):
        context_parts.append(f"City: {state['city']}")

    booking_context = "\n".join(context_parts)

    prompt = f"""You are a browser automation expert. Your job is to fill a booking search form on a website.

Current page URL: {snapshot.get('url', '')}
Page title: {snapshot.get('title', '')}

Booking details:
{booking_context}

Visible form inputs (JSON):
{json.dumps(snapshot.get('inputs', []), indent=2)}

Visible buttons/labels (first 30):
{json.dumps(snapshot.get('buttons', [])[:30], indent=2)}

Page text excerpt:
{snapshot.get('excerpt', '')[:2000]}

TASK: Generate JavaScript code that:
1. Fills in the FROM/ORIGIN field with the origin city or code
2. Fills in the TO/DESTINATION field with the destination city or code
3. Selects/fills the travel date
4. If trip type is 'return', fills the return date too
5. Clicks the SEARCH button

RULES:
- Use document.querySelector or document.querySelectorAll to find fields
- Use input.value = 'value'; input.dispatchEvent(new Event('input', {{bubbles:true}})); to set values
- Handle React/Angular inputs by also firing 'change' and 'blur' events
- For autocomplete dropdowns: focus the input, set value, fire events, then wait and look for dropdown options
- Try multiple selectors if the first doesn't work
- For city names, use both the city name AND the IATA code (e.g., 'Pune' or 'PNQ')
- Return a SINGLE self-contained async JavaScript function that does all steps
- The function should handle errors gracefully

Return ONLY the JavaScript code, no markdown, no explanation.
The code should be an immediately-invoked async function:
(async () => {{
  // your code here
  return {{success: true/false, message: 'what happened'}};
}})()
"""

    try:
        llm = get_llm(
            provider=chat_server.selected_provider,
            model=chat_server.selected_model,
        )
        resp = await llm.ainvoke(prompt)
        js_code = (resp.content or "").strip()

        # Clean up any markdown code fences
        js_code = re.sub(r"```(?:javascript|js)?\s*", "", js_code)
        js_code = re.sub(r"```\s*$", "", js_code).strip()

        if not js_code:
            return False

        await chat_server.send_to_browser("Filling search form with your details...", "status")
        result = await page.evaluate(js_code)
        await asyncio.sleep(2.0)

        if isinstance(result, dict):
            if result.get("success"):
                await chat_server.send_to_browser(
                    f"Form filled: {result.get('message', 'done')}. Waiting for results...",
                    "status",
                )
                return True

        # Even if result is unclear, check if we navigated away (search submitted)
        new_url = page.url
        if new_url != snapshot.get("url", "") and len(new_url) > 20:
            return True

        return bool(result)

    except Exception as e:
        await chat_server.send_to_browser(
            f"Form fill attempt had an issue: {e}. Trying fallback approach...", "status"
        )
        return False


async def _llm_fill_search_form_retry(state: dict, attempt: int = 1) -> bool:
    """Retry form fill with scroll and popup dismissal."""
    page = browser_manager.page
    if not page:
        return False

    # Dismiss common popups/overlays
    try:
        await page.evaluate(
            """() => {
                const closeBtns = Array.from(document.querySelectorAll(
                    'button, [role="button"], .close, .modal-close, [aria-label*="close" i], [aria-label*="dismiss" i]'
                )).filter(el => {
                    const txt = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').toLowerCase();
                    return txt.includes('close') || txt.includes('dismiss') || txt.includes('skip') || txt === 'x' || txt === '✕';
                });
                closeBtns.forEach(btn => { try { btn.click(); } catch(e) {} });
            }"""
        )
        await asyncio.sleep(0.5)
    except Exception:
        pass

    return await _llm_fill_search_form(state)


# ─── Results extraction ───────────────────────────────────────────────────────

async def _extract_search_results() -> list[dict]:
    """Extract actionable options from a search results page."""
    page = browser_manager.page
    if not page:
        return []
    await asyncio.sleep(2.0)
    try:
        options = await page.evaluate(
            """() => {
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const clean = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
                const inChat = (el) => !!el.closest('#agentic-chat-root');

                // Look for primary action buttons (Book, Select, View, etc.)
                const actionWords = ['book', 'select', 'view deal', 'buy', 'reserve', 'proceed', 'choose'];
                const candidates = [];
                const seen = new Set();

                const allClickable = Array.from(document.querySelectorAll('button, a, [role="button"]'))
                    .filter(el => isVisible(el) && !inChat(el) && !el.hasAttribute('disabled'));

                for (const el of allClickable) {
                    const txt = clean(el.innerText || el.textContent || '').toLowerCase();
                    if (!txt || txt.length > 60) continue;
                    if (!actionWords.some(w => txt.includes(w))) continue;
                    if (txt.includes('lock price') || txt.includes('login') || txt.includes('sign in')) continue;

                    const card = el.closest('article, li, [class*="result"], [class*="flight"], [class*="item"], [class*="card"], [class*="row"]');
                    const label = clean((card?.innerText || el.innerText || '').replace(/book|select|view deal|buy|reserve/ig, '')).slice(0, 120) || `Option ${candidates.length + 1}`;
                    const key = `${Math.round(el.getBoundingClientRect().top)}:${txt}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    candidates.push({ label, rank: candidates.length, kind: 'click' });
                    if (candidates.length >= 8) break;
                }

                // Fallback: collect visible text-only result items
                if (candidates.length === 0) {
                    const items = Array.from(document.querySelectorAll('article, li, [class*="result"], [class*="card"]'))
                        .filter(el => isVisible(el) && !inChat(el))
                        .slice(0, 6);
                    for (const item of items) {
                        const label = clean(item.innerText).slice(0, 120);
                        if (label.length < 10) continue;
                        candidates.push({ label, rank: candidates.length, kind: 'scroll_click' });
                    }
                }

                return candidates;
            }"""
        )
        return options or []
    except Exception:
        return []


async def _click_result_by_rank(rank: int) -> bool:
    """Click the nth actionable result on the page."""
    page = browser_manager.page
    if not page:
        return False
    try:
        result = await page.evaluate(
            """(targetRank) => {
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const inChat = (el) => !!el.closest('#agentic-chat-root');
                const actionWords = ['book', 'select', 'view deal', 'buy', 'reserve', 'proceed'];
                const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
                    .filter(el => {
                        if (!isVisible(el) || inChat(el) || el.hasAttribute('disabled')) return false;
                        const txt = (el.innerText || el.textContent || '').toLowerCase().trim();
                        return actionWords.some(w => txt.includes(w)) && !txt.includes('lock price') && !txt.includes('login');
                    });
                const seen = new Set();
                const deduped = [];
                for (const el of candidates) {
                    const r = el.getBoundingClientRect();
                    const key = `${Math.round(r.top)}:${Math.round(r.left)}`;
                    if (!seen.has(key)) { seen.add(key); deduped.push(el); }
                }
                if (targetRank < 0 || targetRank >= deduped.length) return { ok: false, count: deduped.length };
                const target = deduped[targetRank];
                target.scrollIntoView({ block: 'center', behavior: 'instant' });
                try {
                    target.click();
                    return { ok: true, count: deduped.length };
                } catch(e) {
                    target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    return { ok: true, count: deduped.length };
                }
            }""",
            rank,
        )
        if result and result.get("ok"):
            await asyncio.sleep(2.5)
            return True
    except Exception:
        pass
    return False


# ─── Passenger form filling ───────────────────────────────────────────────────

async def _fill_passenger_form_on_page(details: dict, auto_continue: bool = True) -> dict:
    """Fill passenger/contact details on any booking page."""
    page = browser_manager.page
    if not page:
        return {"filled": False, "continue_clicked": False}
    try:
        result = await page.evaluate(
            """({ details, autoContinue }) => {
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const inChat = (el) => !!el.closest('#agentic-chat-root');
                const clean = (x) => String(x || '').trim();
                const norm = (v) => String(v || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\\s+/g, ' ').trim();

                const setVal = (el, value) => {
                    if (!el || !value) return false;
                    el.focus();
                    const proto = Object.getPrototypeOf(el);
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, '');
                    else el.value = '';
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    if (setter) setter.call(el, value);
                    else el.value = value;
                    ['keydown', 'keyup', 'beforeinput', 'input', 'change', 'blur'].forEach(evt =>
                        el.dispatchEvent(new Event(evt, { bubbles: true }))
                    );
                    return true;
                };

                const findInput = (patterns) => {
                    const inputs = Array.from(document.querySelectorAll('input, textarea')).filter(el => isVisible(el) && !inChat(el));
                    if (patterns.some(p => p.includes('email'))) {
                        const emailTyped = inputs.find(el => el.getAttribute('type') === 'email');
                        if (emailTyped) return emailTyped;
                    }
                    if (patterns.some(p => p.includes('phone') || p.includes('mobile'))) {
                        const tel = inputs.find(el => ['tel', 'number'].includes(el.getAttribute('type') || ''));
                        if (tel) return tel;
                    }
                    for (const el of inputs) {
                        const hay = norm([
                            el.getAttribute('name'), el.getAttribute('id'),
                            el.getAttribute('placeholder'), el.getAttribute('aria-label'),
                            el.labels?.[0]?.innerText || '',
                            (el.closest('div, section, form')?.innerText || '').slice(0, 200),
                        ].join(' '));
                        if (patterns.some(p => hay.includes(p))) return el;
                    }
                    return null;
                };

                let filledAny = false;
                const fullName = clean(details.full_name);
                if (fullName) {
                    const parts = fullName.split(/\\s+/);
                    const first = parts[0] || '';
                    const last = parts.slice(1).join(' ') || first;
                    const firstInput = findInput(['first', 'fname', 'given name', 'first name']);
                    const lastInput = findInput(['last', 'lname', 'surname', 'family', 'last name']);
                    const fullInput = findInput(['full name', 'passenger name', 'name', 'traveller', 'traveler']);
                    if (firstInput || lastInput) {
                        if (setVal(firstInput, first)) filledAny = true;
                        if (setVal(lastInput, last)) filledAny = true;
                    } else if (fullInput) {
                        if (setVal(fullInput, fullName)) filledAny = true;
                    }
                }
                if (clean(details.email)) {
                    if (setVal(findInput(['email', 'e-mail', 'mail']), clean(details.email))) filledAny = true;
                }
                if (clean(details.phone)) {
                    if (setVal(findInput(['phone', 'mobile', 'contact', 'number', 'tel']), clean(details.phone))) filledAny = true;
                }
                if (clean(details.gender)) {
                    const label = details.gender.toLowerCase() === 'female' ? 'female' : 'male';
                    const genderBtns = Array.from(document.querySelectorAll('button, label, [role="radio"], input[type="radio"]'))
                        .filter(el => isVisible(el) && !inChat(el))
                        .filter(el => (el.innerText || el.textContent || el.getAttribute('value') || '').toLowerCase().includes(label));
                    if (genderBtns.length > 0) { try { genderBtns[0].click(); filledAny = true; } catch(e) {} }
                }
                if (clean(details.dob)) {
                    const dobInput = findInput(['dob', 'date of birth', 'birth', 'birthday']);
                    if (dobInput && setVal(dobInput, clean(details.dob))) filledAny = true;
                }

                let continueClicked = false;
                if (autoContinue && filledAny) {
                    const continueTexts = ['continue', 'proceed', 'next', 'review', 'payment', 'confirm'];
                    const actions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
                        .filter(el => isVisible(el) && !inChat(el));
                    for (const el of actions) {
                        const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                        if (!txt || txt.includes('lock price') || txt.includes('login')) continue;
                        if (continueTexts.some(k => txt.includes(k))) {
                            try { el.scrollIntoView({ block: 'center' }); el.click(); continueClicked = true; break; } catch(e) {}
                        }
                    }
                }
                return { filled: filledAny, continue_clicked: continueClicked };
            }""",
            {"details": details, "autoContinue": bool(auto_continue)},
        )
        return result or {"filled": False, "continue_clicked": False}
    except Exception:
        return {"filled": False, "continue_clicked": False}


async def _click_continue_on_page() -> bool:
    """Click any Continue/Next/Proceed button on the current page."""
    page = browser_manager.page
    if not page:
        return False
    selectors = [
        "button:text-is('Continue')", "[role='button']:text-is('Continue')",
        "button:has-text('Continue')", "button:has-text('Proceed')",
        "button:has-text('Next')", "button:has-text('Skip')",
        "button:has-text('No Thanks')", "button:has-text('Payment')",
        "button:has-text('Review')", "a:has-text('Continue')",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            if count < 1:
                continue
            for i in range(min(count, 4)):
                cand = loc.nth(i)
                if not await cand.is_visible(timeout=500):
                    continue
                in_chat = await cand.evaluate("(el) => !!el.closest('#agentic-chat-root')")
                if in_chat:
                    continue
                txt = (await cand.inner_text(timeout=500)).strip().lower()
                if "lock price" in txt or "login" in txt:
                    continue
                await cand.scroll_into_view_if_needed()
                await cand.click(timeout=2000)
                await asyncio.sleep(1.5)
                return True
        except Exception:
            continue
    return False


async def _click_button_by_text(button_text: str) -> bool:
    """Click a visible button/link by exact or partial text match (JS-based)."""
    page = browser_manager.page
    if not page:
        return False
    target = button_text.strip().lower()
    try:
        clicked = await page.evaluate("""(target) => {
            const isVisible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) return false;
                const st = window.getComputedStyle(el);
                return st.display !== 'none' && st.visibility !== 'hidden' && parseFloat(st.opacity) > 0;
            };
            const inChat = el => !!el.closest('#agentic-chat-root');
            // Normalize: collapse whitespace, strip emoji/icons, lowercase
            const norm = raw => (raw || '').replace(/[^\\w\\s,.'!?-]/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase();
            const candidates = Array.from(document.querySelectorAll(
                'button, [role="button"], a, input[type="submit"], input[type="button"], span[onclick], div[onclick]'
            )).filter(el => isVisible(el) && !inChat(el));

            // Pass 1: exact normalized match
            for (const el of candidates) {
                const txt = norm(el.innerText || el.textContent || el.value);
                if (txt === target) {
                    try { el.scrollIntoView({ block: 'center' }); el.click(); return true; } catch(e) {}
                }
            }
            // Pass 2: button text contains target (e.g. button says "No thanks for this trip", target is "no thanks")
            for (const el of candidates) {
                const txt = norm(el.innerText || el.textContent || el.value);
                if (txt && txt.includes(target)) {
                    try { el.scrollIntoView({ block: 'center' }); el.click(); return true; } catch(e) {}
                }
            }
            // Pass 3: target contains button text (abbreviated button, e.g. button says "skip", target is "skip for now")
            for (const el of candidates) {
                const txt = norm(el.innerText || el.textContent || el.value);
                if (txt && txt.length > 3 && target.includes(txt)) {
                    try { el.scrollIntoView({ block: 'center' }); el.click(); return true; } catch(e) {}
                }
            }
            // Pass 4: any word from target matches button (fuzzy — last resort)
            const words = target.split(' ').filter(w => w.length > 4);
            for (const el of candidates) {
                const txt = norm(el.innerText || el.textContent || el.value);
                if (txt && words.some(w => txt.includes(w))) {
                    try { el.scrollIntoView({ block: 'center' }); el.click(); return true; } catch(e) {}
                }
            }
            return false;
        }""", target)
        if clicked:
            await asyncio.sleep(1.5)
            return True
    except Exception:
        pass
    return False


async def _llm_analyze_page_and_act(state: dict) -> dict:
    """
    Scrape the current page, ask LLM what to do, execute ONE action,
    then return what happened. Never loops — caller decides next step.

    Returns: {
        "action": str,       # what was done
        "advanced": bool,    # did we move to a new page/step?
        "filled": bool,      # did we fill any form fields?
        "asked_user": bool,  # did we ask the user for something?
        "message": str,      # message sent to user (if any)
        "needs": str,        # what the user should provide (if asked)
    }
    """
    details = state.get("passenger_details") or {}

    # 1. Check for login gate first (fast check, no LLM needed)
    if await _is_login_gate_visible():
        state["stage"] = "awaiting_login"
        state["login_prompted"] = True
        msg = "Login is required on this page. Please complete login — I will detect it automatically and continue."
        await chat_server.send_to_browser(msg, "agent", requires_input=True)
        _start_login_monitor(state)
        return {"action": "login_required", "advanced": False, "filled": False, "asked_user": True, "message": msg, "needs": "login"}

    # 2. Scrape page
    snapshot = await _get_page_snapshot()
    url = snapshot.get("url", "")
    title = snapshot.get("title", "")
    excerpt = (snapshot.get("excerpt", "") or "")
    excerpt_lower = excerpt.lower()
    inputs = snapshot.get("inputs", [])
    buttons = snapshot.get("buttons", [])

    # 3. Fast-path: payment page
    payment_signals = ["payment", "pay now", "card number", "upi", "net banking", "cvv", "debit card", "credit card"]
    if any(w in excerpt_lower for w in payment_signals):
        msg = (
            "I've reached the payment page.\n"
            f"Page: {title or url}\n"
            "Please complete payment here to finalize your booking. Let me know if you need help with anything."
        )
        state["stage"] = "done"
        await chat_server.send_to_browser(msg, "agent", requires_input=True)
        return {"action": "payment_reached", "advanced": True, "filled": False, "asked_user": True, "message": msg, "needs": "payment"}

    # 4. Ask LLM to reason about the page and decide ONE action
    llm = get_llm(
        provider=chat_server.selected_provider,
        model=chat_server.selected_model,
    )

    page_desc = f"URL: {url}\nTitle: {title}\nExcerpt: {excerpt[:2500]}"
    inputs_desc = json.dumps(inputs[:20], indent=1)
    buttons_desc = json.dumps(buttons[:35], indent=1)
    details_desc = json.dumps(details, indent=1)

    analysis_prompt = f"""You are controlling a browser to complete a ticket/travel booking.
Look at the current page state and decide the SINGLE best next action.

Page state:
{page_desc}

Visible inputs:
{inputs_desc}

Visible buttons/labels:
{buttons_desc}

Passenger details we have:
{details_desc}

Booking stage: {state.get('stage', 'booking')}

Choose ONE action from: fill_passenger_details | click_button | ask_user | nothing

Rules (apply in order, stop at the first match):
1. If a login/sign-in form is visible → ask_user (we cannot log in automatically)
2. If we are on a payment/checkout page (card number, CVV, UPI ID, Net Banking visible) → ask_user
3. If passenger name/email/phone input fields are visible AND EMPTY and we have those details → fill_passenger_details
   (IMPORTANT: if the input "value" fields in the JSON already contain text, they are already filled — do NOT re-fill, go to rule 7)
4. If a confirmation/verification dialog shows passenger name or details asking to confirm → click_button with the exact confirm button text (e.g. "Confirm", "Yes", "Looks Good", "That's correct")
5. OPTIONAL ADD-ON PAGES — If the page is offering any of these optional extras that can be skipped, find and click the skip/decline button:
   - Seat selection (window/aisle/middle seat map) → look for "Skip", "Continue without seat", "No thanks", "Proceed", "Done"
   - Meal selection → look for "Skip", "No thanks", "Continue without meal", "Proceed"
   - Travel insurance / cancellation cover → look for "No thanks", "Skip", "No, I'll risk it", "Proceed without"
   - Baggage add-on → look for "No thanks", "Skip", "Proceed without"
   - Cab/taxi booking → look for "No thanks", "Skip", "Continue without cab"
   - Hotel booking upsell → look for "No thanks", "Skip", "Continue without hotel"
   - Priority check-in / fast-track → look for "No thanks", "Skip"
   - Any other upsell/add-on page with "No thanks" or "Skip" → click it
   Use the EXACT text of the skip button as shown on the page.
6. If the page shows "Review your booking", "Booking summary", or a final review page with a "Continue", "Confirm booking", or "Pay" button that does NOT go to payment yet → click_button with that button text
7. If a Continue / Next / Proceed / Review / Book / Submit button is visible and no critical fields are empty → click_button with that exact button text
8. If required passenger data is missing (e.g. passport number, date of birth) that we don't have → ask_user with a specific question
9. If stuck or nothing useful is visible → ask_user describing what you see

Return STRICT JSON only (no markdown fences, no extra keys):
{{
  "action": "fill_passenger_details|click_button|ask_user|nothing",
  "button_text": "exact text of button to click (only when action=click_button, copy text exactly from page)",
  "reason": "one sentence why",
  "ask_message": "exact question to ask user (only if action=ask_user)",
  "what_user_needs_to_provide": "e.g. 'date of birth', 'passport number' (only if ask_user)"
}}"""

    try:
        resp = await llm.ainvoke(analysis_prompt)
        raw = re.sub(r"```(?:json)?\s*|\s*```", "", (resp.content or "").strip())
        decision = json.loads(raw)
    except Exception:
        decision = {"action": "ask_user", "reason": "Could not parse LLM response", "ask_message": ""}

    action = str(decision.get("action", "ask_user")).strip().lower()
    reason = str(decision.get("reason", "")).strip()
    button_text = str(decision.get("button_text", "")).strip()

    # 5. Execute the decided action
    if action == "fill_passenger_details" and details:
        result = await _fill_passenger_form_on_page(details, auto_continue=False)
        if result.get("filled"):
            await chat_server.send_to_browser(
                f"Filled passenger details. ({reason})", "status"
            )
            await asyncio.sleep(0.8)
            # Let caller (loop) re-scrape and decide next step — don't auto-click here
            return {"action": "filled", "advanced": False, "filled": True, "asked_user": False, "message": "", "needs": ""}
        else:
            visible_inputs = [
                f"{inp.get('placeholder') or inp.get('aria') or inp.get('name') or inp.get('id')}"
                for inp in inputs[:8]
                if any(inp.get(k) for k in ['placeholder', 'aria', 'name', 'id'])
            ]
            missing_p = _missing_passenger_fields(state)
            if missing_p:
                needed = [PASSENGER_FIELD_PROMPTS[f] for f in missing_p]
                msg = f"I see a form on the page but couldn't fill it. I still need: {', '.join(needed)}. Please provide them."
            else:
                msg = (
                    f"I see a form but had trouble filling it automatically.\n"
                    f"Visible fields: {', '.join(visible_inputs) or 'unknown'}.\n"
                    "Please share what this page is asking for and I'll help fill it."
                )
            await chat_server.send_to_browser(msg, "agent", requires_input=True)
            return {"action": "fill_failed", "advanced": False, "filled": False, "asked_user": True, "message": msg, "needs": "form_data"}

    elif action == "click_button":
        # Use LLM-specified button text for precise targeting
        btn_label = button_text or "Continue"
        advanced = await _click_button_by_text(btn_label)
        if not advanced:
            # Fallback to generic continue-click if specific text not found
            advanced = await _click_continue_on_page()
        if advanced:
            await chat_server.send_to_browser(
                f"Clicked \"{btn_label}\". ({reason})", "status"
            )
            return {"action": "button_clicked", "advanced": True, "filled": False, "asked_user": False, "message": "", "needs": ""}
        else:
            visible_btns = [b for b in buttons[:10] if b.strip()]
            msg = (
                f"I tried to click \"{btn_label}\" but couldn't find it.\n"
                f"Current page: {title or url}\n"
                f"Visible buttons: {', '.join(visible_btns[:8]) or 'none found'}.\n"
                "What should I do next? (e.g. 'click Confirm', 'skip this step')"
            )
            await chat_server.send_to_browser(msg, "agent", requires_input=True)
            return {"action": "button_failed", "advanced": False, "filled": False, "asked_user": True, "message": msg, "needs": "direction"}

    else:  # ask_user or nothing
        ask_msg = str(decision.get("ask_message", "")).strip()
        needs = str(decision.get("what_user_needs_to_provide", "")).strip()
        if not ask_msg:
            visible_btns = [b for b in buttons[:8] if b.strip()]
            missing_p = _missing_passenger_fields(state)
            if missing_p:
                needed = [PASSENGER_FIELD_PROMPTS[f] for f in missing_p]
                ask_msg = f"I need passenger details to proceed: {', '.join(needed)}. Please share them."
                needs = ", ".join(needed)
            else:
                ask_msg = (
                    f"I'm on: {title or url}\n"
                    f"What I see: {excerpt[:300].strip()}\n"
                    f"Buttons available: {', '.join(visible_btns[:6]) or 'none'}.\n"
                    "What should I do next?"
                )
                needs = "direction"
        await chat_server.send_to_browser(ask_msg, "agent", requires_input=True)
        return {"action": "asked_user", "advanced": False, "filled": False, "asked_user": True, "message": ask_msg, "needs": needs}


async def _auto_progress_booking_page(state: dict) -> dict:
    """
    Multi-step chained executor: scrape → LLM reason → act, repeat until:
    - User input genuinely needed (missing data, payment, login)
    - No progress made after an attempt
    - Safety limit reached (8 steps)

    Each completed step sends a brief status to the user so they can follow along.
    Only stops and asks the user when truly blocked.
    """
    MAX_STEPS = 20  # many intermediate pages possible: seat, meal, insurance, baggage, cab...
    last_result: dict = {}
    fill_count = 0  # guard: don't fill same form more than once without advancing

    for step_num in range(MAX_STEPS):
        result = await _llm_analyze_page_and_act(state)
        last_result = result
        action = result.get("action", "")

        # Stop conditions — these already sent a message to the user
        if result.get("asked_user"):
            return result
        if action in ("login_required", "payment_reached"):
            return result

        # If the step did nothing useful, stop
        if not result.get("advanced") and not result.get("filled"):
            return result

        # Track fills; stop if the form appears to be filled but we can't advance
        if result.get("filled") and not result.get("advanced"):
            fill_count += 1
            if fill_count >= 2:
                # Filled twice without advancing — ask user what to do
                snap = await _get_page_snapshot()
                btns = snap.get("buttons", [])[:8]
                msg = (
                    "I filled the passenger form but couldn't automatically proceed.\n"
                    f"Visible buttons: {', '.join(btns) or 'none'}.\n"
                    "Please tell me which button to click, or what to do next."
                )
                await chat_server.send_to_browser(msg, "agent", requires_input=True)
                return {"action": "asked_user", "advanced": False, "filled": True, "asked_user": True, "message": msg, "needs": "direction"}
        else:
            fill_count = 0  # reset whenever we advance

        # Made progress — wait for page to settle, then continue to next step
        await asyncio.sleep(1.3)

    # Hit step limit — hand back to user
    return last_result


# ─── Login monitor ────────────────────────────────────────────────────────────

_login_monitor_task: asyncio.Task | None = None


def _start_login_monitor(state: dict) -> None:
    global _login_monitor_task
    if _login_monitor_task and not _login_monitor_task.done():
        _login_monitor_task.cancel()
    _login_monitor_task = asyncio.create_task(_wait_for_login(state))


async def _wait_for_login(state: dict) -> None:
    try:
        for _ in range(300):
            await asyncio.sleep(2)
            s = chat_server.universal_booking_state or {}
            if not s.get("active"):
                return
            if s.get("stage") != "awaiting_login":
                return
            if await _is_login_gate_visible():
                continue
            # Login complete
            state["stage"] = "booking"
            state["login_prompted"] = False
            await chat_server.send_to_browser(
                "Login detected. Continuing booking automatically...", "status"
            )
            await _auto_progress_booking_page(state)
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        global _login_monitor_task
        _login_monitor_task = None


# ─── LLM field extraction fallback ───────────────────────────────────────────

async def _llm_extract_travel_fields(text: str, booking_type: str) -> dict:
    try:
        llm = get_llm(
            provider=chat_server.selected_provider,
            model=chat_server.selected_model,
        )
        prompt = (
            f"Extract {booking_type} booking fields from user text.\n"
            "Return ONLY strict JSON with these fields (empty string if unknown):\n"
            "origin, destination, depart_date (YYYY-MM-DD), return_date (YYYY-MM-DD), "
            "trip_type (oneway/return), adults (integer), event_name, city, "
            "checkin_date (YYYY-MM-DD), checkout_date (YYYY-MM-DD).\n"
            f"Today's date: {date.today().isoformat()}\n"
            f"User text: {text}"
        )
        resp = await llm.ainvoke(prompt)
        raw = re.sub(r"```(?:json)?\s*|\s*```", "", (resp.content or "").strip())
        data = json.loads(raw)
        out = {}
        for field in ["origin", "destination", "depart_date", "return_date", "trip_type", "event_name", "city", "checkin_date", "checkout_date"]:
            val = str(data.get(field, "")).strip()
            if val and val not in ("null", "none", "unknown"):
                # Normalize dates
                if "date" in field and val:
                    normed = _norm_date(val) or _parse_human_date(val)
                    if normed:
                        out[field] = normed
                else:
                    out[field] = val
        adults = data.get("adults")
        if isinstance(adults, (int, float)) and int(adults) >= 1:
            out["adults"] = int(adults)
        return out
    except Exception:
        return {}


# ─── Main handler ─────────────────────────────────────────────────────────────

def _parse_choice_number(text: str) -> int:
    m = re.search(r"\b(?:option|pick|choose|go with|number|no\.?)\s*(\d+)\b", text, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d+)\b", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return 0


def _is_new_booking_request(text: str) -> bool:
    t = text.lower()
    has_site = any(s in t for s in SITE_REGISTRY)
    has_travel = re.search(r"\bfrom\s+\w+.*\bto\s+\w+\b", t) is not None
    return (has_site or has_travel) and _is_booking_intent(t)


async def handle_universal_booking(message: str) -> bool:
    """
    Main entry point. Returns True if this message was handled by universal booking.
    """
    text = (message or "").strip()

    # Initialize state
    state = chat_server.universal_booking_state
    if state is None:
        state = _default_state()
        chat_server.universal_booking_state = state

    # Cancel command
    if state.get("active") and text.lower() in {"cancel", "cancel booking", "stop", "quit booking", "exit"}:
        global _login_monitor_task
        if _login_monitor_task and not _login_monitor_task.done():
            _login_monitor_task.cancel()
        chat_server.universal_booking_state = _default_state()
        await chat_server.send_to_browser("Booking flow cancelled.", "system")
        return True

    # Decide if universal handler should take this
    if not state.get("active"):
        if not _should_handle_universally(text):
            return False
        if not _is_booking_intent(text):
            return False

    # --- AWAITING LOGIN ---
    if state.get("stage") == "awaiting_login":
        if text.lower() in {"done", "logged in", "continue", "i logged in"}:
            if await _is_login_gate_visible():
                await chat_server.send_to_browser(
                    "I still see the login form. Please complete it — I'll detect automatically.", "agent", requires_input=True,
                )
            else:
                state["stage"] = "booking"
                await chat_server.send_to_browser("Resuming booking after login...", "status")
                result = await _auto_progress_booking_page(state)
                if not result.get("asked_user") and not result.get("advanced") and not result.get("filled"):
                    await chat_server.send_to_browser(
                        "Continuing. Share any required details visible on the page.", "agent", requires_input=True,
                    )
        else:
            await chat_server.send_to_browser(
                "Please complete login on the page. I will detect it and continue automatically.", "agent", requires_input=True,
            )
        return True

    # --- BOOKING / DONE stage (filling forms, progressing) ---
    if state.get("stage") in ("booking", "done"):
        if _is_new_booking_request(text):
            # Reset for fresh booking
            chat_server.universal_booking_state = _default_state()
            state = chat_server.universal_booking_state
        else:
            # Try to extract passenger details from this message
            if _looks_like_passenger_input(text):
                new_details = _extract_passenger_details(text)
                existing = state.get("passenger_details") or {}
                state["passenger_details"] = {**existing, **new_details}
                await chat_server.send_to_browser("Got it. Filling your details now...", "status")

            result = await _auto_progress_booking_page(state)
            if result.get("asked_user") or state.get("stage") == "done":
                return True
            if result.get("advanced"):
                await chat_server.send_to_browser(
                    "Moved to next step. Continuing booking...", "status"
                )
            else:
                missing_p = _missing_passenger_fields(state)
                if missing_p:
                    prompts = [PASSENGER_FIELD_PROMPTS[f] for f in missing_p]
                    await chat_server.send_to_browser(
                        f"I still need: {', '.join(prompts)}. Please share them to continue.",
                        "agent", requires_input=True,
                    )
                else:
                    await chat_server.send_to_browser(
                        "I evaluated the page. Share any missing field visible on screen and I will fill it.",
                        "agent", requires_input=True,
                    )
            return True

    # --- COMPARING stage (user selects from results) ---
    if state.get("stage") == "comparing":
        if _is_new_booking_request(text):
            chat_server.universal_booking_state = _default_state()
            state = chat_server.universal_booking_state
        else:
            options = state.get("options", [])
            choice = _parse_choice_number(text)
            wants_cheapest = bool(re.search(r"\b(cheapest|lowest|minimum|least)\b", text, re.IGNORECASE))

            if wants_cheapest or (choice == 1 and options):
                rank = 0
            elif choice > 0:
                rank = choice - 1
            else:
                await chat_server.send_to_browser(
                    "Please tell me which option you want, for example 'option 1' or 'cheapest'.",
                    "agent", requires_input=True,
                )
                return True

            await chat_server.send_to_browser(f"Selecting option {rank + 1}...", "status")
            clicked = await _click_result_by_rank(rank)
            if clicked:
                state["stage"] = "booking"
                await chat_server.send_to_browser(
                    "Option selected. Now filling passenger details on the booking page...", "status"
                )
                await asyncio.sleep(2.0)
                result = await _auto_progress_booking_page(state)
                if not result.get("advanced") and not result.get("filled") and not result.get("asked_user"):
                    missing_p = _missing_passenger_fields(state)
                    if missing_p:
                        prompts = [PASSENGER_FIELD_PROMPTS[f] for f in missing_p]
                        await chat_server.send_to_browser(
                            f"Please share passenger details to continue: {', '.join(prompts)}.",
                            "agent", requires_input=True,
                        )
            else:
                await chat_server.send_to_browser(
                    "I couldn't click that option automatically. Please click it on the page and I'll continue.",
                    "agent", requires_input=True,
                )
            return True

    # --- COLLECTING PASSENGER DETAILS ---
    if state.get("stage") == "collecting_passenger":
        new_details = _extract_passenger_details(text)
        existing = state.get("passenger_details") or {}
        merged = {**existing, **new_details}
        state["passenger_details"] = merged

        missing_p = _missing_passenger_fields(state)
        if missing_p and not new_details:
            prompts = [PASSENGER_FIELD_PROMPTS[f] for f in missing_p]
            await chat_server.send_to_browser(
                f"I still need: {', '.join(prompts)}. Please share them.",
                "agent", requires_input=True,
            )
            return True

        # Enough details collected (or user wants to skip) - proceed to navigate
        state["stage"] = "navigating"
        state["passenger_collected"] = True

        site_name = state.get("target_name") or "the booking website"
        await chat_server.send_to_browser(
            f"Great! I have your details. Now opening {site_name} and searching...",
            "status",
        )
        await _navigate_and_search(state)
        return True

    # --- COLLECTING TRAVEL DETAILS ---
    if state.get("stage") == "collecting_travel":
        btype = state.get("booking_type", "flight")
        new_fields = _extract_travel_fields(text, btype)
        if not new_fields:
            new_fields = await _llm_extract_travel_fields(text, btype)
        for k, v in new_fields.items():
            if v:
                state[k] = v

        missing_t = _missing_travel_fields(state)
        if missing_t:
            prompts = [TRAVEL_FIELD_PROMPTS.get(f, f) for f in missing_t]
            await chat_server.send_to_browser(
                f"I need a bit more information: {', '.join(prompts)}. Please share them.",
                "agent", requires_input=True,
            )
            return True

        # Travel details complete — ask for passenger details
        state["stage"] = "collecting_passenger"
        await _ask_for_passenger_details(state)
        return True

    # --- INITIAL / IDLE: Start new booking flow ---
    state["active"] = True
    state["stage"] = "collecting_travel"

    # Detect site and booking type
    site_key, site_url, btype, site_name = _detect_site(text)
    if not btype:
        btype = _detect_booking_type(text) or "flight"
    state["booking_type"] = btype
    state["target_site"] = site_key
    state["target_url"] = site_url
    state["target_name"] = site_name

    # Extract whatever fields are in the initial message
    new_fields = _extract_travel_fields(text, btype)
    if not new_fields:
        new_fields = await _llm_extract_travel_fields(text, btype)
    for k, v in new_fields.items():
        if v:
            state[k] = v

    # Also extract passenger details if given upfront
    if _looks_like_passenger_input(text):
        pd = _extract_passenger_details(text)
        if pd:
            state["passenger_details"] = pd

    # Greet and describe what we'll do
    booking_label = {
        "flight": "flight ticket",
        "train": "train ticket",
        "bus": "bus ticket",
        "hotel": "hotel",
        "event": "ticket",
    }.get(btype, "ticket")

    greeting = f"I'll help you book a {booking_label}"
    if site_name:
        greeting += f" on {site_name}"
    greeting += "."
    await chat_server.send_to_browser(greeting, "agent", requires_input=False)

    missing_t = _missing_travel_fields(state)
    if missing_t:
        prompts = [TRAVEL_FIELD_PROMPTS.get(f, f) for f in missing_t]
        await chat_server.send_to_browser(
            f"I need the following details: {', '.join(prompts)}. Please share them.",
            "agent", requires_input=True,
        )
        return True

    # All travel details collected — ask for passenger details
    state["stage"] = "collecting_passenger"
    await _ask_for_passenger_details(state)
    return True


async def _ask_for_passenger_details(state: dict) -> None:
    """Ask the user for passenger details before navigating to the website."""
    missing_p = _missing_passenger_fields(state)
    if not missing_p:
        # Already have all details, jump to navigating
        state["stage"] = "navigating"
        site_name = state.get("target_name") or "the booking website"
        await chat_server.send_to_browser(
            f"I have all details. Opening {site_name} now...", "status"
        )
        await _navigate_and_search(state)
        return

    # Build a smart summary of what we know
    btype = state.get("booking_type", "flight")
    summary_parts = []
    if state.get("origin"):
        summary_parts.append(f"from {state['origin']}")
    if state.get("destination"):
        summary_parts.append(f"to {state['destination']}")
    if state.get("depart_date"):
        summary_parts.append(f"on {state['depart_date']}")
    if state.get("event_name"):
        summary_parts.append(f"for '{state['event_name']}'")
    if state.get("destination") and btype == "hotel":
        summary_parts.append(f"in {state['destination']}")

    travel_summary = " ".join(summary_parts)
    detail_needed = [PASSENGER_FIELD_PROMPTS[f] for f in missing_p]

    msg = f"Got it"
    if travel_summary:
        msg += f" — {travel_summary}"
    msg += f". To complete the booking, I need passenger details: {', '.join(detail_needed)}. Please share them now."
    await chat_server.send_to_browser(msg, "agent", requires_input=True)


async def _navigate_and_search(state: dict) -> None:
    """Navigate to the target website and fill the search form."""
    target_url = state.get("target_url", "")
    target_name = state.get("target_name") or "the website"
    btype = state.get("booking_type", "flight")

    if not target_url:
        # No specific site — use Google to find the right site
        query_parts = []
        if state.get("origin"):
            query_parts.append(f"from {state['origin']}")
        if state.get("destination"):
            query_parts.append(f"to {state['destination']}")
        if state.get("depart_date"):
            query_parts.append(state["depart_date"])
        query = f"book {btype} ticket {' '.join(query_parts)}"
        await chat_server.send_to_browser(
            f"No specific website detected. I'll search Google for: {query}", "status"
        )
        from tools.search.google import google_search_safe
        await google_search_safe(query=query)
        state["stage"] = "comparing"
        return

    await chat_server.send_to_browser(f"Opening {target_name}...", "status")
    await browser_manager.navigate(target_url)
    await asyncio.sleep(3.0)

    # Dismiss popups
    page = browser_manager.page
    if page:
        try:
            await page.evaluate(
                """() => {
                    const closeBtns = Array.from(document.querySelectorAll(
                        'button, [role="button"], .close-btn, [aria-label*="close" i], [aria-label*="dismiss" i]'
                    )).filter(el => {
                        const txt = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
                        return txt.includes('close') || txt.includes('dismiss') || txt.includes('skip') || txt === 'x' || txt === '✕';
                    });
                    closeBtns.slice(0, 3).forEach(btn => { try { btn.click(); } catch(e) {} });
                }"""
            )
            await asyncio.sleep(0.8)
        except Exception:
            pass

    # Check for login gate right away
    if await _is_login_gate_visible():
        state["stage"] = "awaiting_login"
        await chat_server.send_to_browser(
            f"{target_name} requires login. Please log in — I'll detect completion automatically.",
            "agent",
        )
        _start_login_monitor(state)
        return

    # Describe the page to user before attempting to fill
    snapshot = await _get_page_snapshot()
    page_title = snapshot.get("title", "") or target_name
    visible_inputs = [
        inp.get("placeholder") or inp.get("aria") or inp.get("name") or inp.get("id")
        for inp in snapshot.get("inputs", [])
        if any(inp.get(k) for k in ["placeholder", "aria", "name", "id"])
    ]
    await chat_server.send_to_browser(
        f"Opened {target_name} ({page_title}). "
        + (f"I can see: {', '.join(visible_inputs[:6])} fields. " if visible_inputs else "")
        + "Filling the search form with your details...",
        "status",
    )

    state["search_attempts"] = state.get("search_attempts", 0) + 1
    success = await _llm_fill_search_form(state)
    if not success:
        # One retry with popup dismissal
        await asyncio.sleep(1.5)
        success = await _llm_fill_search_form_retry(state, attempt=2)

    if success:
        await asyncio.sleep(3.0)
        state["stage"] = "comparing"
        await _show_search_results(state)
    else:
        # Scrape page to describe what went wrong
        snap2 = await _get_page_snapshot()
        visible_btns = [b for b in snap2.get("buttons", [])[:8] if b.strip()]
        msg = (
            f"I had trouble auto-filling the form on {target_name}.\n"
            f"The page is open — I can see: {', '.join(visible_btns[:6]) or 'some buttons'}.\n"
            "Please fill in the search details manually on the page and click Search. "
            "Once results appear, type 'results loaded' and I'll take over."
        )
        await chat_server.send_to_browser(msg, "agent", requires_input=True)
        state["stage"] = "comparing"


async def _show_search_results(state: dict) -> None:
    """Extract and present search results to the user."""
    btype = state.get("booking_type", "flight")
    options = await _extract_search_results()
    state["options"] = options

    summary_lines = [
        f"Search results are ready on {state.get('target_name', 'the website')}.",
    ]
    if state.get("origin") and state.get("destination"):
        summary_lines.append(f"Route: {state['origin']} → {state['destination']}")
    if state.get("depart_date"):
        summary_lines.append(f"Date: {state['depart_date']}")
    if state.get("adults"):
        summary_lines.append(f"Passengers: {state['adults']}")

    if options:
        summary_lines.append(f"\nTop {len(options)} option(s) found:")
        for i, opt in enumerate(options, 1):
            summary_lines.append(f"{i}. {opt.get('label', f'Option {i}')}")
        summary_lines.append("\nTell me which option to book (e.g. 'option 1' or 'cheapest').")
    else:
        # Describe what's on the page so user can guide us
        snap = await _get_page_snapshot()
        visible_btns = [b for b in snap.get("buttons", [])[:10] if b.strip()]
        summary_lines.append(
            "\nResults appear to be loaded. I see these actions: "
            + (", ".join(visible_btns[:8]) if visible_btns else "none detected") + ".\n"
            "Tell me which option to pick (e.g. 'option 1', 'cheapest', or 'book first one')."
        )

    await chat_server.send_to_browser("\n".join(summary_lines), "agent", requires_input=True)
