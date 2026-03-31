# gunicorn.conf.py — Production WSGI configuration
# Used by:  gunicorn -c gunicorn.conf.py app:app

import multiprocessing

# Bind address (override with PORT env var on Render)
import os
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# 2 workers is the sweet spot for free-tier (1 vCPU, 512 MB RAM)
workers = 2
worker_class = "sync"
threads = 2

# Timeouts
timeout = 30          # upstream feed fetch can be slow
graceful_timeout = 20
keepalive = 5

# Logging
accesslog = "-"
errorlog  = "-"
loglevel  = "info"

# Reload on code change (disable in production)
reload = False
