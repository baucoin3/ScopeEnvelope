"""Env-driven config for the viewer. Shared by app.py and mock_generate.py."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = str(Path(__file__).resolve().parent.parent / "toy_repo")

MONGODB_URI = os.environ.get("MONGODB_URI", "")
DB_NAME = os.environ.get("MONGODB_DB", "scopeenvelope")
RUNS_COLLECTION = os.environ.get("RUNS_COLLECTION", "runs")
FOOTPRINTS_COLLECTION = os.environ.get("FOOTPRINTS_COLLECTION", "footprints")
