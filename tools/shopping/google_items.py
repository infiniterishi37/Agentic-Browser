"""
tools/shopping/google_items.py
================================
Google-first shopping list resolver.

Flow:
1) Search Google for the natural-language request
2) Scrape result page text
3) Use LLM reasoning to produce a concrete item list
4) Fall back to non-hardcoded heuristics if LLM fails
"""

import asyncio
import os
import re
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from agent.llm_provider import get_llm
from tools.browser import browser_manager


_STOP_WORDS = {
    "buy",
    "purchase",
    "order",
    "shop",
    "get",
    "find",
    "search",
    "for",
    "me",
    "the",
    "a",
    "an",
    "and",
    "or",
    "with",
    "of",
    "in",
    "on",
    "at",
    "to",
    "from",
    "near",
    "best",
    "cheap",
    "good",
    "top",
    "online",
    "india",
    "price",
    "prices",
}

_GENERIC_ITEM_WORDS = {
    "item",
    "items",
    "thing",
    "things",
    "stuff",
    "products",
    "product",
    "supplies",
    "material",
    "materials",
}


def _normalize_item(item: str) -> str:
    return re.sub(r"\s+", " ", item).strip(" ,.-")


def _unique_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for raw in items:
        item = _normalize_item(raw)
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _is_generic_item_phrase(item: str) -> bool:
    words = [w.lower() for w in re.findall(r"[a-zA-Z0-9']+", item)]
    if not words:
        return True
    non_generic = [w for w in words if w not in _GENERIC_ITEM_WORDS]
    return len(non_generic) <= 2 and any(w in _GENERIC_ITEM_WORDS for w in words)


def _extract_items_from_query(query: str) -> list[str]:
    """
    Parse explicitly-listed items from the query (comma/and/& separated).
    This is generic parsing only, with no hardcoded domain lists.
    """
    text = re.sub(r"\band\b|&", ",", query, flags=re.IGNORECASE)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    items: list[str] = []
    for part in parts:
        words = [w for w in re.findall(r"[a-zA-Z0-9']+", part) if w.lower() not in _STOP_WORDS]
        if not words:
            continue
        candidate = " ".join(words)
        if not _is_generic_item_phrase(candidate):
            items.append(candidate)
    return _unique_preserve_order(items)


def _google_text_candidates(page_text: str) -> list[str]:
    """
    Light-weight extraction of likely product phrases from Google result text.
    Keeps short noun-like phrases without any domain-specific hardcoded lists.
    """
    candidates: list[str] = []
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    for ln in lines:
        # Prefer list-like rows often present in snippets.
        if "," in ln:
            parts = [p.strip() for p in ln.split(",")]
        else:
            parts = [ln]
        for p in parts:
            words = [w for w in re.findall(r"[a-zA-Z0-9']+", p) if w.lower() not in _STOP_WORDS]
            if 1 <= len(words) <= 4:
                candidate = " ".join(words)
                if not _is_generic_item_phrase(candidate):
                    candidates.append(candidate)
    return _unique_preserve_order(candidates)[:25]


def _compact_google_context(page_text: str) -> str:
    """
    Shrink raw Google page text into a compact, high-signal context.
    Keeps only short lines and enforces a strict character budget.
    """
    max_chars = int(os.getenv("SHOPPING_LLM_CONTEXT_CHARS", "1600"))
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    curated: list[str] = []

    for ln in lines:
        # Keep snippet-like lines; skip very short noise and very long paragraphs.
        if len(ln) < 24 or len(ln) > 140:
            continue
        curated.append(ln)
        if len(curated) >= 20:
            break

    compact = "\n".join(curated)
    if len(compact) > max_chars:
        compact = compact[:max_chars]
    return compact


async def _llm_reason_items(
    query: str,
    page_text: str,
    seed_items: list[str],
    llm_provider: str = "",
    llm_model: str = "",
) -> list[str]:
    llm = get_llm(
        provider=llm_provider or None,
        model=llm_model or None,
    )
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a shopping-list extraction engine.\n"
                "Return ONLY valid JSON with key 'items' (array of strings).\n"
                "Rules:\n"
                "1) Output concrete purchasable product names, not generic phrases.\n"
                "2) If user query is broad, infer a practical list from Google text context.\n"
                "3) Keep list concise (5-12 items for broad requests, or exact explicit items if already listed).\n"
                "4) Do not include brands unless required.\n",
            ),
            (
                "human",
                "User query:\n{query}\n\n"
                "Google page text (truncated):\n{page_text}\n\n"
                "Seed candidates:\n{seed_items}\n\n"
                "Return JSON: {{\"items\": [\"...\"]}}",
            ),
        ]
    )
    chain = prompt | llm | JsonOutputParser()
    timeout_s = float(os.getenv("SHOPPING_LLM_TIMEOUT_SECONDS", "18"))
    compact_text = _compact_google_context(page_text)
    # Keep the seed list small; it is only meant to guide reasoning.
    max_seed_items = int(os.getenv("SHOPPING_LLM_MAX_SEED_ITEMS", "12"))
    compact_seed_items = seed_items[:max_seed_items]
    payload = await asyncio.wait_for(
        chain.ainvoke(
            {
                "query": query,
                "page_text": compact_text,
                "seed_items": compact_seed_items,
            }
        ),
        timeout=timeout_s,
    )
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return []
    cleaned = [str(x) for x in items if isinstance(x, (str, int, float))]
    return _unique_preserve_order(cleaned)


async def search_items_on_google(
    query: str, llm_provider: str = "", llm_model: str = ""
) -> list[str]:
    """
    Google-first resolver for shopping items.
    """
    print(f"\n[Google Items] Query: '{query}'")

    page_text = ""
    seed_items = _extract_items_from_query(query)
    google_candidates: list[str] = []

    try:
        search_url = "https://www.google.com/search?q=" + query.replace(" ", "+")
        print(f"[Google Items] 🔍 Searching Google: {search_url}")
        await browser_manager.navigate(search_url)
        await browser_manager.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)
        page_text = await browser_manager.page.inner_text("body")
        google_candidates = _google_text_candidates(page_text)
        print(f"[Google Items] ✅ Scraped page text ({len(page_text)} chars)")
    except Exception as e:
        print(f"[Google Items] ⚠️ Google scraping failed: {e}")

    merged_seed = _unique_preserve_order(seed_items + google_candidates)

    try:
        llm_items = await _llm_reason_items(
            query,
            page_text,
            merged_seed,
            llm_provider=llm_provider,
            llm_model=llm_model,
        )
        if llm_items:
            print(f"[Google Items] 🧠 LLM-resolved items: {llm_items}")
            return llm_items
    except Exception as e:
        print(f"[Google Items] ⚠️ LLM reasoning failed: {e}")

    # Non-hardcoded fallback path.
    if merged_seed:
        print(f"[Google Items] ↺ Using heuristic items: {merged_seed}")
        return merged_seed

    fallback = _extract_items_from_query(query) or [_normalize_item(query)]
    print(f"[Google Items] ↺ Final fallback items: {fallback}")
    return _unique_preserve_order(fallback)
