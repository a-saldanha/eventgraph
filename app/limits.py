"""Ingest and LLM-build limits — all overridable via env vars."""
from __future__ import annotations

import os

# File and request size limits
MAX_FILES: int = int(os.getenv("INGEST_MAX_FILES", "20"))
MAX_FILE_BYTES: int = int(os.getenv("INGEST_MAX_FILE_BYTES", str(10 * 1024 * 1024)))
MAX_REQUEST_BYTES: int = int(os.getenv("INGEST_MAX_REQUEST_BYTES", str(25 * 1024 * 1024)))

# Zip-bomb guards
MAX_ZIP_ENTRIES: int = int(os.getenv("INGEST_MAX_ZIP_ENTRIES", "200"))
MAX_ZIP_UNCOMPRESSED: int = int(os.getenv("INGEST_MAX_ZIP_UNCOMPRESSED", str(50 * 1024 * 1024)))

# Per-IP ingest rate (sliding window)
IP_MAX_INGESTS: int = int(os.getenv("INGEST_IP_MAX", "5"))
IP_WINDOW_SECS: int = int(os.getenv("INGEST_IP_WINDOW_SECS", "600"))

# LLM-build quotas
LLM_MAX_PER_SESSION_HOUR: int = int(os.getenv("LLM_MAX_PER_SESSION_HOUR", "1"))
LLM_DAILY_CAP: int = int(os.getenv("LLM_DAILY_CAP", "20"))
LLM_MAX_CHUNKS: int = int(os.getenv("LLM_MAX_CHUNKS", "40"))

# Job lifecycle
JOB_EVICT_SECS: int = int(os.getenv("JOB_EVICT_SECS", "1800"))
JOB_MAX: int = int(os.getenv("JOB_MAX", "100"))
