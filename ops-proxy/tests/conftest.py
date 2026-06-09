import sys
from pathlib import Path


OPS_PROXY_DIR = Path(__file__).resolve().parents[1]

if str(OPS_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_PROXY_DIR))
