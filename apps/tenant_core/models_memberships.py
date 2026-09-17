"""
apps/tenant_core/models_memberships.py — Layer 2 Module I: Memberships, Entitlements & Changes

12 Domain Models:
  1. Membership (memberships)
  2. MembershipContractSnapshot (membership_contract_snapshots)
  3. MembershipEntitlement (membership_entitlements)
  4. MembershipEntitlementLedger (membership_entitlement_ledger)
  5. MembershipBranchHistory (membership_branch_history)
  6. MembershipStatusHistory (membership_status_history)
  7. MembershipFreeze (membership_freezes)
  8. MembershipRenewalPolicy (membership_renewal_policies)
  9. MembershipChangePolicy (membership_change_policies)
  10. MembershipChangePolicyRule (membership_change_policy_rules)
  11. MembershipChangeRequest (membership_change_requests)
  12. MembershipPackageHistory (membership_package_history)

Key Rules Enforced:
- Contract immutability: MembershipContractSnapshot freezes exact commercial and legal state at purchase.
- Append-only entitlement ledger: All session and unit movements are recorded as discrete ledger rows with balance_after.
- Branch & status audit trail: Every home branch transfer or status transition creates an immutable history record.
- Versioned policies: Change & renewal policies are versioned with effective date ranges.
"""

import uuid
from decimal import Decimal
from typing import Optional
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_workforce import UserProfile
from .models_catalog import Program, Package, PackageVersion, PackagePrice, PackageEntitlementDefinition
from .models_commerce import Order, OrderItem


class Membership(models.Model):
    """
    Active or historical fitness membership instance bound to a member profile.
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending Activation'),
        ('ACTIVE', 'Active'),
        ('FROZEN', 'Frozen'),
        ('EXPIRED', 'Expired'),
        ('CANCELLED', 'Cancelled'),
        ('TERMINATED', 'Terminated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_profile = models.ForeignKey(UserProfile, on_delete=models.PROTECT, related_name='memberships')
    program = models.ForeignKey(Program, on_delete=models.PROTECT, null=True, blank=True, related_name='memberships')
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='memberships')
    package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, related_name='memberships')
    package_price = models.ForeignKey(PackagePrice, on_delete=models.PROTECT, null=True, blank=True, related_name='memberships')
    source_order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name='memberships')
    source_order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, null=True, blank=True, related_name='memberships')
    purchase_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='purchased_memberships')
    home_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='home_memberships')
    membership_number = models.CharField(max_length=100, unique=True, db_index=True)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    activated_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    legacy_reference = models.CharField(max_length=150, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self._state.adding and self.pk:
            db = kwargs.get('using') or self._state.db or 'default'
            orig = Membership.objects.using(db).filter(pk=self.pk).first()
            if orig and orig.purchase_branch_id != self.purchase_branch_id:
                raise ValidationError("purchase_branch is immutable and cannot be modified.")
        super().save(*args, **kwargs)

    class Meta:
        db_table = 'memberships'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.membership_number} - {self.package.name} ({self.status})"


class MembershipContractSnapshot(models.Model):
    """
    Immutable snapshot of the commercial agreement and purchased entitlement definitions.
    NO UPDATE, NO DELETE backend protection.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.OneToOneField(Membership, on_delete=models.PROTECT, related_name='contract_snapshot')
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='+')
    package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, related_name='+')
    package_price = models.ForeignKey(PackagePrice, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    package_name_snapshot = models.CharField(max_length=250)
    purchase_price = models.DecimalField(max_digits=14, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    final_amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    duration_value = models.IntegerField()
    duration_unit = models.CharField(max_length=20)
    start_date = models.DateField()
    end_date = models.DateField()
    entitlements_snapshot = models.JSONField(default=list)
    class_access_snapshot = models.JSONField(default=list, null=True, blank=True)
    purchase_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='+')
    terms_document_version_ids = models.JSONField(default=list, blank=True)
    applicable_policy_versions = models.JSONField(default=dict, blank=True)
    source_order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    source_order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding and self.pk:
            raise ValidationError("MembershipContractSnapshot is immutable and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("MembershipContractSnapshot is immutable and cannot be deleted.")

    class Meta:
        db_table = 'membership_contract_snapshots'

    def __str__(self):
        return f"Snapshot for {self.membership.membership_number} ({self.package_name_snapshot})"


class MembershipEntitlement(models.Model):
    """
    Consumable or unlimited entitlement instance (sessions, facility access, assessment counts).
    """
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('EXHAUSTED', 'Exhausted'),
        ('EXPIRED', 'Expired'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name='entitlements')
    source_definition = models.ForeignKey(
        PackageEntitlementDefinition, on_delete=models.PROTECT, null=True, blank=True, related_name='allocated_instances'
    )
    entitlement_type = models.CharField(max_length=50)
    reference_type = models.CharField(max_length=100, null=True, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)
    allocated_units = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    consumed_units = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    is_unlimited = models.BooleanField(default=False)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'membership_entitlements'

    @property
    def remaining_units(self) -> Optional[Decimal]:
        if self.is_unlimited:
            return None
        if self.allocated_units is None:
            return Decimal('0.00')
        return max(Decimal('0.00'), self.allocated_units - self.consumed_units)

    def __str__(self):
        return f"{self.entitlement_type} on {self.membership.membership_number} ({self.consumed_units}/{self.allocated_units or 'INF'})"


class MembershipEntitlementLedger(models.Model):
    """
    Append-only unit movement ledger ensuring total auditability of session credits, bookings, and refunds.
    """
    TRANSACTION_TYPE_CHOICES = [
        ('ALLOCATION', 'Initial Allocation'),
        ('CONSUMPTION', 'Session Consumption'),
        ('REVERSAL', 'Booking Cancellation Reversal'),
        ('ADJUSTMENT', 'Manual Admin Adjustment'),
        ('EXPIRY', 'Periodic Expiry'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership_entitlement = models.ForeignKey(
        MembershipEntitlement, on_delete=models.PROTECT, related_name='ledger_entries'
    )
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES)
    units = models.DecimalField(max_digits=12, decimal_places=2)
    booking_id = models.UUIDField(null=True, blank=True)
    reference_type = models.CharField(max_length=100, null=True, blank=True)
    reference_id = models.UUIDField(null=True, blank=True)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    reason_text = models.TextField(null=True, blank=True)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'membership_entitlement_ledger'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.transaction_type}: {self.units} (bal: {self.balance_after})"


class MembershipBranchHistory(models.Model):
    """
    Append-only log of membership home-branch transfers.
    """
    CHANGE_TYPE_CHOICES = [
        ('INITIAL', 'Initial Home Branch'),
        ('TRANSFER', 'Branch Transfer'),
        ('BRANCH_CLOSURE', 'Branch Closure Reassignment'),
        ('MEMBER_REQUEST', 'Member Relocation Request'),
        ('ADMIN_CORRECTION', 'Admin Correction'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name='branch_history')
    from_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='+')
    change_type = models.CharField(max_length=30, choices=CHANGE_TYPE_CHOICES, default='TRANSFER')
    reason = models.TextField(null=True, blank=True)
    effective_at = models.DateTimeField(default=timezone.now)
    changed_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'membership_branch_history'
        ordering = ['-created_at']


class MembershipStatusHistory(models.Model):
    """
    Append-only log of status changes (e.g. ACTIVE -> FROZEN -> ACTIVE -> EXPIRED).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name='status_history')
    from_status = models.CharField(max_length=20, null=True, blank=True)
    to_status = models.CharField(max_length=20)
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    reason_text = models.TextField(null=True, blank=True)
    changed_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    changed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'membership_status_history'
        ordering = ['-created_at']


class MembershipFreeze(models.Model):
    """
    Scheduled or active membership freeze window extending membership end dates.
    """
    STATUS_CHOICES = [
        ('SCHEDULED', 'Scheduled'),
        ('ACTIVE', 'Active Freeze'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name='freezes')
    freeze_from = models.DateField()
    freeze_until = models.DateField()
    reason_code = models.CharField(max_length=100, null=True, blank=True)
    reason_text = models.TextField(null=True, blank=True)
    extend_membership_days = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='SCHEDULED')
    approved_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'membership_freezes'
        ordering = ['-freeze_from']


class MembershipRenewalPolicy(models.Model):
    """
    Versioned policy for recurring or manual renewals of commercial packages.
    """
    PRICING_MODE_CHOICES = [
        ('CURRENT_PRICE', 'Current Catalog Price'),
        ('ORIGINAL_PRICE', 'Original Purchase Price'),
        ('GRANDFATHERED_PRICE', 'Grandfathered Discounted Price'),
        ('MANUAL_PRICE', 'Manual Custom Price'),
    ]
    ENTITLEMENT_MODE_CHOICES = [
        ('CURRENT_PACKAGE_VERSION', 'Adopt Current Package Version Entitlements'),
        ('EXISTING_PACKAGE_VERSION', 'Retain Original Purchased Version Entitlements'),
        ('CUSTOM', 'Custom Rule Configuration'),
    ]
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('RETIRED', 'Retired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='renewal_policies')
    package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='renewal_policies')
    version_number = models.IntegerField(default=1)
    renewal_pricing_mode = models.CharField(max_length=30, choices=PRICING_MODE_CHOICES, default='CURRENT_PRICE')
    renewal_entitlement_mode = models.CharField(max_length=30, choices=ENTITLEMENT_MODE_CHOICES, default='CURRENT_PACKAGE_VERSION')
    grace_days = models.IntegerField(null=True, blank=True, default=7)
    configuration = models.JSONField(default=dict, blank=True)
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'membership_renewal_policies'
        ordering = ['package', '-version_number']


class MembershipChangePolicy(models.Model):
    """
    Policy header governing membership modifications (upgrade, downgrade, cancellation, transfer).
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('SCHEDULED', 'Scheduled'),
        ('ACTIVE', 'Active'),
        ('RETIRED', 'Retired'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='membership_change_policies')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='membership_change_policies')
    program = models.ForeignKey(Program, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    policy_name = models.CharField(max_length=200)
    version_number = models.IntegerField(default=1)
    rule_behavior = models.CharField(max_length=20, default='CONTRACTUAL')
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    created_by_user = models.ForeignKey(TenantUser, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'membership_change_policies'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.policy_name} v{self.version_number} ({self.status})"


class MembershipChangePolicyRule(models.Model):
    """
    Specific rule rows governing upgrade/downgrade/cancellation fees, proration, and unconsumed session treatment.
    """
    CHANGE_TYPE_CHOICES = [
        ('UPGRADE', 'Package Upgrade'),
        ('DOWNGRADE', 'Package Downgrade'),
        ('CANCELLATION', 'Membership Cancellation'),
        ('EXTENSION', 'Validity Extension'),
        ('RENEWAL', 'Membership Renewal'),
        ('FREEZE', 'Membership Freeze'),
        ('REJOIN', 'Member Rejoin'),
        ('TRANSFER', 'Branch Transfer'),
        ('PLAN_CHANGE', 'General Plan Change'),
    ]
    EFFECTIVE_MODE_CHOICES = [
        ('IMMEDIATE', 'Immediate Execution'),
        ('NEXT_BILLING_CYCLE', 'Next Billing Cycle'),
        ('MEMBERSHIP_END', 'At End of Current Period'),
        ('SPECIFIC_DATE', 'Scheduled Future Date'),
        ('ADMIN_APPROVAL', 'Pending Manager Approval'),
    ]
    PRICING_MODE_CHOICES = [
        ('FULL_PRICE', 'Full Catalog Price'),
        ('DIFFERENCE_ONLY', 'Price Difference Only'),
        ('PRORATED', 'Prorated Difference based on Remaining Days'),
        ('CREDIT_REMAINING_VALUE', 'Credit Remaining Contract Value'),
        ('NO_CHARGE', 'Complimentary / No Charge'),
        ('MANUAL', 'Manual Review'),
    ]
    SESSION_HANDLING_CHOICES = [
        ('CARRY_FORWARD', 'Carry Forward Unconsumed Units'),
        ('CONVERT_TO_CREDIT', 'Convert Unconsumed Units to Credit'),
        ('EXPIRE', 'Forfeit / Expire Unconsumed Units'),
        ('KEEP_EXISTING', 'Retain Existing Entitlement Independent'),
        ('RESET', 'Reset Units to Target Package Version'),
        ('CUSTOM', 'Custom Rule Configuration'),
    ]
    VALIDITY_HANDLING_CHOICES = [
        ('KEEP_CURRENT_END_DATE', 'Keep Current End Date'),
        ('RECALCULATE_FROM_CHANGE_DATE', 'Recalculate From Change Date'),
        ('EXTEND_EXISTING_END_DATE', 'Extend Existing End Date'),
        ('USE_TARGET_PACKAGE_VALIDITY', 'Use Target Package Standard Validity'),
        ('CUSTOM', 'Custom Logic'),
    ]
    REFUND_MODE_CHOICES = [
        ('NONE', 'No Refund Allowed'),
        ('FULL', 'Full Refund'),
        ('PRORATED', 'Prorated Refund'),
        ('CREDIT_WALLET', 'Credit to Internal Wallet'),
        ('MANUAL', 'Manager Discretion'),
    ]
    FEE_TYPE_CHOICES = [
        ('NONE', 'No Cancellation Fee'),
        ('FIXED', 'Fixed Amount Fee'),
        ('PERCENTAGE', 'Percentage of Remaining Value'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership_change_policy = models.ForeignKey(MembershipChangePolicy, on_delete=models.PROTECT, related_name='rules')
    change_type = models.CharField(max_length=30, choices=CHANGE_TYPE_CHOICES)
    rule_name = models.CharField(max_length=200)
    min_membership_age_days = models.IntegerField(null=True, blank=True)
    max_membership_age_days = models.IntegerField(null=True, blank=True)
    min_remaining_days = models.IntegerField(null=True, blank=True)
    max_remaining_days = models.IntegerField(null=True, blank=True)
    min_remaining_sessions = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notice_period_days = models.IntegerField(null=True, blank=True, default=0)
    effective_mode = models.CharField(max_length=30, choices=EFFECTIVE_MODE_CHOICES, default='IMMEDIATE')
    pricing_mode = models.CharField(max_length=30, choices=PRICING_MODE_CHOICES, default='DIFFERENCE_ONLY')
    unused_session_handling = models.CharField(max_length=30, choices=SESSION_HANDLING_CHOICES, default='CARRY_FORWARD')
    validity_handling = models.CharField(max_length=40, choices=VALIDITY_HANDLING_CHOICES, default='RECALCULATE_FROM_CHANGE_DATE')
    refund_mode = models.CharField(max_length=20, choices=REFUND_MODE_CHOICES, default='NONE')
    cancellation_fee_type = models.CharField(max_length=20, choices=FEE_TYPE_CHOICES, default='NONE')
    cancellation_fee_value = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    requires_payment = models.BooleanField(default=False)
    requires_manager_approval = models.BooleanField(default=False)
    requires_member_confirmation = models.BooleanField(default=True)
    requires_terms_acceptance = models.BooleanField(default=False)
    target_package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    target_package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    priority = models.IntegerField(default=100)
    configuration = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=[('ACTIVE', 'Active'), ('INACTIVE', 'Inactive')], default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'membership_change_policy_rules'
        ordering = ['priority', 'created_at']

    def __str__(self):
        return f"{self.change_type} Rule: {self.rule_name} (priority={self.priority})"


class MembershipChangeRequest(models.Model):
    """
    Transactional record and immutable financial quote for an upgrade, downgrade, or cancellation.
    """
    STATUS_CHOICES = [
        ('REQUESTED', 'Requested'),
        ('QUOTED', 'Quoted / Calculation Ready'),
        ('PAYMENT_PENDING', 'Payment Pending'),
        ('APPROVED', 'Approved by Staff'),
        ('APPLIED', 'Applied / Executed'),
        ('REJECTED', 'Rejected'),
        ('CANCELLED', 'Cancelled by Member'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name='change_requests')
    membership_change_policy = models.ForeignKey(MembershipChangePolicy, on_delete=models.PROTECT, related_name='+')
    membership_change_policy_rule = models.ForeignKey(MembershipChangePolicyRule, on_delete=models.PROTECT, related_name='+')
    policy_version_number = models.IntegerField()
    change_type = models.CharField(max_length=30)
    current_package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='+')
    current_package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, related_name='+')
    target_package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    target_package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    effective_mode_applied = models.CharField(max_length=30)
    pricing_mode_applied = models.CharField(max_length=30)
    remaining_sessions_snapshot = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    remaining_days_snapshot = models.IntegerField(null=True, blank=True)
    original_remaining_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    credit_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    refund_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    penalty_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    additional_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    final_amount_payable = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    source_order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='REQUESTED')
    requested_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    approved_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    requested_at = models.DateTimeField(default=timezone.now)
    approved_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'membership_change_requests'
        ordering = ['-created_at']

    def __str__(self):
        return f"ChangeRequest {self.change_type} for {self.membership.membership_number} [{self.status}]"


class MembershipPackageHistory(models.Model):
    """
    Append-only record of package transitions over a membership's lifetime.
    """
    CHANGE_TYPE_CHOICES = [
        ('UPGRADE', 'Package Upgrade'),
        ('DOWNGRADE', 'Package Downgrade'),
        ('EXTENSION', 'Validity Extension'),
        ('RENEWAL', 'Package Renewal'),
        ('PLAN_CHANGE', 'General Plan Change'),
        ('REJOIN', 'Rejoin After Expiry'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name='package_history')
    from_package = models.ForeignKey(Package, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    from_package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    to_package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='+')
    to_package_version = models.ForeignKey(PackageVersion, on_delete=models.PROTECT, related_name='+')
    change_type = models.CharField(max_length=30, choices=CHANGE_TYPE_CHOICES)
    effective_at = models.DateTimeField(default=timezone.now)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    membership_change_request = models.ForeignKey(
        MembershipChangeRequest, on_delete=models.PROTECT, null=True, blank=True, related_name='+'
    )
    changed_by_user = models.ForeignKey(TenantUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'membership_package_history'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.membership.membership_number}: {self.from_package} -> {self.to_package} ({self.change_type})"
