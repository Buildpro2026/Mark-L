"""
Gemini API Initialization — Free Tier Configuration
Handles correct setup of Gemini Live API without Anthropic fallback.
All requests use Gemini. If it fails, fail cleanly (don't fall back to another provider).
"""

import json
import os
import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def get_gemini_api_key() -> str | None:
    """
    Retrieve Gemini API key from config/api_keys.json
    Returns None if not configured.
    """
    base_dir = get_base_dir()
    api_keys_path = base_dir / "config" / "api_keys.json"
    
    if not api_keys_path.exists():
        print("[Gemini] ⚠️ config/api_keys.json not found")
        return None
    
    try:
        with open(api_keys_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            key = data.get("gemini_api_key", "").strip()
            if key:
                return key
            else:
                print("[Gemini] ⚠️ gemini_api_key not set in config/api_keys.json")
                return None
    except Exception as e:
        print(f"[Gemini] ⚠️ Error reading API key: {e}")
        return None


def verify_gemini_connectivity(api_key: str) -> bool:
    """
    Verify Gemini API is reachable with the provided key.
    Returns True if successful, False otherwise.
    Does NOT require audio streaming — just a simple list operation.
    """
    try:
        import google.generativeai as genai
    except ImportError:
        print("[Gemini] ⚠️ google-generativeai not installed. Run: pip install google-generativeai")
        return False
    
    try:
        genai.configure(api_key=api_key)
        # Simple operation to verify connectivity
        genai.get_model("models/gemini-1.5-flash")
        print("[Gemini] ✓ API connectivity verified")
        return True
    except Exception as e:
        print(f"[Gemini] ✗ Connectivity check failed: {e}")
        return False


def configure_gemini_for_live_audio() -> bool:
    """
    Configure Gemini Live API for real-time voice interaction.
    Returns True if configuration successful, False otherwise.
    
    Prerequisites:
    - google-generativeai library installed
    - GEMINI_API_KEY environment variable set OR
    - gemini_api_key in config/api_keys.json
    """
    api_key = os.getenv("GEMINI_API_KEY") or get_gemini_api_key()
    
    if not api_key:
        print("[Gemini Live] ✗ No API key found. Set GEMINI_API_KEY env var or add gemini_api_key to config/api_keys.json")
        return False
    
    try:
        import google.generativeai as genai
    except ImportError:
        print("[Gemini Live] ✗ google-generativeai not installed. Run: pip install google-generativeai")
        return False
    
    try:
        genai.configure(api_key=api_key)
        
        # Verify we can access Gemini 2.0 Flash (required for Live API)
        try:
            genai.get_model("models/gemini-2.0-flash")
            print("[Gemini Live] ✓ Gemini 2.0 Flash model available for real-time voice")
        except:
            # Fallback to 1.5 if 2.0 not available
            genai.get_model("models/gemini-1.5-flash")
            print("[Gemini Live] ✓ Gemini 1.5 Flash model available (using for audio)")
        
        print("[Gemini Live] ✓ Audio API configured and ready")
        return True
        
    except Exception as e:
        print(f"[Gemini Live] ✗ Configuration failed: {e}")
        return False


def get_gemini_live_session_params() -> dict:
    """
    Return the correct parameters for starting a Gemini Live audio session.
    Includes proper audio codec, sample rate, and safety settings.
    """
    return {
        "model": "models/gemini-2.0-flash-exp",
        "config": {
            "generation_config": {
                "temperature": 0.7,
                "top_p": 0.95,
                "top_k": 40
            },
            "system_prompt": """You are JARVIS, the personal AI assistant from Iron Man.
Professional, efficient, direct. No fluff. You help with work, research, decisions, and daily tasks.
- Respond quickly and match response length to the task
- When speaking Turkish: say "efendim"
- When speaking English: say "sir"
- Never mix languages
- Act with Iron Man JARVIS personality: professional, efficient, slightly witty""",
            "safety_settings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"}
            ]
        },
        "audio_config": {
            "encoding": "LINEAR16",
            "sample_rate_hertz": 16000
        }
    }


def initialize_gemini():
    """
    Main initialization sequence for Gemini at startup.
    Call this once when the application starts.
    """
    print("\n[Gemini] Starting initialization sequence...")
    
    api_key = os.getenv("GEMINI_API_KEY") or get_gemini_api_key()
    
    if not api_key:
        print("[Gemini] ✗ FATAL: No API key configured")
        print("[Gemini] ℹ️  Configure one of:")
        print("   1. Set GEMINI_API_KEY environment variable")
        print("   2. Add 'gemini_api_key' to config/api_keys.json")
        return False
    
    if not verify_gemini_connectivity(api_key):
        print("[Gemini] ✗ Cannot connect to Gemini API")
        return False
    
    if not configure_gemini_for_live_audio():
        print("[Gemini] ✗ Cannot configure audio streaming")
        return False
    
    print("[Gemini] ✓ Initialization complete — system ready for voice interaction")
    return True


if __name__ == "__main__":
    success = initialize_gemini()
    sys.exit(0 if success else 1)
