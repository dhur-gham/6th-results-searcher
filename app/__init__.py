"""Package init: auto-load a local .env so `python -m app.*` picks up config.

Zero-dependency loader. Real environment variables (e.g. from Docker Compose)
take precedence — the .env only fills in what isn't already set. Runs once on
first import, before db.py reads DATABASE_URL/DATA_DIR.
"""
import os


def _load_dotenv():
    # repo root = parent of this app/ directory
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:   # don't override real env
                    os.environ[key] = val
    except OSError:
        pass


_load_dotenv()
