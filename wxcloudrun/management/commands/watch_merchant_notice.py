import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from wxcloudrun.merchant_notice import (
    MerchantNoticeConfigurationError,
    MerchantNoticeSourceError,
    run_guarded_watch_current_merchant,
)


class Command(BaseCommand):
    help = 'Poll current merchant data until a new round is observed and dispatch merchant notice messages.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--timeout-seconds',
            type=float,
            default=getattr(settings, 'MERCHANT_NOTIFY_POLL_TIMEOUT_SECONDS', 900),
            help='Maximum polling duration in seconds.',
        )
        parser.add_argument(
            '--poll-interval-seconds',
            type=float,
            default=getattr(settings, 'MERCHANT_NOTIFY_POLL_INTERVAL_SECONDS', 30),
            help='Polling interval in seconds.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Ignore the trigger guard window once.',
        )

    def handle(self, *args, **options):
        try:
            result = run_guarded_watch_current_merchant(
                timeout_seconds=options['timeout_seconds'],
                poll_interval_seconds=options['poll_interval_seconds'],
                force=options['force'],
            )
        except MerchantNoticeConfigurationError as error:
            raise CommandError(str(error)) from error
        except MerchantNoticeSourceError as error:
            raise CommandError(str(error)) from error

        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
