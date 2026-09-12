"""Config package initialization."""

# Expose Celery app so that @shared_task uses this default app
from .celery import app as celery_app

__all__ = ('celery_app',)
