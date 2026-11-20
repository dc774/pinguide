# Answers stream for up to a minute or two, holding a thread each, so one worker
# serves several visitors at once with threads (Chroma and the app's caches are
# loaded once per worker; more workers would multiply memory).
workers = 1
worker_class = "gthread"
threads = 8
timeout = 120
