from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('wxcloudrun', '0002_feedback_upstream_verification'),
    ]

    operations = [
        migrations.CreateModel(
            name='EggPredictorConfig',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('version', models.CharField(db_index=True, max_length=64)),
                (
                    'strategy',
                    models.CharField(
                        choices=[
                            ('upstream_proxy', 'Upstream Proxy'),
                            ('cloud_model', 'Cloud Model'),
                            ('hybrid', 'Hybrid'),
                        ],
                        db_index=True,
                        default='upstream_proxy',
                        max_length=32,
                    ),
                ),
                ('model_type', models.CharField(blank=True, default='', max_length=64)),
                ('artifact_uri', models.CharField(blank=True, default='', max_length=512)),
                ('config_json', models.TextField(blank=True, default='{}')),
                ('notes', models.CharField(blank=True, default='', max_length=255)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'EggPredictorConfig',
                'indexes': [
                    models.Index(fields=['is_active', 'updated_at'], name='eggcfg_active_idx'),
                ],
            },
        ),
    ]
