import uuid
from django.db import models
from django.utils import timezone
from .models_users import TenantUser
from .models_crm import UserProfile
from .models_commerce import Order
from .models_memberships import Membership
from .models_referrals import Referral


class RewardAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_profile = models.OneToOneField(UserProfile, on_delete=models.PROTECT, related_name='reward_account')
    points_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0.0)
    credit_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0.0)
    lifetime_earned = models.DecimalField(max_digits=14, decimal_places=2, default=0.0)
    lifetime_redeemed = models.DecimalField(max_digits=14, decimal_places=2, default=0.0)
    status = models.CharField(
        max_length=20,
        default='ACTIVE',
        choices=[('ACTIVE', 'ACTIVE'), ('SUSPENDED', 'SUSPENDED')]
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'reward_accounts'
        ordering = ['-created_at']

    def __str__(self):
        return f"RewardAccount {self.user_profile} (Points: {self.points_balance}, Credit: {self.credit_balance})"


class RewardLedger(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reward_account = models.ForeignKey(RewardAccount, on_delete=models.PROTECT, related_name='ledger_entries')
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name='reward_ledger_entries')
    transaction_type = models.CharField(
        max_length=20,
        choices=[
            ('EARN', 'EARN'),
            ('REDEEM', 'REDEEM'),
            ('EXPIRE', 'EXPIRE'),
            ('REVERSAL', 'REVERSAL'),
            ('ADJUSTMENT', 'ADJUSTMENT'),
        ]
    )
    reward_type = models.CharField(
        max_length=20,
        choices=[
            ('POINTS', 'POINTS'),
            ('CREDIT', 'CREDIT'),
            ('COUPON', 'COUPON'),
            ('FREE_SESSION', 'FREE_SESSION'),
            ('INCENTIVE', 'INCENTIVE'),
        ]
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=2)
    referral = models.ForeignKey(Referral, on_delete=models.PROTECT, null=True, blank=True, related_name='reward_entries')
    order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name='reward_entries')
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True, related_name='reward_entries')
    reference_type = models.CharField(max_length=100, null=True, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_reward_ledger_entries')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'reward_ledger'
        ordering = ['-created_at']

    def __str__(self):
        return f"RewardLedger {self.transaction_type} {self.quantity} {self.reward_type}"


class OrderRewardRedemption(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='reward_redemptions')
    reward_account = models.ForeignKey(RewardAccount, on_delete=models.PROTECT, related_name='order_redemptions')
    reward_ledger = models.ForeignKey(RewardLedger, on_delete=models.PROTECT, related_name='order_redemptions')
    reward_type = models.CharField(max_length=20)
    units_redeemed = models.DecimalField(max_digits=14, decimal_places=2)
    monetary_value = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'order_reward_redemptions'
        ordering = ['-created_at']

    def __str__(self):
        return f"OrderRewardRedemption {self.order} - {self.units_redeemed} units (${self.monetary_value})"
