"""
apps/tenant_core/models_discounts.py — Layer 2 Module H: Coupons, Discounts & Dynamic Offers

6 Domain Models:
  1. DiscountCampaign (discount_campaigns)
  2. DiscountCode (discount_codes)
  3. DiscountEligibilityRule (discount_eligibility_rules)
  4. DiscountRuleCondition (discount_rule_conditions)
  5. DiscountRuleAction (discount_rule_actions)
  6. DiscountRedemption (discount_redemptions)

Key Rules:
- Percentage, fixed amount, and capped discounts.
- Branch and package scoped eligibility.
- Global and per-user redemption limits.
- Dynamic eligibility evaluation (session consumption, package age, remaining percentage, etc.).
- Immutable discount redemption records linking orders and eligibility snapshots.
"""

import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import UserProfile
from .models_catalog import Package, PackageVersion
from .models_commerce import Order


class DiscountCampaign(models.Model):
    """
    Campaign defining discount rules, caps, limits and validity.
    """
    DISCOUNT_TYPE_CHOICES = [
        ('PERCENTAGE', 'Percentage Discount'),
        ('FIXED', 'Fixed Amount Discount'),
    ]
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('PAUSED', 'Paused'),
        ('EXPIRED', 'Expired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='discount_campaigns')
    name = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPE_CHOICES)
    discount_value = models.DecimalField(max_digits=14, decimal_places=4)
    max_discount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    minimum_order_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    usage_limit = models.IntegerField(null=True, blank=True)
    per_user_limit = models.IntegerField(null=True, blank=True)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    configuration = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'discount_campaigns'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.discount_type}: {self.discount_value})"

    def is_currently_valid(self) -> bool:
        now = timezone.now()
        if self.status != 'ACTIVE':
            return False
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now > self.valid_until:
            return False
        return True


class DiscountCode(models.Model):
    """
    Human or machine redeemable code belonging to a discount campaign.
    """
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('EXPIRED', 'Expired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(DiscountCampaign, on_delete=models.PROTECT, related_name='codes')
    code = models.CharField(max_length=100, db_index=True)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='discount_codes')
    package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='discount_codes')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'discount_codes'
        unique_together = [('campaign', 'code')]
        indexes = [
            models.Index(fields=['code', 'status']),
        ]
        ordering = ['code']

    def __str__(self):
        return f"{self.code} -> {self.campaign.name}"


class DiscountEligibilityRule(models.Model):
    """
    Dynamic package-specific eligibility rule for coupons, offers, cross-sells, or renewals.
    """
    RULE_TYPE_CHOICES = [
        ('COUPON', 'Coupon Eligibility Rule'),
        ('AUTO_DISCOUNT', 'Automatic Discount Rule'),
        ('OFFER', 'Dynamic Promotional Offer'),
        ('UPGRADE_OFFER', 'Upgrade Incentive Offer'),
        ('CROSS_SELL', 'Cross-Sell Offer'),
        ('RENEWAL', 'Renewal Incentive Offer'),
        ('REJOIN', 'Rejoin Incentive Offer'),
    ]
    EVALUATION_MODE_CHOICES = [
        ('ALL_CONDITIONS', 'All Conditions Must Match (AND)'),
        ('ANY_CONDITION', 'Any Condition Matches (OR)'),
    ]
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('EXPIRED', 'Expired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='discount_eligibility_rules')
    name = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    rule_type = models.CharField(max_length=30, choices=RULE_TYPE_CHOICES, default='COUPON')
    source_package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='source_eligibility_rules')
    source_package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='source_version_eligibility_rules')
    target_package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='target_eligibility_rules')
    target_package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='target_version_eligibility_rules')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='branch_eligibility_rules')
    priority = models.IntegerField(default=100)
    evaluation_mode = models.CharField(max_length=20, choices=EVALUATION_MODE_CHOICES, default='ALL_CONDITIONS')
    rule_behavior = models.CharField(max_length=20, default='TRANSACTIONAL')
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_by_user = models.ForeignKey(TenantUser, on_delete=models.PROTECT, null=True, blank=True, related_name='created_discount_rules')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'discount_eligibility_rules'
        ordering = ['priority', '-created_at']

    def __str__(self):
        return f"{self.name} [{self.rule_type}] (priority={self.priority})"


class DiscountRuleCondition(models.Model):
    """
    Individual condition evaluating member state, session consumption, package age, etc.
    """
    OPERATOR_CHOICES = [
        ('EQUALS', 'Equals (==)'),
        ('NOT_EQUALS', 'Not Equals (!=)'),
        ('GREATER_THAN', 'Greater Than (>)'),
        ('GREATER_THAN_OR_EQUAL', 'Greater Than or Equal (>=)'),
        ('LESS_THAN', 'Less Than (<)'),
        ('LESS_THAN_OR_EQUAL', 'Less Than or Equal (<=)'),
        ('IN', 'In List'),
        ('NOT_IN', 'Not In List'),
        ('BETWEEN', 'Between Range'),
        ('EXISTS', 'Exists / True'),
        ('NOT_EXISTS', 'Not Exists / False'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    discount_eligibility_rule = models.ForeignKey(DiscountEligibilityRule, on_delete=models.PROTECT, related_name='conditions')
    condition_type = models.CharField(max_length=60)
    operator = models.CharField(max_length=30, choices=OPERATOR_CHOICES)
    numeric_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    text_value = models.TextField(null=True, blank=True)
    boolean_value = models.BooleanField(null=True, blank=True)
    reference_type = models.CharField(max_length=100, null=True, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)
    sequence = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'discount_rule_conditions'
        ordering = ['sequence', 'created_at']

    def __str__(self):
        return f"{self.condition_type} {self.operator} ({self.numeric_value or self.text_value or self.boolean_value})"


class DiscountRuleAction(models.Model):
    """
    Action produced when an eligibility rule matches.
    """
    ACTION_TYPE_CHOICES = [
        ('SHOW_COUPON', 'Show Coupon Code'),
        ('APPLY_COUPON', 'Apply Coupon Code'),
        ('APPLY_PERCENTAGE_DISCOUNT', 'Apply Percentage Discount'),
        ('APPLY_FIXED_DISCOUNT', 'Apply Fixed Discount'),
        ('SHOW_OFFER', 'Show Dynamic Offer Banner'),
        ('ALLOW_UPGRADE_PRICE', 'Allow Special Upgrade Price'),
        ('GENERATE_COUPON', 'Generate Unique Coupon'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    discount_eligibility_rule = models.ForeignKey(DiscountEligibilityRule, on_delete=models.PROTECT, related_name='actions')
    action_type = models.CharField(max_length=40, choices=ACTION_TYPE_CHOICES)
    discount_campaign = models.ForeignKey(DiscountCampaign, on_delete=models.PROTECT, null=True, blank=True, related_name='rule_actions')
    discount_code = models.ForeignKey(DiscountCode, on_delete=models.PROTECT, null=True, blank=True, related_name='rule_actions')
    discount_percentage = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    maximum_discount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    message = models.TextField(null=True, blank=True)
    auto_apply = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'discount_rule_actions'
        ordering = ['created_at']

    def __str__(self):
        return f"{self.action_type}: {self.message or self.discount_percentage or self.discount_amount}"


class DiscountRedemption(models.Model):
    """
    Immutable audit record of discount actually consumed on an order.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    discount_code = models.ForeignKey(DiscountCode, on_delete=models.PROTECT, null=True, blank=True, related_name='redemptions')
    campaign = models.ForeignKey(DiscountCampaign, on_delete=models.PROTECT, related_name='redemptions')
    eligibility_rule = models.ForeignKey(DiscountEligibilityRule, on_delete=models.PROTECT, null=True, blank=True, related_name='redemptions')
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name='discount_redemptions')
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='discount_redemptions')
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2)
    eligibility_snapshot = models.JSONField(default=dict, blank=True)
    redeemed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'discount_redemptions'
        ordering = ['-redeemed_at']

    def __str__(self):
        return f"Redemption {self.discount_amount} on Order {self.order_id}"
