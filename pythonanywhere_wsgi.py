import os
import sys

PROJECT_HOME = os.path.dirname(os.path.abspath(__file__))

if PROJECT_HOME not in sys.path:
    sys.path.insert(0, PROJECT_HOME)

os.environ.setdefault("DB_PATH", os.path.join(PROJECT_HOME, "saved.db"))

from app import app as application
