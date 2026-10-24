"""CI audit & change-aware test selection engine."""
import os

__version__ = "0.1.0"

BUNDLED_PROFILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")
