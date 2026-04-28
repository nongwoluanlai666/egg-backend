import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from wxcloudrun.merchant_notice import (
    MerchantNoticeSourceError,
    get_dispatch_worker_idle_seconds,
    run_dispatch_worker_once,
)


class Command(BaseCommand):
    help = 'Run the merchant notice async dispatch worker.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once',
            action='store_true',
            help='Process at most one queued dispatch job and exit.',
        )

    def handle(self, *args, **options):
        if int(getattr(settings, 'MERCHANT_NOTICE_DISPATCH_WORKER_ENABLED', 1) or 0) <= 0:
            self.stdout.write('merchant notice dispatch worker disabled')
            return

        idle_seconds = get_dispatch_worker_idle_seconds()
        run_once = bool(options.get('once'))

        while True:
            try:
                result = run_dispatch_worker_once()
            except MerchantNoticeSourceError as error:
                result = {
                    'status': 'source_error',
                    'error': str(error),
                }
            except Exception as error:
                result = {
                    'status': 'worker_error',
                    'error': str(error),
                }

            if result.get('status') != 'idle':
                self.stdout.write(json.dumps(result, ensure_ascii=False))

            if run_once:
                return

            if result.get('status') in {'idle', 'source_error', 'worker_error'}:
                time.sleep(idle_seconds)
