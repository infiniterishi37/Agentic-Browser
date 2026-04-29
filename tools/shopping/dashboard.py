import os
import json

DASHBOARD_FILE = os.path.join("artifacts", "shopping_dashboard.html")

def update_dashboard(results: dict, all_items: list[str], is_running: bool = True):
    """
    Generates a responsive HTML dashboard showing the shopping results.
    If is_running is True, it includes an auto-refresh meta tag.
    """
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)

    platforms = list(results.keys()) if results else ["amazon", "flipkart", "blinkit"]
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agentic Shopping Dashboard</title>
    {"<meta http-equiv='refresh' content='2'>" if is_running else ""}
    <style>
        :root {{
            --bg-dark: #0f172a;
            --bg-card: #1e293b;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #6366f1;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
        }}
        body {{
            font-family: 'Inter', system-ui, -apple-system, sans-serif;
            background-color: var(--bg-dark);
            color: var(--text-main);
            margin: 0;
            padding: 20px;
        }}
        .header {{
            text-align: center;
            margin-bottom: 30px;
            padding: 20px;
            background: linear-gradient(135deg, rgba(99,102,241,0.1), rgba(139,92,246,0.1));
            border-radius: 16px;
            border: 1px solid rgba(139,92,246,0.2);
        }}
        .header h1 {{
            margin: 0;
            font-size: 2.5rem;
            background: -webkit-linear-gradient(45deg, #818cf8, #c084fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        .status-badge {{
            display: inline-block;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 0.875rem;
            font-weight: 600;
            margin-top: 10px;
            background-color: { "rgba(245, 158, 11, 0.2)" if is_running else "rgba(16, 185, 129, 0.2)" };
            color: { "var(--warning)" if is_running else "var(--success)" };
            border: 1px solid { "rgba(245, 158, 11, 0.5)" if is_running else "rgba(16, 185, 129, 0.5)" };
        }}
        .platforms-container {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 24px;
        }}
        .platform-col {{
            background: var(--bg-card);
            border-radius: 16px;
            padding: 20px;
            box-shadow: 0 10px 15px -3px rgba(0,0,0,0.5);
            border: 1px solid rgba(255,255,255,0.05);
        }}
        .platform-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            padding-bottom: 15px;
            margin-bottom: 15px;
        }}
        .platform-header h2 {{
            margin: 0;
            text-transform: capitalize;
            font-size: 1.5rem;
        }}
        .cart-link {{
            color: var(--accent);
            text-decoration: none;
            font-size: 0.875rem;
            padding: 6px 12px;
            background: rgba(99,102,241,0.1);
            border-radius: 8px;
            transition: background 0.2s;
        }}
        .cart-link:hover {{
            background: rgba(99,102,241,0.2);
        }}
        .product-card {{
            display: flex;
            background: rgba(0,0,0,0.2);
            border-radius: 12px;
            padding: 12px;
            margin-bottom: 12px;
            gap: 15px;
            transition: transform 0.2s;
            text-decoration: none;
            color: inherit;
        }}
        .product-card:hover {{
            transform: translateY(-2px);
            background: rgba(0,0,0,0.3);
        }}
        .product-img {{
            width: 80px;
            height: 80px;
            border-radius: 8px;
            object-fit: cover;
            background: #2dd4bf20;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .product-img img {{
            width: 100%;
            height: 100%;
            border-radius: 8px;
            object-fit: cover;
        }}
        .product-info {{
            flex: 1;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }}
        .product-title {{
            font-size: 0.95rem;
            font-weight: 500;
            line-height: 1.4;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            margin-bottom: 8px;
        }}
        .product-price {{
            font-size: 1.1rem;
            font-weight: 700;
            color: #818cf8;
        }}
        .query-badge {{
            font-size: 0.7rem;
            background: rgba(255,255,255,0.1);
            padding: 2px 6px;
            border-radius: 4px;
            align-self: flex-start;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            margin-bottom: 4px;
        }}
        .unavailable-item {{
            background: rgba(239, 68, 68, 0.1);
            border: 1px dashed rgba(239, 68, 68, 0.3);
            border-radius: 12px;
            padding: 12px;
            margin-bottom: 12px;
            color: var(--danger);
            font-size: 0.9rem;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .empty-state {{
            text-align: center;
            padding: 30px;
            color: var(--text-muted);
            font-style: italic;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🛍️ Agentic Shopping Dashboard</h1>
        <div class="status-badge">
            { "⏳ Agent is currently shopping..." if is_running else "✅ Shopping Run Complete" }
        </div>
        <p style="color: var(--text-muted); margin-top: 15px;">Requested Items: {", ".join(all_items)}</p>
    </div>

    <div class="platforms-container">
"""

    for platform, data in results.items():
        added = data.get("added", [])
        unavailable = data.get("unavailable", [])
        cart_url = data.get("cart_url", "#")
        
        html += f"""
        <div class="platform-col">
            <div class="platform-header">
                <h2>{platform}</h2>
                <a href="{cart_url}" target="_blank" class="cart-link">View Cart 🛒</a>
            </div>
            
            <div class="products-list">
"""
        
        if not added and not unavailable:
            html += f'<div class="empty-state">Waiting for items...</div>'
            
        for item in added:
            # Handle old string format if it ever happens
            if isinstance(item, str):
                item = {"query": item, "title": item, "price": "N/A", "image_url": "", "url": "#"}
            
            img_tag = f'<img src="{item.get("image_url")}" alt="Product">' if item.get("image_url") else '<span>📦</span>'
            html += f"""
                <a href="{item.get("url", "#")}" target="_blank" class="product-card">
                    <div class="product-img">{img_tag}</div>
                    <div class="product-info">
                        <span class="query-badge">Search: {item.get("query", "Unknown")}</span>
                        <div class="product-title">{item.get("title", "Unknown Product")}</div>
                        <div class="product-price">{item.get("price", "N/A")}</div>
                    </div>
                </a>
            """
            
        for unavail in unavailable:
            html += f"""
                <div class="unavailable-item">
                    <span>❌</span> Not Available / Out of stock: <strong>{unavail}</strong>
                </div>
            """
            
        html += """
            </div>
        </div>
"""

    html += """
    </div>
</body>
</html>
"""

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    
    return os.path.abspath(DASHBOARD_FILE)

if __name__ == "__main__":
    # Test
    res = {
        "amazon": {
            "added": [{"query": "milk", "title": "Amul Taaza", "price": "₹55", "image_url": "", "url": "#"}],
            "unavailable": ["bread"],
            "cart_url": "https://amazon.in/cart"
        }
    }
    path = update_dashboard(res, ["milk", "bread"], False)
    print(f"Generated test dashboard at {path}")
