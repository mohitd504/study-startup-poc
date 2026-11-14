import os
from pathlib import Path

# Must run before common.db is imported, so tests never touch data/study.db.
os.environ["STUDY_DB_PATH"] = str(Path(__file__).resolve().parent.parent / "data" / "test.db")
