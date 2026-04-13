import asyncio
from tools.browser import browser_manager

async def google_search_safe(query: str):
    """
    Performs a Google Search with built-in handling for CAPTCHAs and errors.
    Pauses execution if an issue is detected, allowing user intervention.
    """
    print(f"\n[Search Tool]: Searching Google for '{query}'")
    try:
        await browser_manager.navigate("https://www.google.com")
        await asyncio.sleep(3)
        
        # Check if we are already on a captcha page or unusual page
        # This is hard to detect perfectly, but if 'textarea[name="q"]' isn't there, something is up.
        try:
             # Wait for the search box
             await browser_manager.page.wait_for_selector('textarea[name="q"], input[name="q"]', timeout=5000)
        except:
             raise Exception("Could not find search box (Possible CAPTCHA or Network issue)")

        await browser_manager.type_text('textarea[name="q"], input[name="q"]', query)
        await browser_manager.page.keyboard.press("Enter")
        await asyncio.sleep(3)
        # Wait for results to load
        # We look for common result containers like 'div.g' or main 'search' div
        try:
             await browser_manager.navigate(browser_manager.page.url) # Ensure we are "navigated" in tracking
             await browser_manager.page.wait_for_selector('#search', timeout=5000)
             print(f"[Search Tool]: Search successful.")
        except:
             raise Exception("Search results did not load (Possible CAPTCHA)")
             
        await asyncio.sleep(2) 

    except Exception as e:
        print(f"\n🛑 [Search Tool] Interrupted: {e}")
        print("👉 Please check the browser window.")
        print("   1. Resolve any CAPTCHA.")
        print("   2. Ensure the search results are visible.")
        input("⌨️  Press ENTER in this terminal when you have fixed it...")
        print("[Search Tool]: Resuming...")
