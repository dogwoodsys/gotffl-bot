import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The shared layer is mounted at /opt/python in Lambda; locally it lives here.
sys.path.insert(0, str(ROOT / "layers" / "shared" / "python"))
sys.path.insert(0, str(ROOT))

# Never let a test reach a real account.
os.environ.setdefault("AWS_DEFAULT_REGION", "ca-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("STATE_TABLE", "gotffl-state-test")
