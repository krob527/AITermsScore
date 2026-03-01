#!/bin/bash
# Azure App Service startup script for getAITermsScore Flask app.
# Oryx builds a virtualenv at antenv during deployment (SCM_DO_BUILD_DURING_DEPLOYMENT=true).
# We activate it here so gunicorn and all packages are immediately available
# without running pip at cold-start time.

# Activate the Oryx-created virtual environment
if [ -d "/home/site/wwwroot/antenv" ]; then
    source /home/site/wwwroot/antenv/bin/activate
fi

exec gunicorn \
  --bind=0.0.0.0:8000 \
  --timeout=600 \
  --workers=1 \
  --threads=4 \
  --log-level=info \
  app:app
