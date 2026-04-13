from tools.browser import browser_manager
from tools.search.google import google_search_safe
from tools.shopping.amazon import shop_on_amazon
from tools.shopping.coordinator import run_shopping_flow

TOOL_CONFIG = {
    "navigate": {
        "func": browser_manager.navigate,
        "desc": "Navigates to a specific URL (args: url)."
    },
    "google_search": {
        "func": google_search_safe,
        "desc": "Searches Google safely (args: query). Use this to find information."
    },
    "shop_all_platforms": {
        "func": run_shopping_flow,
        "desc": (
            "SHOPPING TOOL — Use for ANY shopping/buying/purchasing request. "
            "Searches Google for items, then shops on Amazon + Flipkart + Blinkit "
            "automatically and shows a comparison table. "
            "(args: query='natural language shopping request')"
        ),
    },
    "amazon_shop": {
        "func": shop_on_amazon,
        "desc": "Automates shopping on Amazon only for a list of items (args: items=['item1', 'item2']). Prefer shop_all_platforms instead."
    },
    "click_element": {
        "func": browser_manager.click,
        "desc": "Clicks an element. Selector can be text (e.g. 'Search') or CSS selector (args: selector)."
    },
    "type_text": {
        "func": browser_manager.type_text,
        "desc": "Types text into an element. Selector can be CSS selector (args: selector, text)."
    },
    "read_page": {
        "func": browser_manager.get_content,
        "desc": "Reads the current page content as markdown."
    },
    "list_interactive_elements": {
        "func": browser_manager.get_interactive_elements,
        "desc": "Lists clickable elements (buttons, links) to help identify selectors."
    },
    "scroll": {
        "func": browser_manager.scroll,
        "desc": "Scrolls the page down (args: amount=500)."
    }
}

