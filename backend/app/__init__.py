"""FastAPI application package."""

from app.env import load_env

# Runs before any settings module reads its environment variables.
load_env()
