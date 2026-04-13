import asyncio
from tools.browser import browser_manager
from tools.shopping.amazon import shop_on_amazon
from tools.search.google import google_search_safe

async def run_aloo_paratha_demo():
    await asyncio.sleep(4)
    print("Execution Plan for Browser Agent for 'Aloo Paratha'...")
    
    # 1. creating Plan
    print("\n📋 Creating Plan...")
    print("   1. Search Google for Find Ingredient")
    print("   2. Open Amazon and search for ingredients")
    print("   3. Add Items to cart")
    print("   4. Open Cart")
    
    await asyncio.sleep(5)

    # 2. Open Google Search
    await google_search_safe("Ingredients for Cooking Aloo Paratha at cheaper cost")

    # Scroll slightly to simulate reading
    print("   ... Reading results")
    await browser_manager.page.evaluate("window.scrollBy(0, 500)")
    await asyncio.sleep(5)
    await browser_manager.page.evaluate("window.scrollBy(0, 500)")
    await asyncio.sleep(5)
    
    print("   🔍 Analysing the results...")
    
    # 3. Use the Shopping Tool
    items = ["Atta flour", "Potatoes (Aloo)", "Cooking Oil", "Coriander Leaves"]
    await shop_on_amazon(items)
