from django.db import models
from django.utils import timezone


class Counters(models.Model):
    id = models.AutoField(primary_key=True)
    count = models.IntegerField(default=0)
    createdAt = models.DateTimeField(default=timezone.now)
    updatedAt = models.DateTimeField(auto_now=True)

    def __str__(self):
        return str(self.count)

    class Meta:
        db_table = 'Counters'
        managed = False


class EggFeedback(models.Model):
    SOURCE_TOP1 = 'top1'
    SOURCE_TOP10 = 'top10'
    SOURCE_CUSTOM = 'custom'
    SOURCE_CHOICES = (
        (SOURCE_TOP1, 'Top 1'),
        (SOURCE_TOP10, 'Top 10'),
        (SOURCE_CUSTOM, 'Custom'),
    )

    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_SUSPICIOUS = 'suspicious'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'Pending'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_SUSPICIOUS, 'Suspicious'),
    )

    VERIFICATION_UNKNOWN = 'unknown'
    VERIFICATION_MATCHED = 'matched'
    VERIFICATION_TOP10 = 'top10'
    VERIFICATION_PRESENT = 'present'
    VERIFICATION_MISMATCH = 'mismatch'
    VERIFICATION_ERROR = 'error'
    VERIFICATION_CHOICES = (
        (VERIFICATION_UNKNOWN, 'Unknown'),
        (VERIFICATION_MATCHED, 'Matched'),
        (VERIFICATION_TOP10, 'Top 10'),
        (VERIFICATION_PRESENT, 'Present'),
        (VERIFICATION_MISMATCH, 'Mismatch'),
        (VERIFICATION_ERROR, 'Error'),
    )

    id = models.BigAutoField(primary_key=True)
    request_id = models.CharField(max_length=64, unique=True)
    prediction_session_id = models.CharField(max_length=64, db_index=True)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    size = models.DecimalField(max_digits=8, decimal_places=4)
    weight = models.DecimalField(max_digits=10, decimal_places=4)
    rideable_only = models.BooleanField(default=False)
    confirmed_species = models.CharField(max_length=64)
    predicted_species = models.CharField(max_length=64, blank=True, default='')
    predicted_rank = models.PositiveSmallIntegerField(null=True, blank=True)
    predicted_probability = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    prediction_version = models.CharField(max_length=32, default='local-v1')
    prediction_snapshot = models.TextField(default='[]', blank=True)
    candidate_count = models.PositiveSmallIntegerField(default=0)
    is_custom_species = models.BooleanField(default=False)
    species_in_snapshot = models.BooleanField(default=False)
    quality_status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    quality_score = models.PositiveSmallIntegerField(default=0)
    review_note = models.CharField(max_length=255, blank=True, default='')
    upstream_top1_species = models.CharField(max_length=64, blank=True, default='')
    upstream_top1_probability = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    upstream_confirmed_rank = models.PositiveSmallIntegerField(null=True, blank=True)
    upstream_confirmed_probability = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    upstream_verification_status = models.CharField(
        max_length=16,
        choices=VERIFICATION_CHOICES,
        default=VERIFICATION_UNKNOWN,
        db_index=True,
    )
    upstream_checked_at = models.DateTimeField(null=True, blank=True)
    appid = models.CharField(max_length=64, blank=True, default='')
    openid_hash = models.CharField(max_length=64, db_index=True, blank=True, default='')
    ip_hash = models.CharField(max_length=64, db_index=True, blank=True, default='')
    user_agent = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.confirmed_species} ({self.source})'

    class Meta:
        db_table = 'EggFeedback'
        indexes = [
            models.Index(fields=['openid_hash', 'created_at'], name='eggfb_openid_idx'),
            models.Index(fields=['ip_hash', 'created_at'], name='eggfb_ip_idx'),
            models.Index(fields=['prediction_session_id', 'created_at'], name='eggfb_session_idx'),
        ]


class EggPredictorConfig(models.Model):
    STRATEGY_UPSTREAM_PROXY = 'upstream_proxy'
    STRATEGY_CLOUD_MODEL = 'cloud_model'
    STRATEGY_HYBRID = 'hybrid'
    STRATEGY_CHOICES = (
        (STRATEGY_UPSTREAM_PROXY, 'Upstream Proxy'),
        (STRATEGY_CLOUD_MODEL, 'Cloud Model'),
        (STRATEGY_HYBRID, 'Hybrid'),
    )

    id = models.BigAutoField(primary_key=True)
    version = models.CharField(max_length=64, db_index=True)
    strategy = models.CharField(
        max_length=32,
        choices=STRATEGY_CHOICES,
        default=STRATEGY_UPSTREAM_PROXY,
        db_index=True,
    )
    model_type = models.CharField(max_length=64, blank=True, default='')
    artifact_uri = models.CharField(max_length=512, blank=True, default='')
    config_json = models.TextField(blank=True, default='{}')
    notes = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.version} ({self.strategy})'

    class Meta:
        db_table = 'EggPredictorConfig'
        indexes = [
            models.Index(fields=['is_active', 'updated_at'], name='eggcfg_active_idx'),
        ]
