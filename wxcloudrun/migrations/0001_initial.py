from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='EggFeedback',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('request_id', models.CharField(max_length=64, unique=True)),
                ('prediction_session_id', models.CharField(db_index=True, max_length=64)),
                ('source', models.CharField(choices=[('top1', 'Top 1'), ('top10', 'Top 10'), ('custom', 'Custom')], max_length=16)),
                ('size', models.DecimalField(decimal_places=4, max_digits=8)),
                ('weight', models.DecimalField(decimal_places=4, max_digits=10)),
                ('rideable_only', models.BooleanField(default=False)),
                ('confirmed_species', models.CharField(max_length=64)),
                ('predicted_species', models.CharField(blank=True, default='', max_length=64)),
                ('predicted_rank', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('predicted_probability', models.DecimalField(blank=True, decimal_places=4, max_digits=6, null=True)),
                ('prediction_version', models.CharField(default='local-v1', max_length=32)),
                ('prediction_snapshot', models.TextField(blank=True, default='[]')),
                ('candidate_count', models.PositiveSmallIntegerField(default=0)),
                ('is_custom_species', models.BooleanField(default=False)),
                ('species_in_snapshot', models.BooleanField(default=False)),
                ('quality_status', models.CharField(choices=[('pending', 'Pending'), ('accepted', 'Accepted'), ('suspicious', 'Suspicious')], default='pending', max_length=16)),
                ('quality_score', models.PositiveSmallIntegerField(default=0)),
                ('review_note', models.CharField(blank=True, default='', max_length=255)),
                ('appid', models.CharField(blank=True, default='', max_length=64)),
                ('openid_hash', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('ip_hash', models.CharField(blank=True, db_index=True, default='', max_length=64)),
                ('user_agent', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'EggFeedback',
                'indexes': [
                    models.Index(fields=['openid_hash', 'created_at'], name='eggfb_openid_idx'),
                    models.Index(fields=['ip_hash', 'created_at'], name='eggfb_ip_idx'),
                    models.Index(fields=['prediction_session_id', 'created_at'], name='eggfb_session_idx'),
                ],
            },
        ),
    ]
