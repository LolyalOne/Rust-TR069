"""
Gunicorn Configuration for TR-369 USP FastAPI Manager
Enforces strict physical memory containment (< 1GB) and process isolation.
"""
import multiprocessing
import os

# Server socket
bind = os.getenv("BIND", "0.0.0.0:8000")
backlog = 2048

# Concurrency & Worker model
# Requirement: 2 workers, worker_class="uvicorn.workers.UvicornWorker"
workers = int(os.getenv("WORKERS", "2"))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
timeout = int(os.getenv("TIMEOUT", "60"))
keepalive = 5

# Memory containment & worker recycling (prevents memory fragmentation)
max_requests = 1000
max_requests_jitter = 50

# Logging
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s µs'

# Process naming
proc_name = "fastapi-manager"
