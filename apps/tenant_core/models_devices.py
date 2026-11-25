"""
Turnstile Device Registry — Tenant DB Model

Stores the configuration for every physical access-control terminal
(Dahua TrueFace3000 / TimeWatch) across all studios.

Adding a new studio or machine is purely a data operation — no code change needed.

Design decisions:
- credentials (admin user/pass) are NOT stored here; they come from env vars
  DAHUA_ADMIN_USER and DAHUA_ADMIN_PASS set per-deployment.
- host is the reachable host FROM the cloud (i.e., the studio public IP or domain)
  with port already embedded as a separate field. When a studio uses Router Port
  Forwarding, host = studio public IP, port = forwarded port
  (e.g., host="203.x.x.x", port=8114).
- class_category_code: if set, only bookings in that category trigger a push to
  this device (Andheri Pilates gate vs. Bootcamp gate). NULL means accept ALL
  categories (default for single-gate studios like Malad / Goregaon).
- is_active: safe on/off toggle without deleting the device config.
"""

import uuid
from django.db import models
from django.utils import timezone
from .models_org import Branch


class TurnstileDevice(models.Model):
    """
    One row per physical terminal (Entry and Exit are separate rows).
    """

    DIRECTION_CHOICES = [
        ('ENTRY', 'Entry'),
        ('EXIT', 'Exit'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name='turnstile_devices',
        help_text='Which studio branch this terminal belongs to.',
    )

    # Human-readable label — shown in logs and admin UI
    name = models.CharField(
        max_length=100,
        help_text='e.g. "Malad Entry", "Andheri Pilates Entry", "Andheri Bootcamp Exit"',
    )

    direction = models.CharField(
        max_length=10,
        choices=DIRECTION_CHOICES,
        default='ENTRY',
        help_text='Whether this terminal controls entry or exit.',
    )

    # Network address reachable FROM THE CLOUD
    # For LAN-only setups this will be the private IP (192.168.x.x) —
    # it won't work from the cloud until port forwarding is configured.
    # For fully live studios: this is the studio public IP.
    host = models.CharField(
        max_length=255,
        help_text=(
            'IP or hostname reachable from the cloud. '
            'Private IP (192.168.x.x) for pre-port-forwarding testing; '
            'public IP or domain after port forwarding is set up.'
        ),
    )

    port = models.PositiveIntegerField(
        default=80,
        help_text=(
            'HTTP port of the terminal management API. '
            '80 for LAN direct access; use the forwarded external port '
            '(e.g. 8114, 8152) when routing via public IP.'
        ),
    )

    # Optional: device serial for audit / vendor reference
    device_serial = models.CharField(max_length=100, blank=True, default='')

    # Room-level routing (Andheri only for now)
    # Match against ClassTemplate.category.code (case-insensitive).
    # NULL / empty → this device handles ALL class categories at this branch.
    class_category_code = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text=(
            'Optional. If set, only tokens for bookings in this class category '
            'are pushed to this device. Leave blank for universal access '
            '(Malad, Goregaon). Set to e.g. "PILATES" or "BOOTCAMP" for Andheri.'
        ),
    )

    is_active = models.BooleanField(
        default=True,
        help_text='Deactivate to stop syncing tokens to this device without deleting it.',
    )

    # Timestamps
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenant_core'
        db_table = 'turnstile_devices'
        ordering = ['branch', 'name']
        indexes = [
            models.Index(fields=['branch', 'is_active']),
            models.Index(fields=['is_active', 'direction']),
        ]

    def __str__(self) -> str:
        return f'{self.name} ({self.direction}) @ {self.host}:{self.port}'

    @property
    def base_url(self) -> str:
        """Convenience: returns http://host:port (omits :80 for readability)."""
        if self.port == 80:
            return f'http://{self.host}'
        return f'http://{self.host}:{self.port}'

    def matches_category(self, category_code: str | None) -> bool:
        """
        Returns True if this device should handle a booking for the given
        class category code.

        Rules:
        - Device has no category filter (empty) → matches everything.
        - Device has a category filter → only matches bookings whose
          class_template.category.code matches (case-insensitive).
        """
        if not self.class_category_code:
            return True
        if not category_code:
            # Booking has no category (e.g. Elite open access) → allow on all universal devices
            return not bool(self.class_category_code)
        return self.class_category_code.strip().upper() == category_code.strip().upper()
