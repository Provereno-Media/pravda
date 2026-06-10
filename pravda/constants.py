import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Paths
PROJECT_ROOT = Path(__file__).parent.parent

# Browser connection
BROWSER_WS_URL = os.environ["BROWSER_WS_URL"]
BROWSER_CHANNEL = "chrome"
BROWSER_HEADLESS = False
