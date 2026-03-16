import multiprocessing

# Bind only to localhost (the shim calls this; not exposed publicly)
bind = "127.0.0.1:9090"

# Conservative defaults; bump if needed
workers = max(2, multiprocessing.cpu_count() // 2)
threads = 4
worker_class = "gthread"

# Timeouts/logging
timeout = 30
graceful_timeout = 20
keepalive = 30
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Start fast & share app state
preload_app = True
