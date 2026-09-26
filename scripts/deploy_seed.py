#!/usr/bin/env python
'''
Standalone Deployment Seed Runner.
Sets up the environment and delegates to the 'seed_deployment' Django management command.

Usage:
    python backend/scripts/deploy_seed.py
    python backend/scripts/deploy_seed.py --tenant-slug=sweat --admin-email=admin@sweat.com
'''

import os
import sys

# Add backend directory to sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.core.management import call_command

if __name__ == '__main__':
    # Forward all command line arguments to seed_deployment
    args = sys.argv[1:]
    try:
        call_command('seed_deployment', *args)
    except Exception as exc:
        print(f"[FATAL] Deployment seeding failed: {exc}", file=sys.stderr)
        sys.exit(1)
