import multiprocessing

bind = "unix:/var/www/compare-wages/compare-wages.sock"
workers = 1
worker_class = "uvicorn.workers.UvicornWorker"
timeout = 120
accesslog = "/var/www/compare-wages/logs/access.log"
errorlog = "/var/www/compare-wages/logs/error.log"
loglevel = "info"
