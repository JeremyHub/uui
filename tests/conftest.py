import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The suite never spends anyone's Claude usage: no server it starts offers Claude models
# unless a test points it at a stand-in CLI.
os.environ["UUI_CLAUDE_BIN"] = ""
