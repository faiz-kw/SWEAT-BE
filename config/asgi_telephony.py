"""
Dedicated ASGI service for Server-Sent Events (SSE) — /telephony/events/.
Runs under Uvicorn independently of the main Gunicorn/WSGI workers.
Ensures long-lived SSE connections do not tie up Gunicorn worker threads.
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Initialize Django
django.setup()

# Step 5: Configure conn_max_age=0 for this service so DB connections are never pooled
from django.conf import settings

if hasattr(settings, 'DATABASES'):
    for db_cfg in settings.DATABASES.values():
        if isinstance(db_cfg, dict):
            db_cfg['CONN_MAX_AGE'] = 0

from django.core.asgi import get_asgi_application

application = get_asgi_application()

