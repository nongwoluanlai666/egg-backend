import csv
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from wxcloudrun.models import EggFeedback


def parse_csv_set(raw_value):
    return {item.strip() for item in str(raw_value or '').split(',') if item.strip()}


def build_record_payload(record):
    try:
        prediction_snapshot = json.loads(record.prediction_snapshot or '[]')
    except json.JSONDecodeError:
        prediction_snapshot = []

    return {
        'id': record.id,
        'created_at': record.created_at.isoformat() if record.created_at else '',
        'updated_at': record.updated_at.isoformat() if record.updated_at else '',
        'request_id': record.request_id,
        'prediction_session_id': record.prediction_session_id,
        'size': float(record.size),
        'weight': float(record.weight),
        'rideable_only': bool(record.rideable_only),
        'confirmed_species': record.confirmed_species,
        'source': record.source,
        'prediction_version': record.prediction_version,
        'predicted_species': record.predicted_species,
        'predicted_rank': record.predicted_rank,
        'predicted_probability': (
            float(record.predicted_probability)
            if record.predicted_probability is not None
            else None
        ),
        'quality_status': record.quality_status,
        'quality_score': record.quality_score,
        'review_note': record.review_note,
        'upstream_verification_status': record.upstream_verification_status,
        'upstream_top1_species': record.upstream_top1_species,
        'upstream_top1_probability': (
            float(record.upstream_top1_probability)
            if record.upstream_top1_probability is not None
            else None
        ),
        'upstream_confirmed_rank': record.upstream_confirmed_rank,
        'upstream_confirmed_probability': (
            float(record.upstream_confirmed_probability)
            if record.upstream_confirmed_probability is not None
            else None
        ),
        'upstream_checked_at': record.upstream_checked_at.isoformat() if record.upstream_checked_at else '',
        'is_custom_species': bool(record.is_custom_species),
        'species_in_snapshot': bool(record.species_in_snapshot),
        'candidate_count': record.candidate_count,
        'appid': record.appid,
        'openid_hash': record.openid_hash,
        'ip_hash': record.ip_hash,
        'user_agent': record.user_agent,
        'prediction_snapshot': prediction_snapshot,
    }


class Command(BaseCommand):
    help = 'Export EggFeedback records for offline cleaning, source-site verification, and training.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True, help='output file path, supports .jsonl or .csv')
        parser.add_argument('--min-quality-score', type=int, default=0)
        parser.add_argument(
            '--quality-statuses',
            default='',
            help='optional comma separated quality statuses, empty means all',
        )
        parser.add_argument(
            '--verification-statuses',
            default='',
            help='optional comma separated upstream verification statuses, empty means all',
        )
        parser.add_argument('--include-custom', action='store_true', help='include custom species records')
        parser.add_argument('--keep-duplicates', action='store_true', help='keep duplicate size/weight/species rows')

    def handle(self, *args, **options):
        output_path = Path(options['output']).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        quality_statuses = parse_csv_set(options['quality_statuses'])
        verification_statuses = parse_csv_set(options['verification_statuses'])
        min_quality_score = max(int(options['min_quality_score']), 0)

        queryset = EggFeedback.objects.all().order_by('-quality_score', '-created_at', '-id')
        if quality_statuses:
            queryset = queryset.filter(quality_status__in=quality_statuses)
        if verification_statuses:
            queryset = queryset.filter(upstream_verification_status__in=verification_statuses)
        if min_quality_score > 0:
            queryset = queryset.filter(quality_score__gte=min_quality_score)
        if not options['include_custom']:
            queryset = queryset.filter(is_custom_species=False)

        rows = []
        seen_keys = set()
        for record in queryset.iterator():
            payload = build_record_payload(record)
            dedupe_key = (
                payload['confirmed_species'],
                f"{payload['size']:.4f}",
                f"{payload['weight']:.4f}",
                payload['rideable_only'],
            )
            if not options['keep_duplicates'] and dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            rows.append(payload)

        if output_path.suffix.lower() == '.jsonl':
            with output_path.open('w', encoding='utf-8') as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        elif output_path.suffix.lower() == '.csv':
            if not rows:
                raise CommandError('no rows available to export as csv')
            fieldnames = [key for key in rows[0].keys() if key != 'prediction_snapshot']
            with output_path.open('w', encoding='utf-8', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key) for key in fieldnames})
        else:
            raise CommandError('output must end with .jsonl or .csv')

        summary = {
            'output': str(output_path),
            'records': len(rows),
            'quality_statuses': sorted(quality_statuses),
            'verification_statuses': sorted(verification_statuses),
            'min_quality_score': min_quality_score,
            'include_custom': bool(options['include_custom']),
            'keep_duplicates': bool(options['keep_duplicates']),
        }
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
