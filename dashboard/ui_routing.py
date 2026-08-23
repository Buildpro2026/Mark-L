"""
UI Routing Configuration for JARVIS Dashboard
Controls which interface is served as the default and how routes are configured.

INTERFACE OPTIONS:
- 3d_orb: Sleek sci-fi 3D orb interface (recommended for voice control)
- corporate_dashboard: Traditional web dashboard (legacy, full-featured form-based UI)
"""

import json
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


DEFAULT_UI = "3d_orb"  # Can be overridden via config


def get_ui_preference() -> str:
    """Load UI preference from config. Defaults to 3d_orb."""
    base_dir = get_base_dir()
    business_settings_path = base_dir / "config" / "business_dashboard_settings.json"
    
    try:
        if business_settings_path.exists():
            with open(business_settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("global_settings", {}).get("default_view", DEFAULT_UI)
    except Exception:
        pass
    
    return DEFAULT_UI


def render_home_page(selected_ui: str = None) -> str:
    """
    Generate the home page HTML that routes to the appropriate interface.
    If selected_ui is None, uses the configured default.
    """
    if not selected_ui:
        selected_ui = get_ui_preference()
    
    if selected_ui == "3d_orb":
        # Redirect to 3D orb interface
        return f"""<!DOCTYPE html>
<html>
<head>
    <title>JARVIS — Loading...</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            margin: 0;
            padding: 0;
            background: #0a0e27;
            color: #0ff;
            font-family: 'Courier New', monospace;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
        }}
        .loader {{
            text-align: center;
        }}
        .orb {{
            width: 60px;
            height: 60px;
            border: 2px solid #0ff;
            border-radius: 50%;
            margin: 0 auto 20px;
            animation: pulse 1.5s ease-in-out infinite;
        }}
        @keyframes pulse {{
            0%, 100% {{ box-shadow: 0 0 20px #0ff; }}
            50% {{ box-shadow: 0 0 40px #0ff; }}
        }}
        p {{
            font-size: 14px;
            letter-spacing: 2px;
        }}
    </style>
</head>
<body>
    <div class="loader">
        <div class="orb"></div>
        <p>JARVIS INITIALIZING...</p>
        <p style="font-size: 12px; color: #088;">Connecting to orb interface</p>
    </div>
    <script>
        // Redirect to 3D orb interface after brief delay
        setTimeout(() => {{
            window.location.href = '/3d';
        }}, 500);
    </script>
</body>
</html>
"""
    
    else:
        # Fallback to corporate dashboard (legacy)
        return f"""<!DOCTYPE html>
<html>
<head>
    <title>JARVIS Command Center</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            margin: 0;
            padding: 0;
            background: #1a1a2e;
            color: #fff;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
        }}
        .container {{
            text-align: center;
        }}
        h1 {{
            font-size: 24px;
            margin: 0 0 10px 0;
        }}
        p {{
            font-size: 14px;
            color: #aaa;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>JARVIS</h1>
        <p>Corporate Dashboard</p>
        <p style="margin-top: 20px; font-size: 12px;">Loading command center...</p>
    </div>
    <script>
        // Dashboard will be served by the main server route
    </script>
</body>
</html>
"""


def get_ui_mode_info() -> dict:
    """Return information about current UI mode and available options."""
    current = get_ui_preference()
    return {
        "current_ui": current,
        "available_options": ["3d_orb", "corporate_dashboard"],
        "recommended": "3d_orb",
        "reason": "3D orb interface optimized for voice control and real-time interaction"
    }


# Routes mapping for FastAPI
UI_ROUTES = {
    "home": "/",
    "orb": "/3d",
    "orb_assets": "/3d/assets",
    "orb_api": "/3d/api",
    "dashboard": "/dashboard",
    "api": "/api"
}
