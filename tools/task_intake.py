from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from agent.llm_provider import get_llm
from tools.chat_server import chat_server


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


def _default_intake_state() -> dict[str, Any]:
    return {
        "active": False,
        "intent": "",
        "awaiting_confirm": False,
        "original_request": "",
        "slots": {
            "from": "",
            "to": "",
            "date": "",
            "mode": "",
            "trip_type": "",
            "travellers": "",
            "budget": "",
            "preferences": "",
        },
    }


def _is_travel_intent(text: str) -> bool:
    t = (text or "").lower()
    keywords = [
        "travel", "trip", "flight", "train", "bus", "hotel",
        "vacation", "itinerary", "journey", "book ticket", "tickets",
    ]
    has_route = bool(re.search(r"\bfrom\b.+\bto\b", t))
    return has_route or any(k in t for k in keywords)


def _norm_date(raw: str) -> str:
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
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


def _extract_slots(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    s = (text or "").strip()

    m = re.search(r"\bfrom\s+([A-Za-z .-]{2,50})\s+to\s+", s, flags=re.IGNORECASE)
    if m:
        out["from"] = m.group(1).strip(" .,")

    m = re.search(r"\bto\s+([A-Za-z .-]{2,50})(?:\s+on\s+|\s+for\s+|\s+by\s+|$)", s, flags=re.IGNORECASE)
    if m:
        out["to"] = m.group(1).strip(" .,")

    m = re.search(r"\b(?:on|date|depart(?:ure)?)\s+(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b", s, flags=re.IGNORECASE)
    if m:
        d = _norm_date(m.group(1))
        if d:
            out["date"] = d
    if not out.get("date"):
        d = _parse_human_date(s)
        if d:
            out["date"] = d

    if re.search(r"\bflight|air\b", s, flags=re.IGNORECASE):
        out["mode"] = "flight"
    elif re.search(r"\btrain|rail\b", s, flags=re.IGNORECASE):
        out["mode"] = "train"
    elif re.search(r"\bbus\b", s, flags=re.IGNORECASE):
        out["mode"] = "bus"

    m = re.search(r"\b(\d{1,2})\s+(?:traveller|travelers|travellers|passenger|passengers|adult|adults)\b", s, flags=re.IGNORECASE)
    if m:
        out["travellers"] = m.group(1)

    m = re.search(r"\b(?:budget|under|below)\s*(?:rs\.?|inr|\$)?\s*([\d,]+)\b", s, flags=re.IGNORECASE)
    if m:
        out["budget"] = m.group(1).replace(",", "")

    if re.search(r"\bone way|oneway\b", s, flags=re.IGNORECASE):
        out["trip_type"] = "oneway"
    elif re.search(r"\bround trip|roundtrip|return\b", s, flags=re.IGNORECASE):
        out["trip_type"] = "return"

    return out


async def _llm_extract_slots(text: str) -> dict[str, str]:
    try:
        llm = get_llm(
            provider=chat_server.selected_provider,
            model=chat_server.selected_model,
        )
        prompt = (
            "Extract travel slots from user text and return ONLY strict JSON with keys: "
            "from, to, date, mode, trip_type, travellers, budget, preferences. "
            "Rules: date must be YYYY-MM-DD. Unknown -> empty string. "
            "mode in [flight,train,bus,hotel,cab,other]. trip_type in [oneway,return]. "
            f"User text: {text}"
        )
        resp = await llm.ainvoke(prompt)
        data = json.loads((resp.content or "").strip())
        out: dict[str, str] = {}
        for key in ["from", "to", "date", "mode", "trip_type", "travellers", "budget", "preferences"]:
            v = str(data.get(key, "")).strip()
            if v:
                out[key] = v
        if out.get("date"):
            d = _norm_date(out["date"])
            if d:
                out["date"] = d
            else:
                out.pop("date", None)
        return out
    except Exception:
        return {}


def _missing_required(slots: dict[str, str]) -> list[str]:
    missing = []
    if not slots.get("from"):
        missing.append("from")
    if not slots.get("to"):
        missing.append("to")
    if not slots.get("date"):
        missing.append("date")
    return missing


def _merge_slots(dst: dict[str, str], src: dict[str, str]) -> dict[str, str]:
    for k, v in src.items():
        if v:
            dst[k] = v
    return dst


def _build_enriched_query(slots: dict[str, str], original_text: str) -> str:
    filters = []
    for key in ["mode", "trip_type", "travellers", "budget", "preferences"]:
        if slots.get(key):
            filters.append(f"{key}={slots[key]}")
    suffix = f"; filters: {', '.join(filters)}" if filters else ""
    return (
        f"{original_text}\n\n"
        f"Structured travel details (user-confirmed): "
        f"from={slots.get('from','')}, to={slots.get('to','')}, date={slots.get('date','')}{suffix}."
    )


async def handle_task_intake_message(message: str) -> tuple[bool, str]:
    text = (message or "").strip()
    if not text:
        return True, ""

    state = chat_server.task_intake_state or _default_intake_state()
    chat_server.task_intake_state = state

    if text.lower() in {"cancel", "cancel intake", "reset", "reset travel"} and state.get("active"):
        chat_server.task_intake_state = _default_intake_state()
        await chat_server.send_to_browser("Travel intake reset. Share your new request when ready.", "system")
        return True, ""

    travel_intent = _is_travel_intent(text)
    if not state.get("active") and not travel_intent:
        return False, text

    if not state.get("active"):
        state["active"] = True
        state["intent"] = "travel"
        state["awaiting_confirm"] = False
        state["original_request"] = text
        await chat_server.send_to_browser(
            "I will handle this as a travel request and extract route/date/filters before execution.",
            "agent",
        )

    state["slots"] = _merge_slots(state.get("slots", {}), _extract_slots(text))
    missing = _missing_required(state["slots"])
    if missing:
        llm_slots = await _llm_extract_slots(text)
        state["slots"] = _merge_slots(state.get("slots", {}), llm_slots)
        missing = _missing_required(state["slots"])

    slots = state["slots"]
    if missing:
        prompts = {
            "from": "origin city/airport",
            "to": "destination city/airport",
            "date": "travel date (YYYY-MM-DD)",
        }
        ask = ", ".join(prompts[m] for m in missing)
        await chat_server.send_to_browser(
            f"I still need: {ask}. Example: 'from Pune to Delhi on 2026-05-12'.",
            "agent",
        )
        return True, ""

    if not state.get("awaiting_confirm"):
        summary = (
            f"I extracted: from {slots.get('from')} to {slots.get('to')} on {slots.get('date')}"
            f". Mode: {slots.get('mode') or 'not specified'}, trip: {slots.get('trip_type') or 'not specified'}, "
            f"travellers: {slots.get('travellers') or 'not specified'}, budget: {slots.get('budget') or 'not specified'}."
        )
        await chat_server.send_to_browser(summary, "agent")
        await chat_server.send_to_browser(
            "Reply 'yes' to continue, or send corrections (for example: 'change date to 2026-05-15').",
            "agent",
        )
        state["awaiting_confirm"] = True
        return True, ""

    if text.lower() in {"yes", "y", "confirm", "go ahead", "continue"}:
        enriched = _build_enriched_query(slots, state.get("original_request") or message)
        chat_server.task_intake_state = _default_intake_state()
        return False, enriched

    # User likely sent corrections; absorb and ask confirm again.
    state["slots"] = _merge_slots(state.get("slots", {}), _extract_slots(text))
    llm_slots = await _llm_extract_slots(text)
    state["slots"] = _merge_slots(state.get("slots", {}), llm_slots)
    slots = state["slots"]
    await chat_server.send_to_browser(
        (
            f"Updated details: from {slots.get('from') or '?'} to {slots.get('to') or '?'} on {slots.get('date') or '?'}"
            f". Reply 'yes' to continue."
        ),
        "agent",
    )
    return True, ""
