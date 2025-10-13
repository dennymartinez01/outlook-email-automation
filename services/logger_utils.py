"""
services/logger_utils.py
----------------------------------------------------
Centralized logging utilities for the Outlook Email Invoice Tracker.
- Supports console logging (default)
- Optional HTTP logging to Seq (if SEQ_URL set)
- Includes `retry` and `with_retry` decorators for resilient API calls
----------------------------------------------------
"""

import os
import time
import logging
from functools import wraps
from dotenv import load_dotenv
from pathlib import Path

# ---------------------------------------------------------------------
# Load environment variables safely
# ---------------------------------------------------------------------
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# ---------------------------------------------------------------------
# Base logger setup
# ---------------------------------------------------------------------
logger = logging.getLogger("invoice-monitor")
logger.setLevel(logging.INFO)

if not logger.handlers:
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(console_handler)

    # Optional HTTP logging to Seq (via basic requests)
    SEQ_URL = os.getenv("SEQ_URL")
    SEQ_API_KEY = os.getenv("SEQ_API_KEY")

    if SEQ_URL and SEQ_URL.strip():
        try:
            import requests

            class SeqHttpHandler(logging.Handler):
                def emit(self, record):
                    try:
                        payload = {
                            "events": [{
                                "level": record.levelname,
                                "messageTemplate": record.getMessage(),
                                "properties": {
                                    "logger": record.name,
                                    "time": record.created
                                }
                            }]
                        }
                        headers = {
                            "Content-Type": "application/json"
                        }
                        if SEQ_API_KEY:
                            headers["X-Seq-ApiKey"] = SEQ_API_KEY
                        requests.post(f"{SEQ_URL}/api/events/raw", json=payload, headers=headers, timeout=2)
                    except Exception:
                        # fail silently to avoid breaking app
                        pass

            logger.addHandler(SeqHttpHandler())
            logger.info(f"✅ Seq HTTP logging initialized at {SEQ_URL}")
        except Exception as e:
            logger.warning(f"⚠️ Could not initialize Seq HTTP logging: {e}")
    else:
        logger.info("ℹ️ Seq logging not configured; using console output only.")

# ---------------------------------------------------------------------
# Retry decorator (resilient API calls)
# ---------------------------------------------------------------------
def retry(max_attempts=3, base_delay=1.0, backoff=2.0):
    """
    Retry decorator (legacy name for backward compatibility).
    """
    return with_retry(max_attempts=max_attempts, base_delay=base_delay, backoff=backoff)


def with_retry(max_attempts=3, base_delay=1.0, backoff=2.0):
    """
    Retry decorator that retries a function if it raises an exception.
    Logs each retry attempt and applies exponential backoff.
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            delay = base_delay
            while attempt < max_attempts:
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.error(f"❌ Giving up after {attempt} attempts: {fn.__name__}")
                        raise
                    logger.warning(
                        f"⚠️ Retry {attempt}/{max_attempts} for {fn.__name__}: {e} — sleeping {delay:.2f}s"
                    )
                    time.sleep(delay)
                    delay *= backoff
        return wrapper

    return decorator

# ---------------------------------------------------------------------
# Utility: log custom event
# ---------------------------------------------------------------------
def log_event(level: str, message: str):
    """
    Log a custom event to Seq (or console if not configured).
    """
    level = level.lower()
    if level == "error":
        logger.error(message)
    elif level == "warning":
        logger.warning(message)
    elif level == "debug":
        logger.debug(message)
    else:
        logger.info(message)
