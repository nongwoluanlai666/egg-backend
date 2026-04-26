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


class MerchantSnapshot(models.Model):
    id = models.BigAutoField(primary_key=True)
    slot_date = models.DateField(db_index=True)
    round = models.PositiveSmallIntegerField(db_index=True)
    total_rounds = models.PositiveSmallIntegerField(default=4)
    next_refresh_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.CharField(max_length=32, blank=True, default='')
    items_json = models.TextField(default='[]', blank=True)
    fingerprint = models.CharField(max_length=64, unique=True)
    has_special_hit = models.BooleanField(default=False, db_index=True)
    special_item_names = models.CharField(max_length=255, blank=True, default='')
    notification_target_count = models.PositiveIntegerField(default=0)
    notification_success_count = models.PositiveIntegerField(default=0)
    notification_dispatched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self):
        return f'{self.slot_date}#{self.round}'

    class Meta:
        db_table = 'MerchantSnapshot'
        indexes = [
            models.Index(fields=['slot_date', 'round'], name='mch_slot_round_idx'),
            models.Index(fields=['has_special_hit', 'created_at'], name='mch_special_idx'),
        ]


class MerchantNoticeSubscription(models.Model):
    STATUS_IDLE = 'idle'
    STATUS_ACTIVE = 'active'
    STATUS_CONSUMED = 'consumed'
    STATUS_INVALID = 'invalid'
    STATUS_CHOICES = (
        (STATUS_IDLE, 'Idle'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_CONSUMED, 'Consumed'),
        (STATUS_INVALID, 'Invalid'),
    )

    id = models.BigAutoField(primary_key=True)
    openid = models.CharField(max_length=128, unique=True)
    openid_hash = models.CharField(max_length=64, db_index=True, blank=True, default='')
    appid = models.CharField(max_length=64, blank=True, default='')
    template_id = models.CharField(max_length=128, blank=True, default='')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_IDLE, db_index=True)
    subscribed_at = models.DateTimeField(default=timezone.now, db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    last_notified_snapshot = models.ForeignKey(
        MerchantSnapshot,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='notified_subscriptions',
    )
    notify_count = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=32, blank=True, default='')
    last_error_message = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.openid_hash[:8]} ({self.status})'

    class Meta:
        db_table = 'MerchantNoticeSubscription'
        indexes = [
            models.Index(fields=['status', 'subscribed_at'], name='mnotice_active_idx'),
            models.Index(fields=['openid_hash', 'updated_at'], name='mnotice_hash_idx'),
        ]


class MerchantNoticeSendLog(models.Model):
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_SKIPPED = 'skipped'
    STATUS_CHOICES = (
        (STATUS_SUCCESS, 'Success'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_SKIPPED, 'Skipped'),
    )

    id = models.BigAutoField(primary_key=True)
    subscription = models.ForeignKey(
        MerchantNoticeSubscription,
        on_delete=models.CASCADE,
        related_name='send_logs',
    )
    snapshot = models.ForeignKey(
        MerchantSnapshot,
        on_delete=models.CASCADE,
        related_name='send_logs',
    )
    template_id = models.CharField(max_length=128, blank=True, default='')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_FAILED, db_index=True)
    error_code = models.CharField(max_length=32, blank=True, default='')
    error_message = models.CharField(max_length=255, blank=True, default='')
    msg_id = models.CharField(max_length=64, blank=True, default='')
    special_item_names = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self):
        return f'{self.snapshot_id}:{self.subscription_id}:{self.status}'

    class Meta:
        db_table = 'MerchantNoticeSendLog'
        constraints = [
            models.UniqueConstraint(
                fields=['subscription', 'snapshot'],
                name='mnotice_send_subsnap_uq',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'created_at'], name='mnotice_send_status_idx'),
        ]


class MerchantNoticeJobState(models.Model):
    STATUS_IDLE = 'idle'
    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_SKIPPED = 'skipped'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = (
        (STATUS_IDLE, 'Idle'),
        (STATUS_RUNNING, 'Running'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_SKIPPED, 'Skipped'),
        (STATUS_FAILED, 'Failed'),
    )

    id = models.BigAutoField(primary_key=True)
    job_key = models.CharField(max_length=64, unique=True)
    guard_seconds = models.PositiveIntegerField(default=1800)
    last_triggered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_completed_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_IDLE)
    last_result_json = models.TextField(blank=True, default='{}')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.job_key} ({self.last_status})'

    class Meta:
        db_table = 'MerchantNoticeJobState'
        indexes = [
            models.Index(fields=['last_triggered_at', 'updated_at'], name='mnotice_job_run_idx'),
        ]
