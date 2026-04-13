"""
demo.py — Test the multi-platform shopping automation WITHOUT any LLM calls.

Usage:
    python demo.py                              # All three platforms (default list)
    python demo.py search "milk and bread"      # Google-first flow (full shopping flow)
    python demo.py amazon                       # Amazon only
    python demo.py flipkart                     # Flipkart only
    python demo.py blinkit                      # Blinkit only
    python demo.py coordinator                  # All three + open all carts

Edit SHOPPING_LIST below to change what gets searched in non-search modes.
"""

import asyncio
import sys
from dotenv import load_dotenv

load_dotenv()

# ── Edit your list here ──────────────────────────────────────────────────────
SHOPPING_LIST = [
    "milk",
    "bread",
    "eggs",
]
# ────────────────────────────────────────────────────────────────────────────


async def run(target: str = "coordinator"):
    from tools.browser import browser_manager

    print("\n🚀  Starting browser...")
    await browser_manager.start()

    try:
        if target == "amazon":
            from tools.shopping.amazon import shop_on_amazon
            result = await shop_on_amazon(SHOPPING_LIST)
            _print_result(result)

        elif target == "flipkart":
            from tools.shopping.flipkart import shop_on_flipkart
            result = await shop_on_flipkart(SHOPPING_LIST)
            _print_result(result)

        elif target == "blinkit":
            from tools.shopping.blinkit import shop_on_blinkit
            result = await shop_on_blinkit(SHOPPING_LIST)
            _print_result(result)

        elif target == "search":
            # Full Google-first flow: query → items → all 3 platforms → comparison
            from tools.shopping.coordinator import run_shopping_flow
            query = " ".join(sys.argv[2:]) or " ".join(SHOPPING_LIST)
            await run_shopping_flow(query)

        else:  # coordinator (default)
            from tools.shopping.coordinator import run_all_shops
            results = await run_all_shops(SHOPPING_LIST)
            for platform, data in results.items():
                _print_result(data)

        print("\n✅  Demo finished. The browser stays open so you can inspect the carts.")
        print("    Press Ctrl+C or close the terminal when done.\n")

        # Keep alive — poll so Ctrl+C is caught cleanly.
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\n⏹  Interrupted by user.")
    finally:
        print("🛑  Shutting down browser...")
        try:
            await browser_manager.stop()
        except Exception:
            pass  # Browser may already be closed — that's fine.


def _print_result(data: dict):
    platform = data.get("platform", "unknown").capitalize()
    added = data.get("added", [])
    unavailable = data.get("unavailable", [])
    print(f"\n{'─'*40}")
    print(f"  Platform   : {platform}")
    print(f"  Added      : {added if added else '(none)'}")
    if unavailable:
        print(f"  ❌ Not available: {unavailable}")
    print(f"{'─'*40}")


if __name__ == "__main__":
    # Accept an optional CLI argument to choose the platform
    target = sys.argv[1].lower() if len(sys.argv) > 1 else "coordinator"
    valid = {"amazon", "flipkart", "blinkit", "coordinator", "search"}
    if target not in valid:
        print(f"❌  Unknown target '{target}'. Choose from: {', '.join(sorted(valid))}")
        sys.exit(1)

    asyncio.run(run(target))
