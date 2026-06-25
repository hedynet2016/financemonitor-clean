#!/usr/bin/env python3
"""
WSGI entry point for gunicorn.
Usage: gunicorn --workers 1 --timeout 300 -b 0.0.0.0:$PORT wsgi:app
"""
import os
import sys
import threading
import atexit
import subprocess
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from webui import app, start_scheduler, SCHEDULER_PROCS

logger = logging.getLogger("wsgi")

def _start_scheduler_thread():
    """Start scheduler in a daemon thread (runs once)."""
    try:
        procs = start_scheduler()
        if procs:
            logger.info(f"Scheduler started, PIDs: {[p.pid for p in procs]}")
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")

# Start scheduler in background thread (only once)
_t = threading.Thread(target=_start_scheduler_thread, daemon=True)
_t.start()

# Make app available as wsgi:app
__all__ = ["app"]
