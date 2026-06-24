"""
Flight booking assistant flow for chat-driven interaction.

Collects minimum flight details, opens Ixigo comparison, and then
opens the selected booking partner URL based on user choice.
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
from tools.flights.ixigo_automation import (
    build_ixigo_results_url,
    open_ixigo_and_search_from_home,
    proceed_with_first_ixigo_option,
)

_flight_login_task: asyncio.Task | None = None


def _default_state() -> dict:
    return {
        "active": False,
        "stage": "idle",  # idle | collecting | compared | awaiting_login | provider_ready | done
        "topic": "execution",  # execution | human
        "origin": "",
        "destination": "",
        "depart_date": "",
        "return_date": "",
        "trip_type": "oneway",
        "adults": 1,
        "auto_book": False,
        "book_cheapest": True,   # always auto-select cheapest by default
        "login_prompted": False,
        "post_login_action": "",
        "pending_option": {},
        "options": [],
        "passenger_details": {},
        "selected_travellers": [],
        "cancellation_prompted": False,
        "cancellation_choice": "no",  # default: option 3 — no cancellation
        "cancellation_applied": False,
        "passenger_pre_prompted": False,
        "passenger_prompted": False,   # whether we asked for passenger details on the booking page
        "awaiting_passenger_fields": [],  # specific fields still needed
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
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.fullmatch(r"(\d{2})(\d{2})(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return ""


CITY_TO_IATA = {
    # India — comprehensive
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
    "madras": "MAA",
    "kolkata": "CCU",
    "calcutta": "CCU",
    "ahmedabad": "AMD",
    "goa": "GOI",
    "ranchi": "IXR",
    "patna": "PAT",
    "lucknow": "LKO",
    "kochi": "COK",
    "cochin": "COK",
    "coimbatore": "CJB",
    "indore": "IDR",
    "varanasi": "VNS",
    "bhopal": "BHO",
    "srinagar": "SXR",
    "chandigarh": "IXC",
    "amritsar": "ATQ",
    "nagpur": "NAG",
    "visakhapatnam": "VTZ",
    "vizag": "VTZ",
    "thiruvananthapuram": "TRV",
    "trivandrum": "TRV",
    "guwahati": "GAU",
    "agartala": "IXA",
    "imphal": "IMF",
    "port blair": "IXZ",
    "leh": "IXL",
    "jammu": "IXJ",
    "shimla": "SLV",
    "udaipur": "UDR",
    "jodhpur": "JDH",
    "dehradun": "DED",
    "jaipur": "JAI",
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
    token = re.sub(r"[^a-z ]", " ", (raw or "").strip().lower())
    token = re.sub(r"\s+", " ", token).strip()
    if re.fullmatch(r"[a-z]{3}", token):
        return token.upper()
    if token in CITY_TO_IATA:
        return CITY_TO_IATA[token]
    # Fuzzy fallback for minor typos like "mumbali" or "delih".
    close = get_close_matches(token, CITY_TO_IATA.keys(), n=1, cutoff=0.78)
    if close:
        return CITY_TO_IATA[close[0]]
    return ""


def _norm_place_phrase(raw: str) -> str:
    """
    Normalize place text that may include trailing words like
    'delhi tomorrow cheapest option' by testing progressively
    shorter prefixes.
    """
    token = re.sub(r"[^a-z ]", " ", (raw or "").strip().lower())
    token = re.sub(r"\s+", " ", token).strip()
    if not token:
        return ""

    direct = _norm_place(token)
    if direct:
        return direct

    words = token.split()
    max_take = min(len(words), 4)
    for take in range(max_take, 0, -1):
        probe = " ".join(words[:take])
        code = _norm_place(probe)
        if code:
            return code
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


def _extract_fields(text: str) -> dict:
    out: dict[str, str | int] = {}
    s = (text or "").strip()
    date_token_pat = r"\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4}|\d{8}"

    # Prefer IATA codes for reliability.
    m = re.search(r"\bfrom\s+([A-Za-z]{3})\b", s, flags=re.IGNORECASE)
    if m:
        out["origin"] = m.group(1).upper()
    else:
        m = re.search(r"\bfrom\s+([A-Za-z ]{2,40})\s+\bto\b", s, flags=re.IGNORECASE)
        if m:
            place = _norm_place_phrase(m.group(1))
            if place:
                out["origin"] = place
    # Natural "Pune to Delhi" style without explicit "from".
    if not out.get("origin") or not out.get("destination"):
        m = re.search(r"\b([A-Za-z ]{2,60})\s+\bto\b\s+([A-Za-z ]{2,60})\b", s, flags=re.IGNORECASE)
        if m:
            left = re.sub(r"[^a-zA-Z ]", " ", m.group(1)).strip()
            right = re.sub(r"[^a-zA-Z ]", " ", m.group(2)).strip()
            left_words = [w for w in left.split() if w]
            right_words = [w for w in right.split() if w]

            origin_guess = ""
            destination_guess = ""

            # Try suffixes on left side so "book flight pune" resolves to "pune".
            for take in range(min(4, len(left_words)), 0, -1):
                probe = " ".join(left_words[-take:])
                code = _norm_place_phrase(probe)
                if code:
                    origin_guess = code
                    break

            # Try prefixes on right side so "delhi on 18th may" resolves to "delhi".
            for take in range(min(4, len(right_words)), 0, -1):
                probe = " ".join(right_words[:take])
                code = _norm_place_phrase(probe)
                if code:
                    destination_guess = code
                    break

            if origin_guess and not out.get("origin"):
                out["origin"] = origin_guess
            if destination_guess and not out.get("destination"):
                out["destination"] = destination_guess
    m = re.search(r"\bto\s+([A-Za-z]{3})\b", s, flags=re.IGNORECASE)
    if m:
        out["destination"] = m.group(1).upper()
    else:
        m = re.search(r"\bto\s+([A-Za-z ]{2,40})(?:\s+\bon\b|\s+\bfor\b|$)", s, flags=re.IGNORECASE)
        if m:
            place = _norm_place_phrase(m.group(1))
            if place:
                out["destination"] = place
    if not out.get("destination"):
        m = re.search(r"\bto\s+([A-Za-z ]{2,60})", s, flags=re.IGNORECASE)
        if m:
            place = _norm_place_phrase(m.group(1))
            if place:
                out["destination"] = place

    m = re.search(rf"\b(?:on|depart|departure)\s+({date_token_pat})\b", s, flags=re.IGNORECASE)
    if m:
        d = _norm_date(m.group(1))
        if d:
            out["depart_date"] = d
    if not out.get("depart_date"):
        m = re.search(rf"\b({date_token_pat})\b", s)
        if m:
            d = _norm_date(m.group(1))
            if d:
                out["depart_date"] = d
    if not out.get("depart_date"):
        d = _parse_relative_date(s)
        if d:
            out["depart_date"] = d
    if not out.get("depart_date"):
        d = _parse_human_date(s)
        if d:
            out["depart_date"] = d

    m = re.search(rf"\b(?:return|inbound)\s+({date_token_pat})\b", s, flags=re.IGNORECASE)
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
    if _wants_cheapest_booking(s):
        out["book_cheapest"] = True

    return out


def _wants_cheapest_booking(text: str) -> bool:
    s = (text or "").lower()
    return bool(
        re.search(
            r"\b(cheapest|lowest fare|minimum fare|least fare)\b", s
        )
        and re.search(r"\b(book|flight|ticket|tickets)\b", s)
    )


def _cancel_login_monitor() -> None:
    global _flight_login_task
    if _flight_login_task and not _flight_login_task.done():
        _flight_login_task.cancel()
    _flight_login_task = None


async def _is_login_gate_visible() -> bool:
    page = browser_manager.page
    if not page:
        return False
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        const cs = window.getComputedStyle(el);
                        if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                        const r = el.getBoundingClientRect();
                        return !!r && r.width > 8 && r.height > 8;
                    };

                    const bodyText = (document.body?.innerText || '').toLowerCase();
                    const hasStrongLoginCopy =
                        bodyText.includes('log in to continue') ||
                        bodyText.includes('login to continue') ||
                        bodyText.includes('sign in to continue') ||
                        bodyText.includes('continue with phone') ||
                        bodyText.includes('enter mobile number') ||
                        bodyText.includes('enter otp') ||
                        bodyText.includes('verify otp') ||
                        bodyText.includes('enter password');

                    const pw = Array.from(document.querySelectorAll("input[type='password']")).some(isVisible);
                    const otp = Array.from(document.querySelectorAll("input[name*='otp' i], input[id*='otp' i], input[autocomplete='one-time-code']")).some(isVisible);
                    return hasStrongLoginCopy || pw || otp;
                }"""
            )
        )
    except Exception:
        return False


async def _attempt_post_login_proceed_click() -> bool:
    page = browser_manager.page
    if not page:
        return False
    selectors = [
        "button:has-text('Continue')",
        "button:has-text('Proceed')",
        "button:has-text('Next')",
        "button:has-text('Book')",
        "button:has-text('Review')",
        "a:has-text('Continue')",
        "a:has-text('Proceed')",
        "[role='button']:has-text('Continue')",
        "[role='button']:has-text('Proceed')",
    ]
    for sel in selectors:
        try:
            target = page.locator(sel).first
            if await target.is_visible(timeout=900):
                await target.scroll_into_view_if_needed()
                await target.click(timeout=2200)
                await asyncio.sleep(1.8)
                return True
        except Exception:
            continue
    return False


async def _enter_human_login_handoff(
    state: dict,
    resume_action: str = "resume_continue",
    pending_option: dict | None = None,
) -> None:
    state["stage"] = "awaiting_login"
    state["topic"] = "human"
    state["login_prompted"] = True
    state["post_login_action"] = resume_action
    state["pending_option"] = pending_option or {}
    await chat_server.send_to_browser(
        "Topic switched to human: login is required on this page. Please enter phone number/OTP and complete login.",
        "status",
    )
    await chat_server.send_to_browser(
        "I will detect successful login automatically and switch back to execution.",
        "agent",
    )
    _start_login_monitor()


async def _resume_after_login(state: dict) -> None:
    state["topic"] = "execution"
    # After login we should continue provider-side booking flow,
    # not mark it done, so passenger/cancellation handling stays active.
    state["stage"] = "provider_ready"
    await chat_server.send_to_browser(
        "Topic switched to execution: login detected. Continuing automatically.",
        "status",
    )

    action = (state.get("post_login_action") or "resume_continue").strip().lower()
    pending_option = state.get("pending_option") or {}
    state["post_login_action"] = ""
    state["pending_option"] = {}

    if action == "resume_cheapest":
        result = await _attempt_cheapest_click()
        await chat_server.send_to_browser(
            (
                "Post-login cheapest flow executed."
                f" Sorted: {'yes' if result.get('sorted') else 'no'}"
                f", Option clicked: {'yes' if result.get('clicked') else 'no'}."
                + (" (fallback click used)" if result.get("fallback_rank_click") else "")
            ),
            "agent",
        )
        if result.get("clicked"):
            await _handle_provider_page_after_open(state)
        else:
            if await _is_login_gate_visible():
                await _enter_human_login_handoff(state, resume_action="resume_cheapest")
            else:
                await chat_server.send_to_browser(
                    "I could not move ahead automatically after login. Please pick an option on the page and I will continue.",
                    "agent",
                )
                state["stage"] = "compared"
        return

    if action == "resume_selected":
        clicked = await _click_option_choice(pending_option)
        if clicked:
            await chat_server.send_to_browser("I clicked your selected option after login.", "status")
            await _handle_provider_page_after_open(state)
        else:
            if await _is_login_gate_visible():
                await _enter_human_login_handoff(
                    state,
                    resume_action="resume_selected",
                    pending_option=pending_option,
                )
            else:
                await chat_server.send_to_browser(
                    "I could not click the selected option after login. Please click it once and share the opened URL.",
                    "agent",
                )
        return

    advanced = await _attempt_post_login_proceed_click()
    if advanced:
        await chat_server.send_to_browser(
            "I moved ahead after login. Share passenger details now and I will continue booking.",
            "agent",
        )
    else:
        await chat_server.send_to_browser(
            "Login is complete. Share passenger details on this page and I will continue booking.",
            "agent",
        )


async def _handle_provider_page_after_open(state: dict) -> None:
    """Called right after a flight option is clicked. Immediately starts the autonomous booking loop."""
    await _auto_book_after_selection(state)


async def _auto_book_after_selection(state: dict) -> None:
    """
    Drives the autonomous post-selection booking flow:
      1. Wait for provider page to load.
      2. Hand off to human if login is required.
      3. Auto-fill passenger details, skip add-ons, click through every step.
      4. Stop and notify user when payment page is reached.
    """
    await asyncio.sleep(2.5)

    if await _is_login_gate_visible():
        await _enter_human_login_handoff(state, resume_action="resume_continue")
        return

    state["topic"] = "execution"
    state["stage"] = "provider_ready"

<<<<<<< HEAD
    # Auto-apply cancellation option 3 (no cancellation) silently
    if not state.get("cancellation_applied"):
        await _apply_cancellation_choice(state, state.get("cancellation_choice") or "no", announce=False)
        await chat_server.send_to_browser(
            "Cancellation option 3 (No cancellation) selected automatically.",
            "status",
        )

    # Now ask user for passenger details — specify exactly what's needed
    await _ask_for_missing_passenger_fields(state)


async def _ask_for_missing_passenger_fields(
    state: dict,
    specific_fields: list[str] | None = None,
) -> None:
    """
    Ask the user for passenger details, requesting only what is missing.
    `specific_fields` overrides the auto-detect (used when re-asking after partial fill).
    Sets state['passenger_prompted'] = True so we don't repeat unnecessarily.
    """
    state["passenger_prompted"] = True
    current_pd = state.get("passenger_details") or {}
    required = specific_fields or ["full_name", "email", "phone"]
    missing = [f for f in required if not current_pd.get(f)]
    labels = {
        "full_name": "full name",
        "email": "email address",
        "phone": "mobile number",
        "gender": "gender (male/female)",
    }
    if missing:
        state["awaiting_passenger_fields"] = missing
        ask = ", ".join(labels.get(f, f) for f in missing)
        await chat_server.send_to_browser(
            f"Please share your passenger details: {ask}.",
            "agent",
        )
    else:
        # All details already available — nothing to ask, caller should proceed
        state["awaiting_passenger_fields"] = []


async def _detect_empty_required_fields() -> list[str]:
    """
    Inspect the live booking page and return a list of our field-name keys
    (full_name, email, phone, gender) that appear as visible empty required inputs.
    """
    page = browser_manager.page
    if not page:
        return []
    try:
        result = await page.evaluate(
            """() => {
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const norm = (v) => String(v || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\\s+/g, ' ').trim();
                const inputs = Array.from(document.querySelectorAll('input, textarea')).filter(isVisible);
                const missing = new Set();
                for (const el of inputs) {
                    if (el.closest('#agentic-chat-root')) continue;
                    if (String(el.value || '').trim()) continue;  // already filled
                    const hay = [
                        el.getAttribute('name') || '',
                        el.getAttribute('id') || '',
                        el.getAttribute('placeholder') || '',
                        el.getAttribute('aria-label') || '',
                        el.labels && el.labels[0] ? (el.labels[0].innerText || '') : '',
                    ].join(' ').toLowerCase();

                    if (/email|e-mail/.test(hay)) { missing.add('email'); continue; }
                    if (/phone|mobile|contact|tel/.test(hay)) { missing.add('phone'); continue; }
                    if (/first|fname|given/.test(hay) || /last|lname|surname/.test(hay) || /full.?name|passenger.?name/.test(hay)) {
                        missing.add('full_name'); continue;
                    }
                    if (/gender|title/.test(hay)) { missing.add('gender'); continue; }
                }
                return Array.from(missing);
            }"""
        )
        return result if isinstance(result, list) else []
    except Exception:
        return []
=======
    await chat_server.send_to_browser(
        "Booking page is open. Starting autonomous booking — filling details and progressing through steps...",
        "status",
    )

    result = await _auto_progress_provider_flow(state, user_message="")

    if result.get("payment_reached"):
        return  # message already sent inside the loop

    if result.get("asked_user"):
        return  # message already sent by the step that asked

    if result.get("advanced"):
        state["stage"] = "done"
        await chat_server.send_to_browser(
            "I progressed through booking steps. Continuing to next step...",
            "status",
        )
        return

    # Could not advance — ask for passenger details if we don't have them
    merged = state.get("passenger_details") or {}
    if not merged:
        await chat_server.send_to_browser(
            "Booking page is ready. Please share passenger details (full name, email, mobile number) "
            "and I will fill them and proceed automatically.",
            "agent",
        )
    else:
        await chat_server.send_to_browser(
            "I entered available details and attempted to proceed. "
            "Share any missing field visible on the booking page and I will continue.",
            "agent",
        )
>>>>>>> c7c6b0322d6b2f7c4d5e363bf67a8986ba09c732


def _extract_passenger_details(text: str) -> dict:
    out: dict[str, str] = {}
    s = (text or "").strip()
    if not s:
        return out
    m = re.search(r"\b(?:name\s*(?:is|:)\s*)?([A-Za-z]+(?:\s+[A-Za-z]+){1,3})\b", s, flags=re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
        if len(candidate.split()) >= 2:
            out["full_name"] = " ".join(w.capitalize() for w in candidate.split())
    m = re.search(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", s)
    if m:
        out["email"] = m.group(1).strip()
    m = re.search(r"\b(?:\+?91[\s-]?)?([6-9]\d{9})\b", s)
    if m:
        out["phone"] = m.group(1).strip()
    if re.search(r"\b(male|man)\b", s, flags=re.IGNORECASE):
        out["gender"] = "male"
    elif re.search(r"\b(female|woman)\b", s, flags=re.IGNORECASE):
        out["gender"] = "female"
    return out


def _extract_traveller_names(text: str) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    cleaned = re.sub(r"\b(mr|mrs|ms|miss|dr)\.?\s+", "", s, flags=re.IGNORECASE)
    pattern = r"([A-Za-z]+(?:\s+[A-Za-z]+){1,3})"
    names = []
    for m in re.finditer(pattern, cleaned):
        candidate = " ".join(x.capitalize() for x in m.group(1).split())
        candidate = re.sub(
            r"^(select|choose|pick|add|include|passenger|traveller|traveler)\s+",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip()
        low = candidate.lower()
        if any(k in low for k in ["passenger", "traveller", "adult", "child", "male", "female", "email", "phone"]):
            continue
        if len(candidate.split()) >= 2:
            names.append(candidate)
    # preserve order, unique
    seen = set()
    out = []
    for n in names:
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def _looks_like_passenger_input(text: str) -> bool:
    s = (text or "").lower()
    signals = [
        "name",
        "email",
        "phone",
        "mobile",
        "passenger",
        "traveller",
        "male",
        "female",
        "@",
    ]
    if any(k in s for k in signals):
        return True
    return bool(re.search(r"\b[6-9]\d{9}\b", s))


async def _fill_passenger_details_on_page(details: dict, auto_continue: bool = False) -> dict:
    page = browser_manager.page
    if not page:
        return {"filled": False, "selected": False, "continue_clicked": False}
    try:
        result = await page.evaluate(
            """({ details, autoContinue }) => {
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const clean = (x) => String(x || '').trim();
                const findInput = (patterns) => {
                    const inputs = Array.from(document.querySelectorAll("input, textarea")).filter(isVisible);
                    // Fast-path for semantic types first.
                    if (patterns.some((p) => p.includes('email'))) {
                        const emailTyped = inputs.find((el) =>
                            String(el.getAttribute('type') || '').toLowerCase() === 'email'
                        );
                        if (emailTyped) return emailTyped;
                    }
                    if (patterns.some((p) => p.includes('phone') || p.includes('mobile') || p.includes('contact'))) {
                        const telTyped = inputs.find((el) =>
                            ['tel', 'number'].includes(String(el.getAttribute('type') || '').toLowerCase())
                        );
                        if (telTyped) return telTyped;
                    }
                    for (const el of inputs) {
                        if (el.closest('#agentic-chat-root')) continue;
                        const container = el.closest('div, section, article, form') || el.parentElement || el;
                        const hay = [
                            el.getAttribute('name') || '',
                            el.getAttribute('id') || '',
                            el.getAttribute('placeholder') || '',
                            el.getAttribute('aria-label') || '',
                            el.labels && el.labels[0] ? (el.labels[0].innerText || '') : '',
                            (container.innerText || '').slice(0, 220),
                        ].join(' ').toLowerCase();
                        if (patterns.some((p) => hay.includes(p))) return el;
                    }
                    return null;
                };
                const setVal = (el, value) => {
                    if (!el || !value) return false;
                    el.focus();
                    const proto = Object.getPrototypeOf(el);
                    const valueSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (valueSetter) {
                        valueSetter.call(el, '');
                    } else {
                        el.value = '';
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    if (valueSetter) {
                        valueSetter.call(el, value);
                    } else {
                        el.value = value;
                    }
                    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'a' }));
                    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'a' }));
                    el.dispatchEvent(new Event('beforeinput', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                    return clean(el.value) === clean(value);
                };
                const setByLabel = (labelText, value) => {
                    if (!value) return false;
                    const target = norm(labelText);
                    const labels = Array.from(document.querySelectorAll('label, div, span, p, h4')).filter(isVisible);
                    for (const node of labels) {
                        if (node.closest('#agentic-chat-root')) continue;
                        const txt = norm(node.innerText || node.textContent || '');
                        if (!txt || !txt.includes(target)) continue;
                        const block = node.closest('div, section, article, form') || node.parentElement || node;
                        const field = block.querySelector('input, textarea');
                        if (field && isVisible(field) && setVal(field, value)) return true;
                    }
                    return true;
                };
                const norm = (v) => String(v || '').toLowerCase().replace(/[^a-z]/g, ' ').replace(/\\s+/g, ' ').trim();
                const maybeToggleCheckbox = (name) => {
                    const target = norm(name);
                    if (!target) return false;
                    const labels = Array.from(document.querySelectorAll('label, div, span, p, li')).filter(isVisible);
                    for (const node of labels) {
                        if (node.closest('#agentic-chat-root')) continue;
                        const txt = norm(node.innerText || node.textContent || '');
                        if (!txt || !txt.includes(target)) continue;
                        const box = node.querySelector("input[type='checkbox']") || node.closest('label')?.querySelector("input[type='checkbox']");
                        if (box && !box.checked) {
                            try { box.click(); return true; } catch (e) {}
                        }
                        const nearby = node.parentElement ? node.parentElement.querySelector("input[type='checkbox']") : null;
                        if (nearby && !nearby.checked) {
                            try { nearby.click(); return true; } catch (e) {}
                        }
                        const clickable = node.closest("label, [role='checkbox'], [role='button'], div");
                        if (clickable) {
                            try { clickable.click(); return true; } catch (e) {}
                        }
                    }
                    return false;
                };

                let filledAny = false;
                let selectedAny = false;
                const selectedTravellers = Array.isArray(details.selected_travellers) ? details.selected_travellers : [];
                for (const t of selectedTravellers) {
                    selectedAny = maybeToggleCheckbox(t) || selectedAny;
                }
                const fullName = clean(details.full_name);
                if (fullName) {
                    const parts = fullName.split(/\\s+/);
                    const first = parts[0] || '';
                    const last = parts.slice(1).join(' ') || '';
                    const firstInput = findInput(['first', 'fname', 'given']);
                    const lastInput = findInput(['last', 'lname', 'surname', 'family']);
                    const fullNameInput = findInput(['full name', 'passenger name', 'traveller name']);
                    if (firstInput || lastInput) {
                        filledAny = setVal(firstInput, first) || filledAny;
                        filledAny = setVal(lastInput, last || first) || filledAny;
                    } else if (fullNameInput) {
                        filledAny = setVal(fullNameInput, fullName) || filledAny;
                    }
                }
                if (clean(details.email)) {
                    filledAny = setVal(findInput(['email', 'e-mail']), clean(details.email)) || filledAny;
                    if (!filledAny) {
                        filledAny = setByLabel('email', clean(details.email)) || filledAny;
                    }
                }
                if (clean(details.phone)) {
                    filledAny = setVal(findInput(['phone', 'mobile', 'contact']), clean(details.phone)) || filledAny;
                }
                if (clean(details.gender)) {
                    const label = details.gender.toLowerCase() === 'female' ? 'female' : 'male';
                    const candidates = Array.from(document.querySelectorAll("button, label, [role='button'], [role='radio'], input[type='radio']"))
                        .filter(isVisible)
                        .filter((el) => (el.innerText || el.textContent || el.getAttribute('value') || '').toLowerCase().includes(label));
                    if (candidates.length > 0) {
                        const t = candidates[0];
                        try { t.click(); filledAny = true; } catch (e) {}
                    }
                }

                let continueClicked = false;
                if (autoContinue) {
                    const continueTexts = ['continue', 'proceed', 'next', 'review', 'payment'];
                    const actions = Array.from(document.querySelectorAll("button, a, [role='button']")).filter(isVisible);
                    for (const el of actions) {
                        if (el.closest('#agentic-chat-root')) continue;
                        const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                        if (!txt) continue;
                        if (txt.includes('lock price')) continue;
                        if (!continueTexts.some((k) => txt.includes(k))) continue;
                        try {
                            el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
                            el.click();
                            continueClicked = true;
                            break;
                        } catch (e) {}
                    }
                }
                return { filled: filledAny, selected: selectedAny, continue_clicked: continueClicked };
            }""",
            {"details": details, "autoContinue": bool(auto_continue)},
        )
        return result or {"filled": False, "selected": False, "continue_clicked": False}
    except Exception:
        return {"filled": False, "selected": False, "continue_clicked": False}


def _extract_cancellation_choice(text: str) -> str:
    s = (text or "").strip().lower()
    if not s:
        return ""
    if re.search(r"\b(1|free cancellation)\b", s):
        return "free"
    if re.search(r"\b(2|resched|flex)\b", s):
        return "flex"
    if re.search(r"\b(3|no|don't want|do not want|skip|without cancellation)\b", s):
        return "no"
    return ""


def _compact_text(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return s[:head] + "\n...[truncated]...\n" + s[-tail:]


def _compact_chat_history(limit: int = 30) -> str:
    entries = (chat_server.history or [])[-max(1, limit):]
    lines: list[str] = []
    for item in entries:
        typ = str(item.get("type", "")).strip() or "unknown"
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        lines.append(f"{typ.upper()}: {content}")
    return "\n".join(lines)


async def _get_provider_page_snapshot() -> dict:
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
                    .slice(0, 24)
                    .map((el) => ({
                        type: cleaned(el.getAttribute('type') || el.tagName.toLowerCase()),
                        name: cleaned(el.getAttribute('name')),
                        id: cleaned(el.getAttribute('id')),
                        placeholder: cleaned(el.getAttribute('placeholder')),
                        aria: cleaned(el.getAttribute('aria-label')),
                        value: cleaned(el.value),
                    }));

                const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], [role="radio"], label'))
                    .filter((el) => isVisible(el) && !inChat(el))
                    .map((el) => cleaned(el.innerText || el.textContent || el.getAttribute('aria-label')))
                    .filter(Boolean)
                    .slice(0, 40);

                const bodyText = cleaned(document.body ? (document.body.innerText || document.body.textContent || '') : '');
                return {
                    inputs,
                    buttons,
                    excerpt: bodyText.slice(0, 2600),
                };
            }"""
        )
    except Exception:
        snap = {"inputs": [], "buttons": [], "excerpt": ""}
    return {
        "url": url,
        "title": title,
        "inputs": snap.get("inputs", []) if isinstance(snap, dict) else [],
        "buttons": snap.get("buttons", []) if isinstance(snap, dict) else [],
        "excerpt": snap.get("excerpt", "") if isinstance(snap, dict) else "",
    }


async def _llm_choose_provider_action(
    *,
    user_message: str,
    state: dict,
    merged_details: dict,
) -> dict:
    try:
        llm = get_llm(
            provider=chat_server.selected_provider,
            model=chat_server.selected_model,
        )
        snapshot = await _get_provider_page_snapshot()
        history_text = _compact_chat_history(limit=int(os.getenv("FLIGHT_HISTORY_LINES", "40")))
        prompt = (
            "You are an autonomous flight booking execution controller.\n"
            "Choose the next best action for the current provider booking page.\n\n"
            "Return STRICT JSON only with schema:\n"
            "{"
            "\"action\":\"fill_details|select_cancellation|click_continue|ask_user|wait\","
            "\"reason\":\"string\","
            "\"cancellation_choice\":\"free|flex|no|\","
            "\"message\":\"string\""
            "}\n\n"
            "Rules:\n"
            "- Prefer progressing the booking without asking user again unless blocked by mandatory missing data.\n"
            "- If email/phone/name fields are visible and details exist, choose fill_details.\n"
            "- If cancellation section is visible but uncertain, choose select_cancellation with no by default.\n"
            "- If details seem filled and a Continue/Proceed/Next button is visible, choose click_continue.\n"
            "- Choose ask_user only when required data is missing.\n\n"
            f"Current user message: {user_message}\n"
            f"State cancellation_choice: {state.get('cancellation_choice', '')}\n"
            f"Known merged details JSON: {json.dumps(merged_details, ensure_ascii=True)}\n"
            f"Chat history:\n{_compact_text(history_text, int(os.getenv('FLIGHT_HISTORY_CHARS', '5000')))}\n\n"
            f"Page URL: {snapshot.get('url', '')}\n"
            f"Page title: {snapshot.get('title', '')}\n"
            f"Visible inputs JSON: {json.dumps(snapshot.get('inputs', []), ensure_ascii=True)}\n"
            f"Visible buttons/labels JSON: {json.dumps(snapshot.get('buttons', []), ensure_ascii=True)}\n"
            f"Page excerpt:\n{_compact_text(str(snapshot.get('excerpt', '')), int(os.getenv('FLIGHT_PAGE_CHARS', '3500')))}\n"
        )
        resp = await llm.ainvoke(prompt)
        raw = (resp.content or "").strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _merge_passenger_state(state: dict, text: str) -> dict:
    details = _extract_passenger_details(text)
    traveller_names = _extract_traveller_names(text)
    merged = {**(state.get("passenger_details") or {}), **details}
    if traveller_names:
        existing = state.get("selected_travellers") or []
        merged_names = existing + [n for n in traveller_names if n.lower() not in {x.lower() for x in existing}]
        state["selected_travellers"] = merged_names
        merged["selected_travellers"] = merged_names
    elif state.get("selected_travellers"):
        merged["selected_travellers"] = state.get("selected_travellers")
    state["passenger_details"] = merged
    return merged


async def _llm_plan_provider_actions(
    *,
    user_message: str,
    state: dict,
    merged_details: dict,
) -> list[dict]:
    try:
        llm = get_llm(
            provider=chat_server.selected_provider,
            model=chat_server.selected_model,
        )
        snapshot = await _get_provider_page_snapshot()
        history_text = _compact_chat_history(limit=int(os.getenv("FLIGHT_HISTORY_LINES", "40")))
        payment_signals = ["card number", "cvv", "upi", "net banking", "debit card", "credit card", "pay now", "make payment"]
        page_excerpt_lower = (snapshot.get("excerpt") or "").lower()
        is_payment_page = any(sig in page_excerpt_lower for sig in payment_signals)

        prompt = (
            "You are a flight-booking planner for an autonomous browser agent.\n"
            "Given current page state and chat context, return a short action plan to execute NOW.\n"
            "Return STRICT JSON object only:\n"
            "{"
            "\"plan\":["
            "{\"action\":\"fill_details|select_cancellation|click_continue|ask_user|wait\",\"cancellation_choice\":\"free|flex|no|\",\"message\":\"\"}"
            "]"
            "}\n"
            "Rules:\n"
            "- Max 4 actions.\n"
            "- PAYMENT PAGE: If page contains card number / CVV / UPI / Net Banking fields, return ONLY [{\"action\":\"ask_user\",\"message\":\"payment_reached\"}].\n"
            "- LOGIN PAGE: If page contains OTP / password / mobile login fields, return [{\"action\":\"ask_user\",\"message\":\"login_required\"}].\n"
            "- ADD-ON / UPSELL PAGES (seat selection, meal, travel insurance, baggage, cab, hotel upsell): "
            "return click_continue with a visible skip/no-thanks/proceed button text.\n"
            "- CANCELLATION SECTION: if 'free cancellation' options visible, include select_cancellation with 'no' by default.\n"
            "- PASSENGER FORM: if name/email/phone inputs are visible and empty and details are available, include fill_details then click_continue.\n"
            "- REVIEW / SUMMARY PAGE: if a 'Confirm', 'Continue', 'Proceed to Payment' button is visible, return click_continue.\n"
            "- Prefer progressing without asking user unless mandatory details are truly missing.\n"
            f"Is payment page (auto-detected): {is_payment_page}\n"
            f"Current user message: {user_message}\n"
            f"State stage: {state.get('stage', '')}\n"
            f"State cancellation_choice: {state.get('cancellation_choice', '')}\n"
            f"Merged details JSON: {json.dumps(merged_details, ensure_ascii=True)}\n"
            f"Chat history:\n{_compact_text(history_text, int(os.getenv('FLIGHT_HISTORY_CHARS', '5000')))}\n"
            f"Page URL: {snapshot.get('url', '')}\n"
            f"Page title: {snapshot.get('title', '')}\n"
            f"Visible inputs JSON: {json.dumps(snapshot.get('inputs', []), ensure_ascii=True)}\n"
            f"Visible buttons/labels JSON: {json.dumps(snapshot.get('buttons', []), ensure_ascii=True)}\n"
            f"Page excerpt:\n{_compact_text(str(snapshot.get('excerpt', '')), int(os.getenv('FLIGHT_PAGE_CHARS', '3500')))}\n"
        )
        resp = await llm.ainvoke(prompt)
        raw = (resp.content or "").strip()
        data = json.loads(raw)
        plan = data.get("plan", []) if isinstance(data, dict) else []
        if not isinstance(plan, list):
            return []
        out = []
        allowed = {"fill_details", "select_cancellation", "click_continue", "ask_user", "wait"}
        for step in plan[:4]:
            if not isinstance(step, dict):
                continue
            action = str(step.get("action", "")).strip().lower()
            if action not in allowed:
                continue
            out.append(step)
        return out
    except Exception:
        return []


async def _execute_provider_plan(
    *,
    state: dict,
    merged_details: dict,
    plan: list[dict],
) -> dict:
    did_fill = False
    did_select = False
    did_advance = False
    for step in plan:
        action = str(step.get("action", "")).strip().lower()
        if action == "fill_details" and merged_details:
            result = await _fill_passenger_details_on_page(merged_details, auto_continue=False)
            if result.get("selected"):
                did_select = True
                await chat_server.send_to_browser("Selected traveller(s) from checklist.", "status")
            if result.get("filled"):
                did_fill = True
                await chat_server.send_to_browser("Passenger/contact details entered on the page.", "status")
        elif action == "select_cancellation":
            choice = str(step.get("cancellation_choice", "")).strip().lower() or state.get("cancellation_choice") or "no"
            ok = await _apply_cancellation_choice(state, choice, announce=True)
            did_select = did_select or ok
        elif action == "click_continue":
            advanced = await _click_booking_continue_on_page()
            did_advance = did_advance or advanced
            if advanced:
                break
        elif action == "ask_user":
            msg = str(step.get("message", "")).strip() or "Please share any required details shown on the page so I can continue."
            await chat_server.send_to_browser(msg, "agent")
            return {"asked_user": True, "advanced": False, "filled": did_fill, "selected": did_select}

    return {"asked_user": False, "advanced": did_advance, "filled": did_fill, "selected": did_select}


def _plan_requires_user(plan: list[dict]) -> bool:
    for step in plan:
        if str(step.get("action", "")).strip().lower() == "ask_user":
            return True
    return False


async def _click_booking_step(
    page,
    label: str,
    text_variants: list,
    exclude: list,
) -> bool:
    """
    Playwright-based click for a single hardcoded booking step.
    Searches the full page (not restricted to panels) for the first visible
    button/link whose text matches one of `text_variants` and does NOT contain
    any word in `exclude`.  Sends a status message on success.
    """
    exclude_lower = [e.lower() for e in exclude]

    async def try_el(el) -> bool:
        try:
            if not await el.is_visible(timeout=500):
                return False
            inner = (await el.inner_text()).strip()
            if not inner or len(inner) > 80:
                return False
            inner_lower = inner.lower()
            if any(ex in inner_lower for ex in exclude_lower):
                return False
            await el.scroll_into_view_if_needed()
            await el.click(timeout=3000)
            await chat_server.send_to_browser(
                f"Booking progress: clicked '{inner}' ({label}).",
                "status",
            )
            return True
        except Exception:
            return False

    for text in text_variants:
        # 1. Playwright role-based locator (most reliable for native buttons)
        try:
            loc = page.get_by_role("button", name=text, exact=False)
            count = await loc.count()
            for i in range(count):
                if await try_el(loc.nth(i)):
                    return True
        except Exception:
            pass
        # 2. Any standard clickable element containing the text
        try:
            loc = page.locator("button, a, [role='button']").filter(has_text=text)
            count = await loc.count()
            for i in range(count):
                if await try_el(loc.nth(i)):
                    return True
        except Exception:
            pass
        # 3. React/custom-rendered elements (div, span) — common in Ixigo side panels.
        #    try_el's 80-char inner_text limit filters out parent containers.
        try:
            loc = page.locator("div, span").filter(has_text=text)
            count = await loc.count()
            for i in range(count):
                if await try_el(loc.nth(i)):
                    return True
        except Exception:
            pass

    return False


async def _auto_progress_provider_flow(state: dict, user_message: str) -> dict:
    """
<<<<<<< HEAD
    Hardcoded sequential booking flow (Playwright-based, no JS state machine):
      Start : Passenger details already filled
      Step 1: Check 'No Cancellation' option
      Step 2: Click main 'Continue' button
      Step 3: Click 'Confirm' on the side panel modal
      Step 4: Click 'No, Thanks' in the updated side panel
      Step 5: Click 'Meal Selection' once the page opens
      Step 6: Click 'Continue' 1st time
      Step 7: Click 'Continue' 2nd time
      Step 8: Click 'Continue' 3rd time
      Step 9: Click 'Continue to Pay' in the side panel → stop
=======
    Repeatedly scrape->plan->execute until blocked or iteration cap.
    Stops at: payment page, login gate, genuine missing data, or after max_iters.
    After every fill_details step, always attempts click_continue immediately.
    """
    max_iters = int(os.getenv("FLIGHT_AUTO_PROGRESS_ITERS", "15"))
    if max_iters < 1:
        max_iters = 1
>>>>>>> c7c6b0322d6b2f7c4d5e363bf67a8986ba09c732

    Rules:
    - Always wait 2s before every click attempt.
    - Each step uses Playwright locators on the full page (not panel-restricted).
    - If the button is not found, retry the same step up to 3 more times (4 total),
      each with a 2s wait.  If all 4 attempts fail, stop the flow.
    - Verify each click via the Playwright click return; only advance on success.
    """
    any_advanced = False
    any_filled = False
    any_selected = False
<<<<<<< HEAD

    page = browser_manager.page
    if not page:
        return {"advanced": False, "filled": False, "selected": False, "asked_user": False}

    # Fill passenger details if the form is currently visible
    merged = _merge_passenger_state(state, user_message)
    fill_res = await _fill_passenger_details_on_page(merged, auto_continue=False)
    if fill_res.get("filled"):
        any_filled = True
    if fill_res.get("selected"):
        any_selected = True

    # Step 1: Select 'No Cancellation'
    cancel_choice = state.get("cancellation_choice") or "no"
    cancel_ok = await _apply_cancellation_choice(state, cancel_choice, announce=False)
    if cancel_ok:
        any_selected = True

    # Steps 2-9: (label, text_variants, exclude_words, is_final_step)
    # exclude_words prevents accidentally clicking pay/skip/fee buttons at wrong steps.
    BOOKING_STEPS = [
        (
            "passenger_continue",
            ["Continue", "Proceed", "Next"],
            ["pay", "to pay", "lock price", "skip", "fee"],
            False,
        ),
        (
            "confirm_panel",
            ["Confirm", "Confirm Selection", "Confirm Details"],
            ["pay", "to pay", "skip", "fee"],
            False,
        ),
        (
            "no_thanks_panel",
            ["No, Thanks", "No Thanks", "No thank you", "Don't want",
             "Dont want", "Not now", "Maybe later", "No I don't want"],
            ["pay", "to pay", "payment", "fee"],
            False,
        ),
        (
            "meal_selection",
            ["Meal Selection", "Add Meal", "Select Meal", "Choose Meal"],
            ["pay", "to pay", "skip"],
            False,
        ),
        (
            "continue_1",
            ["Continue", "Proceed", "Next"],
            ["pay", "to pay", "lock price", "skip", "fee"],
            False,
        ),
        (
            "continue_2",
            ["Continue", "Proceed", "Next"],
            ["pay", "to pay", "lock price", "skip", "fee"],
            False,
        ),
        (
            "continue_3",
            ["Continue", "Proceed", "Next"],
            ["pay", "to pay", "lock price", "skip", "fee"],
            False,
        ),
        (
            "continue_to_pay",
            ["Continue to Pay", "Proceed to Pay", "Proceed to Payment", "Continue to Payment"],
            [],
            True,
        ),
    ]

    for label, texts, exclude, is_final in BOOKING_STEPS:
        step_clicked = False

        for attempt in range(4):  # 1 initial + 3 retries
            await asyncio.sleep(2.0)  # Always wait 2s before every click

            ok = await _click_booking_step(page, label, texts, exclude)
            if ok:
                any_advanced = True
                step_clicked = True
                if is_final:
                    await chat_server.send_to_browser(
                        "Flow stopped: 'Continue to Pay' clicked.",
                        "status",
                    )
                    return {
                        "advanced": any_advanced,
                        "filled": any_filled,
                        "selected": any_selected,
                        "asked_user": False,
                    }
                break  # Click verified — advance to next step

        if not step_clicked:
            # Button not found after 4 attempts — stop the flow
            break

=======
    asked_user = False
    payment_reached = False
    consecutive_no_progress = 0

    payment_signals = [
        "card number", "cvv", "upi", "net banking", "debit card",
        "credit card", "pay now", "make payment", "payment method",
    ]

    for iteration in range(max_iters):
        # ── Payment page fast-path (no LLM call needed) ──────────────────────
        snapshot_quick = await _get_provider_page_snapshot()
        excerpt_lower = (snapshot_quick.get("excerpt") or "").lower()
        if any(sig in excerpt_lower for sig in payment_signals):
            payment_reached = True
            state["stage"] = "done"
            await chat_server.send_to_browser(
                "\U0001f4b3 Payment page reached! Please complete payment here to finalise your booking.",
                "agent",
            )
            break

        # ── Login gate fast-path ─────────────────────────────────────────────
        if await _is_login_gate_visible():
            await _enter_human_login_handoff(state, resume_action="resume_continue")
            asked_user = True
            break

        merged = _merge_passenger_state(state, user_message)
        plan = await _llm_plan_provider_actions(
            user_message=user_message,
            state=state,
            merged_details=merged,
        )
        if not plan:
            plan = [{"action": "fill_details"}, {"action": "click_continue"}]

        # ── Intercept LLM ask_user for special signals ───────────────────────
        if _plan_requires_user(plan):
            # Check if it's a payment or login signal from LLM
            for step in plan:
                msg = str(step.get("message", "")).lower()
                if "payment_reached" in msg:
                    payment_reached = True
                    state["stage"] = "done"
                    await chat_server.send_to_browser(
                        "\U0001f4b3 Payment page reached! Please complete payment to finalise your booking.",
                        "agent",
                    )
                    break
                if "login_required" in msg:
                    await _enter_human_login_handoff(state, resume_action="resume_continue")
                    break
            asked_user = True
            break

        result = await _execute_provider_plan(state=state, merged_details=merged, plan=plan)
        any_advanced = any_advanced or bool(result.get("advanced"))
        any_filled = any_filled or bool(result.get("filled"))
        any_selected = any_selected or bool(result.get("selected"))

        if result.get("asked_user"):
            asked_user = True
            break

        # ── After fill_details, always try click_continue immediately ─────────
        if result.get("filled") and not result.get("advanced"):
            await asyncio.sleep(0.6)
            # Also try cancellation before continuing
            if not state.get("cancellation_applied"):
                await _apply_cancellation_choice(state, state.get("cancellation_choice") or "no", announce=False)
            advanced_now = await _click_booking_continue_on_page()
            if advanced_now:
                any_advanced = True
                result["advanced"] = True
                await chat_server.send_to_browser("Details entered and moved to next step.", "status")

        # ── Auto-apply cancellation choice silently ───────────────────────────
        if not state.get("cancellation_applied"):
            target_choice = state.get("cancellation_choice") or "no"
            await _apply_cancellation_choice(state, target_choice, announce=False)

        # ── Progress tracking ─────────────────────────────────────────────────
        if result.get("advanced"):
            consecutive_no_progress = 0
            await asyncio.sleep(1.2)
            continue

        # No forward progress this iteration
        consecutive_no_progress += 1
        if consecutive_no_progress >= 2:
            # Genuinely stuck — stop and let the caller decide
            break
        await asyncio.sleep(0.5)

>>>>>>> c7c6b0322d6b2f7c4d5e363bf67a8986ba09c732
    return {
        "advanced": any_advanced,
        "filled": any_filled,
        "selected": any_selected,
<<<<<<< HEAD
        "asked_user": False,
=======
        "asked_user": asked_user,
        "payment_reached": payment_reached,
>>>>>>> c7c6b0322d6b2f7c4d5e363bf67a8986ba09c732
    }


async def _select_cancellation_on_page(choice: str) -> bool:
    page = browser_manager.page
    if not page:
        return False
    target = (choice or "no").strip().lower()
    if target not in {"free", "flex", "no"}:
        target = "no"
    try:
        # Deterministic ixigo selectors from provider DOM:
        # free -> id="Free Cancellation-radio" (value=0)
        # flex -> id="Free Cancellation + Rescheduling-radio" (value=1)
        # no   -> id="standalone-none-fareType" (value=2)
        direct = await page.evaluate(
            """(target) => {
                const idByTarget = {
                    free: 'Free Cancellation-radio',
                    flex: 'Free Cancellation + Rescheduling-radio',
                    no: 'standalone-none-fareType',
                };
                const expectedValue = { free: '0', flex: '1', no: '2' };
                const groupName = 'fare-type-selection';
                const id = idByTarget[target] || idByTarget.no;
                const input = document.getElementById(id);
                if (!input) return { ok: false, reason: 'id_not_found' };

                const fire = (el, type) => {
                    try {
                        el.dispatchEvent(new Event(type, { bubbles: true }));
                    } catch (e) {}
                };
                const fireMouse = (el, type) => {
                    try {
                        el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                    } catch (e) {}
                };

                const wrapper = input.closest('span, label, section, div') || input;
                try { wrapper.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) {}

                // Click path first for framework handlers.
                try { input.click(); } catch (e) {}
                fireMouse(input, 'mousedown');
                fireMouse(input, 'mouseup');
                fireMouse(input, 'click');
                fire(input, 'input');
                fire(input, 'change');

                // Force checked as last resort and re-emit events.
                if (!input.checked) {
                    try { input.checked = true; } catch (e) {}
                    fire(input, 'input');
                    fire(input, 'change');
                }

                const group = Array.from(document.querySelectorAll("input[type='radio'][name='" + groupName + "']"));
                const checked = group.find((el) => el.checked);
                const ok = !!(checked && String(checked.value) === expectedValue[target]);
                return {
                    ok,
                    checked_value: checked ? String(checked.value) : '',
                    target_value: expectedValue[target],
                };
            }""",
            target,
        )
        if direct and direct.get("ok"):
            return True

        status = await page.evaluate(
            """async (target) => {
                const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const norm = (v) => String(v || '')
                    .toLowerCase()
                    .replace(/[’']/g, "'")
                    .replace(/\\s+/g, ' ')
                    .trim();
                const hasWord = (txt, phrase) => norm(txt).includes(norm(phrase));
                const hasAssuredFeeInSummary = () => {
                    const panels = Array.from(document.querySelectorAll('div, section, article')).filter(isVisible);
                    for (const p of panels) {
                        const txt = norm(p.innerText || p.textContent || '');
                        if (!txt.includes('fare summary')) continue;
                        if (txt.includes('assured fee')) return true;
                    }
                    return false;
                };
                const hasClassState = (el) => {
                    let cur = el;
                    for (let i = 0; i < 4 && cur; i++) {
                        const cls = (cur.className || '').toString().toLowerCase();
                        if (cls.includes('selected') || cls.includes('checked') || cls.includes('active')) return true;
                        if ((cur.getAttribute?.('aria-checked') || '').toLowerCase() === 'true') return true;
                        cur = cur.parentElement;
                    }
                    return false;
                };
                const isSelectedNode = (node) => {
                    const checkedInput = node.querySelector("input[type='radio']:checked, input[type='checkbox']:checked");
                    if (checkedInput) return true;
                    const ariaChecked = node.querySelector("[role='radio'][aria-checked='true'], [role='checkbox'][aria-checked='true']");
                    if (ariaChecked) return true;
                    return hasClassState(node);
                };

                const allContainers = Array.from(document.querySelectorAll('section, article, div')).filter(isVisible);
                const sectionCandidates = allContainers
                    .filter((el) => {
                        const txt = norm(el.innerText || el.textContent || '');
                        return txt.includes('add free cancellation to your trip') && txt.includes('free cancellation');
                    })
                    .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                const section = sectionCandidates[0] || document.body;
                const scopedNodes = Array.from(section.querySelectorAll('label, div, article, section, li, p, span, button'))
                    .filter(isVisible)
                    .filter((el) => !el.closest('#agentic-chat-root'));

                const rowFor = (kind) => {
                    let found = [];
                    for (const el of scopedNodes) {
                        const txt = norm(el.innerText || el.textContent || '');
                        if (!txt) continue;
                        if (kind === 'no') {
                            if (
                                txt.includes("i don't want free cancellation")
                                || txt.includes('do not want free cancellation')
                                || txt.includes('dont want free cancellation')
                                || txt.includes('no cancellation')
                                || txt.includes('without cancellation')
                                || txt.includes('risk')
                            ) found.push(el);
                        } else if (kind === 'flex') {
                            if (txt.includes('free cancellation + rescheduling') || txt.includes('rescheduling')) found.push(el);
                        } else if (kind === 'free') {
                            if (txt.includes('free cancellation') && !txt.includes('rescheduling') && !txt.includes("don't want")) found.push(el);
                        }
                    }
                    found = found
                        .filter((el) => norm(el.innerText || '').length < 280)
                        .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                    return found[0] || null;
                };
                const findClickableRowAncestor = (node, phrase) => {
                    let cur = node;
                    for (let i = 0; i < 8 && cur; i++) {
                        const txt = norm(cur.innerText || cur.textContent || '');
                        const r = cur.getBoundingClientRect();
                        if (
                            txt.includes(phrase) &&
                            r.width > 250 &&
                            r.height >= 24
                        ) {
                            return cur;
                        }
                        cur = cur.parentElement;
                    }
                    return node;
                };

                const clickTarget = async (node) => {
                    if (!node) return false;
                    const clickables = [
                        ...node.querySelectorAll("input[type='radio'], input[type='checkbox']"),
                        ...node.querySelectorAll("[role='radio'], [role='checkbox'], button"),
                    ];
                    if (node.closest('label')) clickables.unshift(node.closest('label'));
                    clickables.push(node);
                    const uniq = [];
                    const seen = new Set();
                    for (const c of clickables) {
                        if (!c || seen.has(c)) continue;
                        seen.add(c);
                        uniq.push(c);
                    }
                    for (const c of uniq) {
                        try {
                            c.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
                            c.click();
                            await sleep(70);
                            if (isSelectedNode(node)) return true;
                        } catch (e) {}
                    }
                    return isSelectedNode(node);
                };

                const rowCandidate = rowFor(target);
                const noPhrase = "i don't want free cancellation";
                const row = rowCandidate
                    ? findClickableRowAncestor(rowCandidate, target === 'no' ? noPhrase : norm(rowCandidate.innerText || ''))
                    : null;
                if (!row) return { section_found: section !== document.body, row_found: false, selected: false };
                if (isSelectedNode(row)) return { section_found: true, row_found: true, selected: true };
                const clicked = await clickTarget(row);
                if (clicked) return { section_found: true, row_found: true, selected: true };

                // Fallback: click near left side of the row where radio indicator usually sits.
                try {
                    const r = row.getBoundingClientRect();
                    const y = Math.round(r.top + Math.min(r.height / 2, 24));
                    const xs = [
                        Math.max(6, Math.round(r.left - 36)),
                        Math.max(6, Math.round(r.left - 24)),
                        Math.max(6, Math.round(r.left - 12)),
                        Math.max(6, Math.round(r.left + 8)),
                        Math.max(6, Math.round(r.left + 18)),
                    ];
                    for (const x of xs) {
                        const hit = document.elementFromPoint(x, y);
                        if (!hit || !isVisible(hit)) continue;
                        hit.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                        hit.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                        hit.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        await sleep(90);
                        if (isSelectedNode(row)) break;
                    }
                } catch (e) {}
                const selectedNow = isSelectedNode(row);
                if (!selectedNow && target === 'no' && !hasAssuredFeeInSummary()) {
                    return { section_found: true, row_found: true, selected: true };
                }
                return { section_found: true, row_found: true, selected: selectedNow };
            }""",
            target,
        )
        return bool(status and status.get("selected"))
    except Exception:
        return False


async def _apply_cancellation_choice(state: dict, choice: str, announce: bool = False) -> bool:
    target = (choice or "no").strip().lower()
    if target not in {"free", "flex", "no"}:
        target = "no"

    for _ in range(3):
        ok = await _select_cancellation_on_page(target)
        if ok:
            state["cancellation_choice"] = target
            state["cancellation_applied"] = True
            if announce:
                label = {
                    "free": "Free Cancellation",
                    "flex": "Free Cancellation + Rescheduling",
                    "no": "I don't want Free Cancellation",
                }.get(target, "I don't want Free Cancellation")
                await chat_server.send_to_browser(f"Selected cancellation option: {label}.", "status")
            return True
        await asyncio.sleep(0.35)
    state["cancellation_choice"] = target
    state["cancellation_applied"] = False
    return False


async def _click_booking_continue_on_page() -> dict:
    """Unified booking progress helper. Detects the current page state/modal
    and clicks the appropriate progress CTA:
    - Main page: clicks standard 'Continue' first, then 'Meal Selection' later.
    - Side panels: clicks 'Confirm' first, then 'No, Thanks', then 'Continue to Pay'.
    """
    page = browser_manager.page
    if not page:
        return {"clicked": False}
    try:
        result = await page.evaluate(
            """async () => {
                const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const inChat = (el) => !!el.closest('#agentic-chat-root');
                const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, ' ').replace(/\\s+/g, ' ').trim();
                const textOf = (el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();

                const findBtn = (roots, keywords) => {
                    for (const root of roots) {
                        const btns = Array.from(root.querySelectorAll('button, a, [role="button"], span, div'))
                            .filter(el => isVisible(el) && !inChat(el));
                        for (const kw of keywords) {
                            for (const btn of btns) {
                                const text = textOf(btn);
                                const t = norm(text);
                                if (t && t.includes(kw) && text.length <= 50) {
                                    if (t.includes('lock price')) continue;
                                    if (t.includes('skip')) continue;
                                    return btn;
                                }
                            }
                        }
                    }
                    return null;
                };

                const findConfirmBtn = (roots) => {
                    for (const root of roots) {
                        const btns = Array.from(root.querySelectorAll('button, a, [role="button"], span, div'))
                            .filter(el => isVisible(el) && !inChat(el));
                        for (const btn of btns) {
                            const text = textOf(btn);
                            const t = norm(text);
                            if (t && text.length <= 50) {
                                if (t === 'confirm' || t.startsWith('confirm ') || t === 'confirm selection' || t === 'confirm details') {
                                    return btn;
                                }
                            }
                        }
                    }
                    return null;
                };

                const findPayBtn = (roots) => {
                    for (const root of roots) {
                        const btns = Array.from(root.querySelectorAll('button, a, [role="button"], span, div'))
                            .filter(el => isVisible(el) && !inChat(el));
                        for (const btn of btns) {
                            const text = textOf(btn);
                            const t = norm(text);
                            if (t && text.length <= 50) {
                                const isPay = t.includes('continue to pay') ||
                                              t.includes('proceed to pay') ||
                                              t.includes('proceed to payment') ||
                                              t.includes('continue to payment') ||
                                              t === 'pay now' ||
                                              t === 'pay';
                                const isNotPayBtn = t.includes('fee') || t.includes('cancel') || t.includes('reschedule') || t.includes('skip');
                                if (isPay && !isNotPayBtn) {
                                    return btn;
                                }
                            }
                        }
                    }
                    return null;
                };

                const clickEl = async (el) => {
                    if (!el) return false;
                    try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) {}
                    try {
                        el.click();
                    } catch (e) {
                        try {
                            el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                            el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        } catch (e2) {
                            return false;
                        }
                    }
                    await sleep(600);
                    return true;
                };

                // Get current state from sessionStorage
                let step = sessionStorage.getItem('ixigo_booking_step') || 'start';

                // Look for side panels
                const panelRoots = Array.from(document.querySelectorAll(
                    'aside, [role="dialog"], [aria-modal="true"], .drawer, .modal, .sidebar, .panel, [class*="side"], [class*="drawer"], [class*="panel"]'
                )).filter(isVisible);

                const confirmBtn = findConfirmBtn(panelRoots.length > 0 ? panelRoots : [document.body]);
                const noThanksBtn = findBtn(panelRoots.length > 0 ? panelRoots : [document.body], ['no thanks', 'dont want']);
                const mealBtn = findBtn([document.body], ['meal selection']);
                const payBtn = findPayBtn(panelRoots.length > 0 ? panelRoots : [document.body]);
                const continueBtn = findBtn([document.body], ['continue', 'proceed', 'next']);

                if (step === 'start') {
                    // Start by clicking the main passenger form Continue button
                    if (continueBtn) {
                        if (await clickEl(continueBtn)) {
                            sessionStorage.setItem('ixigo_booking_step', 'continue_clicked');
                            return { clicked: 'passenger_continue', text: textOf(continueBtn), stop_flow: false };
                        }
                    }
                }

                if (step === 'continue_clicked') {
                    // Click Confirm in side panel
                    if (confirmBtn) {
                        if (await clickEl(confirmBtn)) {
                            sessionStorage.setItem('ixigo_booking_step', 'confirm_clicked');
                            return { clicked: 'confirm_side_panel', text: textOf(confirmBtn), stop_flow: false };
                        }
                    }
                    // If side panel skip confirm and goes to no thanks directly
                    if (noThanksBtn) {
                        if (await clickEl(noThanksBtn)) {
                            sessionStorage.setItem('ixigo_booking_step', 'no_thanks_clicked');
                            return { clicked: 'no_thanks_side_panel', text: textOf(noThanksBtn), stop_flow: false };
                        }
                    }
                }

                if (step === 'confirm_clicked') {
                    // Click No Thanks in side panel
                    if (noThanksBtn) {
                        if (await clickEl(noThanksBtn)) {
                            sessionStorage.setItem('ixigo_booking_step', 'no_thanks_clicked');
                            return { clicked: 'no_thanks_side_panel', text: textOf(noThanksBtn), stop_flow: false };
                        }
                    }
                }

                if (step === 'no_thanks_clicked') {
                    if (mealBtn) {
                        if (await clickEl(mealBtn)) {
                            sessionStorage.setItem('ixigo_booking_step', 'meal_clicked');
                            return { clicked: 'meal_selection', text: textOf(mealBtn), stop_flow: false };
                        }
                    } else {
                        if (continueBtn) {
                            if (await clickEl(continueBtn)) {
                                sessionStorage.setItem('ixigo_booking_step', 'continue_1');
                                return { clicked: 'continue_1', text: textOf(continueBtn), stop_flow: false };
                            }
                        }
                    }
                }

                if (step === 'meal_clicked') {
                    if (continueBtn) {
                        if (await clickEl(continueBtn)) {
                            sessionStorage.setItem('ixigo_booking_step', 'continue_1');
                            return { clicked: 'continue_1', text: textOf(continueBtn), stop_flow: false };
                        }
                    }
                }

                if (step === 'continue_1') {
                    if (continueBtn) {
                        if (await clickEl(continueBtn)) {
                            sessionStorage.setItem('ixigo_booking_step', 'continue_2');
                            return { clicked: 'continue_2', text: textOf(continueBtn), stop_flow: false };
                        }
                    }
                }

                if (step === 'continue_2') {
                    if (continueBtn) {
                        if (await clickEl(continueBtn)) {
                            sessionStorage.setItem('ixigo_booking_step', 'continue_3');
                            return { clicked: 'continue_3', text: textOf(continueBtn), stop_flow: false };
                        }
                    }
                }

                if (step === 'continue_3' || step === 'continue_2' || step === 'continue_1') {
                    if (payBtn) {
                        if (await clickEl(payBtn)) {
                            sessionStorage.setItem('ixigo_booking_step', 'pay_clicked');
                            return { clicked: 'continue_to_pay', text: textOf(payBtn), stop_flow: true };
                        }
                    }
                }

                // Generic fallback if we get stuck or state is out of sync
                if (payBtn) {
                    if (await clickEl(payBtn)) {
                        sessionStorage.setItem('ixigo_booking_step', 'pay_clicked');
                        return { clicked: 'pay_fallback', text: textOf(payBtn), stop_flow: true };
                    }
                }
                if (continueBtn) {
                    if (await clickEl(continueBtn)) {
                        return { clicked: 'continue_fallback', text: textOf(continueBtn), stop_flow: false };
                    }
                }

                return null;
            }"""
        )
        if result and isinstance(result, dict) and result.get("clicked"):
            await chat_server.send_to_browser(
                f"Booking progress: clicked '{result.get('text')}' ({result.get('clicked')}).",
                "status",
            )
            return {"clicked": True, "stop_flow": bool(result.get("stop_flow"))}
    except Exception:
        pass
    return {"clicked": False}


async def _click_side_panel_sequence() -> list[str]:
    """
    Try right-side panel/modal CTAs in sequence:
    Confirm -> No Thanks -> Continue/Proceed/Payment.
    Returns list of clicked labels.
    """
    page = browser_manager.page
    if not page:
        return []
    try:
        clicked = await page.evaluate(
            """async () => {
                const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    return !!r && r.width > 8 && r.height > 8;
                };
                const inChat = (el) => !!el.closest('#agentic-chat-root');
                const textOf = (el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
                const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
                const clickEl = async (el) => {
                    if (!el || !isVisible(el) || inChat(el)) return false;
                    try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) {}
                    try {
                        el.click();
                    } catch (e) {
                        try {
                            el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                            el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        } catch (e2) {
                            return false;
                        }
                    }
                    await sleep(500);
                    return true;
                };

                const containers = Array.from(document.querySelectorAll('aside, [role="dialog"], [aria-modal="true"], .drawer, .modal, .sidebar, .panel, section, div'));
                const clickableIn = (root) => Array.from(root.querySelectorAll('button, a, [role="button"], span, div'))
                    .filter((el) => isVisible(el) && !inChat(el))
                    .filter((el) => {
                        const t = norm(textOf(el));
                        return !!t && t.length <= 80;
                    });

                const priority = ['confirm', 'no thanks', 'continue', 'proceed', 'next', 'payment', 'meal selection', 'skip'];
                const clicked = [];

                // Two passes allow newly opened prompts (e.g., Confirm -> No Thanks)
                for (let pass = 0; pass < 2; pass++) {
                    let didAny = false;
                    const roots = [document.body, ...containers];
                    for (const key of priority) {
                        let target = null;
                        for (const root of roots) {
                            const cands = clickableIn(root);
                            for (const el of cands) {
                                const t = norm(textOf(el));
                                if (!t.includes(key)) continue;
                                if (t.includes('lock price')) continue;
                                target = el;
                                break;
                            }
                            if (target) break;
                        }
                        if (target && await clickEl(target)) {
                            clicked.push(key);
                            didAny = true;
                        }
                    }
                    if (!didAny) break;
                }
                return clicked;
            }"""
        )
        if isinstance(clicked, list):
            return [str(x) for x in clicked if str(x).strip()]
    except Exception:
        return []
    return []


async def _ensure_cancellation_prompt(state: dict) -> None:
    if state.get("cancellation_prompted"):
        return
    state["cancellation_prompted"] = True
    await chat_server.send_to_browser(
        (
            "Cancellation add-on checklist:\n"
            "1. Free Cancellation\n"
            "2. Free Cancellation + Rescheduling\n"
            "3. I don't want Free Cancellation (default)\n"
            "Default is being applied automatically now. Reply only if you want to override with 1 or 2."
        ),
        "agent",
    )
    selected = await _apply_cancellation_choice(state, "no", announce=False)
    if selected:
        await chat_server.send_to_browser(
            "Default applied: option 3 selected (I don't want Free Cancellation).",
            "status",
        )
    else:
        await chat_server.send_to_browser(
            "I could not confirm cancellation default click yet. I will retry before final continue.",
            "status",
        )


def _start_login_monitor() -> None:
    global _flight_login_task
    _cancel_login_monitor()
    _flight_login_task = asyncio.create_task(_wait_for_login_and_proceed())


async def _wait_for_login_and_proceed() -> None:
    try:
        for _ in range(300):  # ~10 minutes
            await asyncio.sleep(2)
            state = chat_server.flight_booking_state or {}
            if not state.get("active"):
                return
            if state.get("stage") != "awaiting_login":
                return
            if await _is_login_gate_visible():
                continue

            await _resume_after_login(state)
            return

        await chat_server.send_to_browser(
            "Still waiting for login completion. Once you log in, I will continue automatically.",
            "status",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Keep flow resilient even if polling fails.
        pass
    finally:
        global _flight_login_task
        _flight_login_task = None


async def _llm_extract_fields(text: str, provider: str = "", model: str = "") -> dict:
    """
    LLM fallback parser for natural-language flight booking details.
    Returns normalized partial fields.
    """
    try:
        llm = get_llm(
            provider=provider or chat_server.selected_provider,
            model=model or chat_server.selected_model,
        )
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


async def _extract_clickable_ixigo_options() -> list[dict]:
    """
    Extract visible clickable flight actions from Ixigo results page.
    Options are rank-based so user choices can be re-resolved against
    the latest DOM at click time.
    """
    await asyncio.sleep(1.5)
    page = browser_manager.page
    if not page:
        return []
    try:
        options = await page.evaluate(
            """() => {
                const isVisible = (el) => {
                    const cs = window.getComputedStyle(el);
                    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity || '1') === 0) return false;
                    const r = el.getBoundingClientRect();
                    if (!r || r.width < 8 || r.height < 8) return false;
                    if (r.bottom < 0 || r.top > window.innerHeight) return false;
                    if (r.right < 0 || r.left > window.innerWidth) return false;
                    return true;
                };

                const getActionType = (el) => {
                    const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    if (!txt) return '';
                    if (txt.includes('lock price')) return '';
                    if (txt === 'book' || txt.startsWith('book ')) return 'book';
                    if (txt === 'select' || txt.startsWith('select ')) return 'select';
                    if (txt.includes('view deal')) return 'deal';
                    return '';
                };

                const isAction = (el) => {
                    const actionType = getActionType(el);
                    if (!actionType) return false;
                    const tag = (el.tagName || '').toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const enabled = !el.hasAttribute('disabled') && (el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
                    if (!enabled) return false;
                    if (el.closest('#agentic-chat-root')) return false;
                    return tag === 'button' || tag === 'a' || role === 'button';
                };

                const cardLabel = (el) => {
                    const card = el.closest('article, li, [class*="flight"], [class*="result"], [data-testid*="flight"], [class*="item"]');
                    const raw = (card?.innerText || el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const cleaned = raw
                        .replace(/\\b(book|select|view deal|details)\\b/ig, ' ')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    return cleaned.slice(0, 140);
                };

                const nodes = Array.from(document.querySelectorAll("button, a, [role='button']"));
                const candidates = [];
                const seen = new Set();
                for (const el of nodes) {
                    if (!isAction(el) || !isVisible(el)) continue;
                    const r = el.getBoundingClientRect();
                    const actionType = getActionType(el);
                    const key = `${Math.round(r.left)}:${Math.round(r.top)}:${Math.round(r.width)}:${Math.round(r.height)}:${(el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase()}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    candidates.push({
                        el,
                        actionType,
                        top: r.top,
                        left: r.left,
                    });
                }

                const actionPriority = (t) => (t === 'book' ? 0 : t === 'select' ? 1 : 2);
                candidates.sort((a, b) =>
                    (actionPriority(a.actionType) - actionPriority(b.actionType)) ||
                    (a.top - b.top) ||
                    (a.left - b.left)
                );
                const out = [];
                for (let i = 0; i < candidates.length && out.length < 8; i++) {
                    const el = candidates[i].el;
                    const label = cardLabel(el) || `Option ${out.length + 1}`;
                    out.push({
                        label,
                        rank: out.length,
                        kind: 'ixigo_click',
                    });
                }
                return out;
            }"""
        )
        return options or []
    except Exception:
        return []


async def _count_clickable_ixigo_options(limit: int = 8) -> int:
    options = await _extract_clickable_ixigo_options()
    if not options:
        return 0
    return min(len(options), max(1, int(limit)))


async def _click_option_choice(selected: dict) -> bool:
    """
    Execute a selected option from state.
    """
    page = browser_manager.page
    if not page:
        return False

    if selected.get("kind") == "external_url" and selected.get("url"):
        await browser_manager.navigate(selected["url"])
        return True

    # Resolve click by current on-screen rank to avoid stale DOM ids/indexes.
    rank = selected.get("rank")
    if isinstance(rank, int) and rank >= 0:
        clicked = await _click_ixigo_option_by_rank(rank)
        if clicked:
            return True
        # One refresh retry for late Ixigo rerenders.
        await asyncio.sleep(1.2)
        return await _click_ixigo_option_by_rank(rank)

    return False


async def _click_ixigo_option_by_rank(rank: int) -> bool:
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
                    if (!r || r.width < 8 || r.height < 8) return false;
                    if (r.bottom < 0 || r.top > window.innerHeight) return false;
                    if (r.right < 0 || r.left > window.innerWidth) return false;
                    return true;
                };
                const getActionType = (el) => {
                    const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    if (!txt) return '';
                    if (txt.includes('lock price')) return '';
                    if (txt === 'book' || txt.startsWith('book ')) return 'book';
                    if (txt === 'select' || txt.startsWith('select ')) return 'select';
                    if (txt.includes('view deal')) return 'deal';
                    return '';
                };
                const isAction = (el) => {
                    const actionType = getActionType(el);
                    if (!actionType) return false;
                    if (el.closest('#agentic-chat-root')) return false;
                    if (el.hasAttribute('disabled')) return false;
                    if ((el.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
                    const tag = (el.tagName || '').toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    return tag === 'button' || tag === 'a' || role === 'button';
                };

                const nodes = Array.from(document.querySelectorAll("button, a, [role='button']"))
                    .filter((el) => isAction(el) && isVisible(el))
                    .map((el) => ({ el, rect: el.getBoundingClientRect(), actionType: getActionType(el) }));

                const dedup = [];
                const seen = new Set();
                for (const n of nodes) {
                    const txt = (n.el.innerText || n.el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const key = `${Math.round(n.rect.left)}:${Math.round(n.rect.top)}:${Math.round(n.rect.width)}:${Math.round(n.rect.height)}:${txt}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    dedup.push(n);
                }

                const actionPriority = (t) => (t === 'book' ? 0 : t === 'select' ? 1 : 2);
                dedup.sort((a, b) =>
                    (actionPriority(a.actionType) - actionPriority(b.actionType)) ||
                    (a.rect.top - b.rect.top) ||
                    (a.rect.left - b.rect.left)
                );
                if (targetRank < 0 || targetRank >= dedup.length) return { ok: false, reason: 'out_of_range', count: dedup.length };

                const target = dedup[targetRank].el;
                target.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
                try {
                    target.click();
                    return { ok: true, count: dedup.length };
                } catch (e) {
                    try {
                        target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                        target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                        target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        return { ok: true, count: dedup.length };
                    } catch (e2) {
                        return { ok: false, reason: 'click_failed', count: dedup.length };
                    }
                }
            }""",
            rank,
        )
        if result and result.get("ok"):
            await asyncio.sleep(2.0)
            return True
    except Exception:
        return False
    return False


async def _attempt_cheapest_click() -> dict:
    """
    Try cheapest auto-flow first, then fallback to first ranked visible Book action.
    """
    result = await proceed_with_first_ixigo_option()
    if result.get("clicked"):
        result["fallback_rank_click"] = False
        return result

    # Fallback for pages where cheapest card CTA exists but initial click misses.
    fallback_clicked = await _click_ixigo_option_by_rank(0)
    result["fallback_rank_click"] = bool(fallback_clicked)
    if fallback_clicked:
        result["clicked"] = True
    return result


async def _open_first_available_flight_offer() -> None:
    """
    Try to open a flight offer card so provider links become visible.
    """
    page = browser_manager.page
    if not page:
        return
    selectors = [
        "button:has-text('Book')",
        "button:has-text('Select')",
        "button:has-text('View deal')",
        "[role='button']:has-text('Book')",
        "a:has-text('Book')",
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
    idx = _extract_choice_number(text)
    if idx <= 0:
        return 0
    if 1 <= idx <= max_n:
        return idx
    return 0


def _extract_choice_number(text: str) -> int:
    m = re.search(r"\b(?:option|pick|choose|go with)\s*(\d+)\b", text, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d+)\b", text, flags=re.IGNORECASE)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except Exception:
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
        _cancel_login_monitor()
        chat_server.flight_booking_state = _default_state()
        await chat_server.send_to_browser("Flight booking flow cancelled.", "system")
        page = browser_manager.page
        if page:
            try:
                await page.evaluate("sessionStorage.removeItem('ixigo_booking_step')")
            except Exception:
                pass
        return True

    is_intent = _is_flight_intent(text)
    if not state.get("active") and not is_intent:
        return False

    # Reset stale flow on a clearly new flight request.
    if state.get("active") and _is_new_flight_request(text):
        _cancel_login_monitor()
        state = _default_state()
        chat_server.flight_booking_state = state
        page = browser_manager.page
        if page:
            try:
                await page.evaluate("sessionStorage.removeItem('ixigo_booking_step')")
            except Exception:
                pass

    if state.get("stage") == "awaiting_login":
        if text.lower() in {"logged in", "i logged in", "done", "continue"}:
            if await _is_login_gate_visible():
                await chat_server.send_to_browser(
                    "I still see the login step on page. Please complete login and I will proceed automatically.",
                    "agent",
                )
            else:
                _cancel_login_monitor()
                await _resume_after_login(state)
            return True
        await chat_server.send_to_browser(
            "Please complete login on the opened page. I will auto-detect and continue.",
            "agent",
        )
        return True

    if state.get("stage") == "done":
        if _is_new_flight_request(text):
            _cancel_login_monitor()
            state = _default_state()
            chat_server.flight_booking_state = state
        else:
            result = await _auto_progress_provider_flow(state, text)
            if result.get("asked_user"):
                return True
            if result.get("advanced"):
                await chat_server.send_to_browser("Done. I continued to the next booking step.", "status")
                return True
            merged = state.get("passenger_details") or {}
            if not result.get("filled") and not merged and not _looks_like_passenger_input(text):
                await chat_server.send_to_browser(
                    "Share required details shown on page (for example email/date of birth) and I will fill them now.",
                    "agent",
                )
                return True
            await chat_server.send_to_browser(
                "I evaluated the current page and could not advance yet. Share the missing field visible on screen and I will fill it.",
                "agent",
            )
            return True

    if state.get("stage") == "provider_ready":
        if _is_new_flight_request(text):
            _cancel_login_monitor()
            state = _default_state()
            chat_server.flight_booking_state = state
        else:
            # Allow user to override cancellation choice
            cancellation_choice = _extract_cancellation_choice(text)
            if cancellation_choice and cancellation_choice != state.get("cancellation_choice"):
                picked = await _apply_cancellation_choice(state, cancellation_choice, announce=True)
                if not picked:
                    label = {
                        "free": "Free Cancellation",
                        "flex": "Free Cancellation + Rescheduling",
                        "no": "I don't want Free Cancellation",
                    }.get(cancellation_choice, "I don't want Free Cancellation")
                    await chat_server.send_to_browser(
                        f"Noted: {label}. I will apply it now.",
                        "status",
                    )

            # Merge whatever passenger details user just sent
            if _looks_like_passenger_input(text):
                _merge_passenger_state(state, text)

            # Determine which fields are still awaited
            needed = state.get("awaiting_passenger_fields") or []
            current_pd = state.get("passenger_details") or {}
            still_missing = [f for f in needed if not current_pd.get(f)]

            if still_missing:
                # User provided some — try to fill what we have, then ask for the rest
                merged = _merge_passenger_state(state, text)
                await _fill_passenger_details_on_page(merged, auto_continue=False)
                still_missing = [f for f in still_missing if not merged.get(f)]
                if still_missing:
                    await _ask_for_missing_passenger_fields(state, specific_fields=still_missing)
                    return True
                # All collected — try to proceed
                state["awaiting_passenger_fields"] = []
            elif not state.get("passenger_prompted"):
                # Haven't asked yet — ask now
                await _ask_for_missing_passenger_fields(state)
                return True

            # We have passenger details — fill page and proceed
            merged = _merge_passenger_state(state, text)
            fill_result = await _fill_passenger_details_on_page(merged, auto_continue=False)

            # Check for any form fields on page that are still empty
            page_missing = await _detect_empty_required_fields()
            if page_missing:
                state["awaiting_passenger_fields"] = page_missing
                labels = {
                    "full_name": "full name",
                    "email": "email address",
                    "phone": "mobile number",
                    "gender": "gender (male/female)",
                }
                ask = ", ".join(labels.get(f, f) for f in page_missing)
                await chat_server.send_to_browser(
                    f"Some required fields are still empty: {ask}. Please share them and I will fill and continue.",
                    "agent",
                )
                return True

            # Everything filled — run auto-progress flow to fill details, check cancellation, click Continue, No thanks, Meals, and Pay.
            result = await _auto_progress_provider_flow(state, text)
            if result.get("advanced"):
                state["stage"] = "done"
                await chat_server.send_to_browser(
                    "Details filled, cancellation choice checked, and progressed through subsequent steps to payment.",
                    "status",
                )
            else:
                await chat_server.send_to_browser(
                    "Details entered. If there's a Continue/Proceed button visible, I'll click it — or let me know what to do next.",
                    "agent",
                )
            return True

    # Start or continue flow
    first_activation = not state.get("active")
    if first_activation:
        state["active"] = True
        state["stage"] = "collecting"

    state = _merge_state(state, _extract_fields(text))
    # Also capture passenger details if given in the same message
    if _looks_like_passenger_input(text):
        pd = _extract_passenger_details(text)
        if pd:
            existing_pd = state.get("passenger_details") or {}
            state["passenger_details"] = {**existing_pd, **pd}
    missing_after_rules = _missing_fields(state)
    if missing_after_rules:
        # LLM reasoning fallback for ambiguous city/date phrasing.
        llm_fields = await _llm_extract_fields(
            text,
            provider=chat_server.selected_provider,
            model=chat_server.selected_model,
        )
        if llm_fields:
            state = _merge_state(state, llm_fields)
    # If this was the first activation and flight fields are still missing,
    # now show the welcome/guidance message.
    if first_activation and _missing_fields(state):
        await chat_server.send_to_browser(
            "I can help with flight booking. Share details naturally, for example: Pune to Delhi on 18 May for 2 adults. I will handle city spellings and format everything correctly.",
            "agent",
        )

    # If already compared, check choice
    if state.get("stage") == "compared":
        options = state.get("options", [])
        if _wants_cheapest_booking(text):
            state["book_cheapest"] = True
        if state.get("book_cheapest"):
            await chat_server.send_to_browser(
                "Cheapest-flight request confirmed. Sorting by cheapest and proceeding with the first option.",
                "status",
            )
            result = await _attempt_cheapest_click()
            await chat_server.send_to_browser(
                (
                    "Cheapest option flow executed."
                    f" Sorted: {'yes' if result.get('sorted') else 'no'}"
                    f", Option clicked: {'yes' if result.get('clicked') else 'no'}."
                    + (" (fallback click used)" if result.get("fallback_rank_click") else "")
                ),
                "agent",
            )
            if result.get("clicked"):
                await _handle_provider_page_after_open(state)
            else:
                if await _is_login_gate_visible():
                    await _enter_human_login_handoff(state, resume_action="resume_cheapest")
                else:
                    state["stage"] = "compared"
                    await chat_server.send_to_browser(
                        "I could not click cheapest yet. I kept the results page active, so say 'select cheapest and proceed' to retry immediately.",
                        "agent",
                    )
            return True

        raw_choice = _extract_choice_number(text)
        choice = _parse_choice(text, len(options))
        direct_url = re.search(r"https?://\S+", text)
        if choice:
            selected = options[choice - 1]
            await chat_server.send_to_browser(
                f"Opening option {choice}: {selected.get('label', 'selected provider')}", "status"
            )
            clicked = await _click_option_choice(selected)
            if clicked:
                await chat_server.send_to_browser("Done. I clicked your selected option.", "status")
                await _handle_provider_page_after_open(state)
                return True
            if await _is_login_gate_visible():
                await _enter_human_login_handoff(
                    state,
                    resume_action="resume_selected",
                    pending_option=selected,
                )
                return True
            await chat_server.send_to_browser(
                "I could not click that option automatically. Please select it on the page and share the opened booking URL.",
                "agent",
            )
            return True
        if raw_choice > 0 and options:
            await chat_server.send_to_browser(
                f"I found {len(options)} option(s) right now. Please choose between option 1 and option {len(options)}.",
                "agent",
            )
            return True
        if raw_choice > 0 and not options:
            clicked = await _click_ixigo_option_by_rank(raw_choice - 1)
            if clicked:
                await chat_server.send_to_browser("Done. I clicked your selected option.", "status")
                await _handle_provider_page_after_open(state)
                return True
            if await _is_login_gate_visible():
                await _enter_human_login_handoff(
                    state,
                    resume_action="resume_selected",
                    pending_option={"rank": raw_choice - 1, "kind": "ixigo_click", "label": f"Option {raw_choice}"},
                )
                return True
            await chat_server.send_to_browser(
                "I could not find that option on screen right now. Please scroll flight results and try again with option number.",
                "agent",
            )
            return True
        if direct_url:
            await chat_server.send_to_browser("Opening your provided booking URL now.", "status")
            await browser_manager.navigate(direct_url.group(0))
            await _handle_provider_page_after_open(state)
            return True
        await chat_server.send_to_browser(
            "Please tell me which flight option you want, for example 'option 1', and I will continue.",
            "agent",
        )
        return True

    missing = _missing_fields(state)
    if missing:
        prompts = {
            "origin": "departure city or airport",
            "destination": "destination city or airport",
            "depart_date": "departure date",
            "return_date": "return date",
        }
        ask = ", ".join(prompts[m] for m in missing)
        await chat_server.send_to_browser(
            f"I need a bit more information: {ask}. You can write it naturally in one line.",
            "agent",
        )
        return True

    # Passenger details are collected at the booking stage (provider_ready),
    # not upfront — go straight to Ixigo search.

    # Run comparison
    fallback_url = build_ixigo_results_url(state)
    date_match = re.search(r"[?&]date=(\d{8})\b", fallback_url)
    compact_date = date_match.group(1) if date_match else "DDMMYYYY"
    await chat_server.send_to_browser(
        (
            "Opening Ixigo homepage and entering details manually now:\n"
            f"- From: {state['origin']}\n"
            f"- To: {state['destination']}\n"
            f"- Date: {state['depart_date']} (Ixigo URL format: {compact_date})"
            + (f"\n- Return: {state['return_date']}" if state.get("return_date") else "")
            + f"\n- Adults: {state.get('adults', 1)}\n"
            "Proceeding to search flights."
        ),
        "status",
    )
    search_exec = await open_ixigo_and_search_from_home(state)
    if search_exec.get("manual_search_submitted"):
        if search_exec.get("normalized_date_applied"):
            await chat_server.send_to_browser(
                "Ixigo search was submitted from the homepage form. Final results URL date was normalized to DDMMYYYY.",
                "status",
            )
        else:
            await chat_server.send_to_browser(
                "Ixigo search was submitted from the homepage form after entering values on-page.",
                "status",
            )
    elif search_exec.get("fallback_used"):
        await chat_server.send_to_browser(
            (
                "Ixigo form interaction partially succeeded, then I continued with the equivalent search URL "
                f"using date format {compact_date}."
            ),
            "status",
        )

    options = await _extract_clickable_ixigo_options()
    if not options and state.get("book_cheapest"):
        # For cheapest auto-book, one click attempt to expose proceed actions.
        await _open_first_available_flight_offer()
        options = await _extract_clickable_ixigo_options()
    # Secondary fallback: provider links (kept only as backup).
    if not options:
        link_options = await _extract_booking_options()
        options = [{**opt, "kind": "external_url"} for opt in link_options]
    if not options:
        visible_count = await _count_clickable_ixigo_options(limit=6)
        if visible_count > 0:
            options = [
                {"label": f"Option {i}", "rank": i - 1, "kind": "ixigo_click"}
                for i in range(1, visible_count + 1)
            ]
    state["options"] = options
    state["stage"] = "compared"
    page = browser_manager.page
    if page:
        try:
            await page.evaluate("sessionStorage.removeItem('ixigo_booking_step')")
        except Exception:
            pass

    summary = [
        "Flight comparison is ready on Ixigo.",
        f"Route: {state['origin']} → {state['destination']}",
        f"Date: {state['depart_date']}" + (f" | Return: {state['return_date']}" if state.get("return_date") else ""),
        f"Passengers: {state.get('adults', 1)} adult(s)",
    ]
    if options:
        # Always auto-select cheapest by default
        await chat_server.send_to_browser(
            "Selecting the cheapest available flight automatically.",
            "status",
        )
        result = await _attempt_cheapest_click()
        await chat_server.send_to_browser(
            (
                "Cheapest flight selected."
                f" Sorted: {'yes' if result.get('sorted') else 'no'}"
                f", Clicked: {'yes' if result.get('clicked') else 'no'}."
                + (" (fallback used)" if result.get("fallback_rank_click") else "")
            ),
            "status",
        )
        if result.get("clicked"):
            await _handle_provider_page_after_open(state)
        else:
            if await _is_login_gate_visible():
                await _enter_human_login_handoff(state, resume_action="resume_cheapest")
            else:
                state["stage"] = "compared"
                # Show options as fallback so user can pick manually
                summary.append("Could not auto-select. Please choose an option:")
                for i, opt in enumerate(options, start=1):
                    if opt.get("kind") == "external_url" and opt.get("url"):
                        summary.append(f"{i}. {opt.get('label', 'Option')} — {opt.get('url')}")
                    else:
                        summary.append(f"{i}. {opt.get('label', 'Option')}")
                summary.append("Tell me which one to proceed with, for example 'option 1'.")
                await chat_server.send_to_browser("\n".join(summary), "agent")
        return True
    else:
        summary.append("No clickable options found yet. Retrying...")
        await chat_server.send_to_browser("\n".join(summary), "status")
        # One retry after a short wait
        await asyncio.sleep(3)
        options = await _extract_clickable_ixigo_options()
        if options:
            result = await _attempt_cheapest_click()
            if result.get("clicked"):
                await _handle_provider_page_after_open(state)
                return True
        await chat_server.send_to_browser(
            "Results loaded. Say 'book cheapest' to retry, or pick an option number if shown.",
            "agent",
        )
    return True
