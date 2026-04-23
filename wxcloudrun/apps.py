import logging
import os
import sys

from django.apps import AppConfig


class AppNameConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'wxcloudrun'

    def ready(self):
        command = sys.argv[1] if len(sys.argv) > 1 else ''
        if command not in {'runserver', 'gunicorn', 'uwsgi'}:
            return

        if command == 'runserver' and os.environ.get('RUN_MAIN') not in {'true', 'True', '1'}:
            return

        try:
            from wxcloudrun.local_model_predict import preload_local_model_if_configured

            preload_local_model_if_configured()
        except Exception:
            logging.getLogger('log').exception('local model preload failed during app startup')
