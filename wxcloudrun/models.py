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
