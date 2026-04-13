"""
tools.shopping
==============
Platform-specific shopping automation scripts + multi-platform coordinator.

Available modules:
    amazon      — Amazon.in automation
    flipkart    — Flipkart.com automation
    blinkit     — Blinkit.com automation
    coordinator — Runs all three sequentially and opens all carts
    google_items — Extracts item list from a natural-language query

High-level entry point:
    run_shopping_flow(query)  — Google search → all 3 platforms → comparison table
"""

from tools.shopping.amazon import shop_on_amazon
from tools.shopping.flipkart import shop_on_flipkart
from tools.shopping.blinkit import shop_on_blinkit
from tools.shopping.coordinator import run_all_shops, run_shopping_flow

__all__ = [
    "shop_on_amazon",
    "shop_on_flipkart",
    "shop_on_blinkit",
    "run_all_shops",
    "run_shopping_flow",
]
