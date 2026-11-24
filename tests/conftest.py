import os
import sys
import tempfile
from pathlib import Path

# bot.py crée le Store à l'import : on le pointe vers un fichier temporaire, jamais vers le vrai data.json
os.environ["DATA_FILE"] = str(Path(tempfile.mkdtemp()) / "test-data.json")
os.environ.setdefault("DISCORD_TOKEN", "test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
