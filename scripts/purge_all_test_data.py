import os, sys, django
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.master.models import Tenant, TenantDataSource
from apps.master.models_iam import AuthenticationIdentity
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias, set_tenant_db_alias

t = Tenant.objects.get(slug='sweat')
ds = TenantDataSource.objects.get(tenant=t)
alias = build_tenant_db_alias(t.id)
_register_tenant_connection(alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
set_tenant_db_alias(alias)

from django.db import transaction
from django.apps import apps
from apps.tenant_core.models_org import Branch, Organization
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import (
    ProgramCategory, ProgramType, Program, ProgramBranchAvailability,
    Package, PackageVersion, PackagePrice, PackageBranchAvailability, PackageEntitlementDefinition
)
from apps.tenant_core.models_classes import (
    ClassCategory, ClassTemplate, ClassPrice, ClassBranchAvailability,
    ClassScheduleRule, ClassOccurrence, ClassOccurrenceTrainer, PackageClassAccessRule,
    ClassSpecialtyRequirement, ClassScheduleImportBatch, ClassScheduleImportRow,
    ClassContentItem, ClassContentMapping, ClassContentAssignment,
    ClassDemandEvent, ClassDemandPlanningRun, ClassScheduleRecommendation
)
from apps.tenant_core.models_crm import (
    Lead, IntakeSubmission, IntakeAnswer, TrialBooking, TrialStatusHistory,
    LeadStatusHistory, LeadAssignment, LeadActivity, LeadNote, LeadConversion, SalesFollowupTask,
    LeadAttribution, LeadCommercialProfile
)
from apps.tenant_core.models_memberships import (
    Membership, MembershipContractSnapshot, MembershipEntitlement, MembershipEntitlementLedger,
    MembershipFreeze, MembershipStatusHistory, MembershipBranchHistory, MembershipPackageHistory,
    MembershipChangeRequest, MembershipChangePolicy, MembershipChangePolicyRule, MembershipRenewalPolicy
)
from apps.tenant_core.models_commerce import (
    Order, OrderItem, MemberInvoice, PaymentLink, PaymentLinkEvent, PaymentTransaction, Refund
)
from apps.tenant_core.models_discounts import (
    DiscountCampaign, DiscountCode, DiscountEligibilityRule,
    DiscountRuleCondition, DiscountRuleAction, DiscountRedemption
)
from apps.tenant_core.models_bookings import (
    BookingPolicySet, BookingCancellationRule, RewardRedemptionRule, AttendancePolicySet,
    AttendancePenaltyRule, MemberAttendanceState, Booking, BookingStatusHistory,
    BookingReschedule, BookingCancellation, BookingWaitlistEvent, AttendancePolicyEvent
)
from apps.tenant_core.models_attendance import AttendanceRecord, AccessEvent
from apps.tenant_core.models_communication import CommunicationMessage, CommunicationStatusEvent

TermsAcceptance = apps.get_model('tenant_core', 'TermsAcceptance')
ReferralQualificationRule = apps.get_model('tenant_core', 'ReferralQualificationRule')
ReferralBenefitRule = apps.get_model('tenant_core', 'ReferralBenefitRule')
RewardLedger = apps.get_model('tenant_core', 'RewardLedger')
OrderRewardRedemption = apps.get_model('tenant_core', 'OrderRewardRedemption')

staff_emails = ['admin@sweat.com', 'frontdesk@sweat.com', 'sales@sweat.com', 'trainer@sweat.com']

print("=== STARTING COMPLETE DATA PURGE FOR 'SWEAT' TENANT ===")

with transaction.atomic(using=alias):
    # 1. Communications
    print("Purging Communications...")
    CommunicationStatusEvent.objects.using(alias).all().delete()
    CommunicationMessage.objects.using(alias).all().delete()

    # 2. Attendance & Bookings
    print("Purging Attendance & Bookings...")
    AccessEvent.objects.using(alias).all().delete()
    AttendanceRecord.objects.using(alias).all().delete()
    AttendancePolicyEvent.objects.using(alias).all().delete()
    MemberAttendanceState.objects.using(alias).all().delete()
    BookingWaitlistEvent.objects.using(alias).all().delete()
    BookingCancellation.objects.using(alias).all().delete()
    BookingReschedule.objects.using(alias).all().delete()
    BookingStatusHistory.objects.using(alias).all().delete()
    Booking.objects.using(alias).all().delete()
    RewardRedemptionRule.objects.using(alias).all().delete()
    BookingCancellationRule.objects.using(alias).all().delete()
    AttendancePenaltyRule.objects.using(alias).all().delete()
    AttendancePolicySet.objects.using(alias).all().delete()

    # 3. Memberships
    print("Purging Memberships...")
    OrderRewardRedemption.objects.using(alias).all().delete()
    RewardLedger.objects.using(alias).all().delete()
    MembershipEntitlementLedger.objects.using(alias).all().delete()
    MembershipEntitlement.objects.using(alias).all().delete()
    MembershipContractSnapshot.objects.using(alias).all().delete()
    MembershipFreeze.objects.using(alias).all().delete()
    MembershipStatusHistory.objects.using(alias).all().delete()
    MembershipBranchHistory.objects.using(alias).all().delete()
    MembershipPackageHistory.objects.using(alias).all().delete()
    MembershipChangeRequest.objects.using(alias).all().delete()
    MembershipChangePolicyRule.objects.using(alias).all().delete()
    MembershipChangePolicy.objects.using(alias).all().delete()
    MembershipRenewalPolicy.objects.using(alias).all().delete()
    Membership.objects.using(alias).all().delete()

    # 4. Commerce
    print("Purging Commerce...")
    Refund.objects.using(alias).all().delete()
    PaymentTransaction.objects.using(alias).all().delete()
    PaymentLinkEvent.objects.using(alias).all().delete()
    PaymentLink.objects.using(alias).all().delete()
    MemberInvoice.objects.using(alias).all().delete()
    DiscountRedemption.objects.using(alias).all().delete()
    OrderItem.objects.using(alias).all().delete()
    Order.objects.using(alias).all().delete()
    DiscountRuleAction.objects.using(alias).all().delete()
    DiscountRuleCondition.objects.using(alias).all().delete()
    DiscountEligibilityRule.objects.using(alias).all().delete()
    DiscountCode.objects.using(alias).all().delete()
    DiscountCampaign.objects.using(alias).all().delete()

    # 5. CRM & Leads
    print("Purging CRM & Leads...")
    TermsAcceptance.objects.using(alias).all().delete()
    SalesFollowupTask.objects.using(alias).all().delete()
    LeadConversion.objects.using(alias).all().delete()
    LeadNote.objects.using(alias).all().delete()
    LeadActivity.objects.using(alias).all().delete()
    LeadAssignment.objects.using(alias).all().delete()
    LeadStatusHistory.objects.using(alias).all().delete()
    TrialStatusHistory.objects.using(alias).all().delete()
    TrialBooking.objects.using(alias).all().delete()
    IntakeAnswer.objects.using(alias).all().delete()
    IntakeSubmission.objects.using(alias).all().delete()
    LeadAttribution.objects.using(alias).all().delete()
    LeadCommercialProfile.objects.using(alias).all().delete()
    Lead.objects.using(alias).all().delete()

    # 6. Classes
    print("Purging Classes...")
    PackageClassAccessRule.objects.using(alias).all().delete()
    ClassOccurrenceTrainer.objects.using(alias).all().delete()
    ClassContentAssignment.objects.using(alias).all().delete()
    ClassOccurrence.objects.using(alias).all().delete()
    ClassScheduleRule.objects.using(alias).all().delete()
    ClassBranchAvailability.objects.using(alias).all().delete()
    ClassPrice.objects.using(alias).all().delete()
    ClassSpecialtyRequirement.objects.using(alias).all().delete()
    ClassScheduleImportRow.objects.using(alias).all().delete()
    ClassScheduleImportBatch.objects.using(alias).all().delete()
    ClassContentMapping.objects.using(alias).all().delete()
    ClassContentItem.objects.using(alias).all().delete()
    ClassDemandEvent.objects.using(alias).all().delete()
    ClassScheduleRecommendation.objects.using(alias).all().delete()
    ClassDemandPlanningRun.objects.using(alias).all().delete()
    BookingPolicySet.objects.using(alias).filter(class_template__isnull=False).delete()
    BookingPolicySet.objects.using(alias).filter(program__isnull=False).delete()
    ClassTemplate.objects.using(alias).all().delete()
    ClassCategory.objects.using(alias).all().delete()

    # 7. Catalog (Programs & Packages)
    print("Purging Catalog...")
    ReferralBenefitRule.objects.using(alias).all().delete()
    ReferralQualificationRule.objects.using(alias).all().delete()
    PackageEntitlementDefinition.objects.using(alias).all().delete()
    PackageBranchAvailability.objects.using(alias).all().delete()
    PackagePrice.objects.using(alias).all().delete()
    PackageVersion.objects.using(alias).all().delete()
    Package.objects.using(alias).all().delete()
    ProgramBranchAvailability.objects.using(alias).all().delete()
    Program.objects.using(alias).all().delete()
    ProgramType.objects.using(alias).all().delete()
    ProgramCategory.objects.using(alias).all().delete()

    # 8. Member Users & Profiles (Preserve staff accounts)
    print("Purging Member Users...")
    users_to_delete = TenantUser.objects.using(alias).exclude(email__in=staff_emails)
    user_ids_to_delete = [str(uid) for uid in users_to_delete.values_list('id', flat=True)]
    print(f"Users to delete: {list(users_to_delete.values_list('email', flat=True))}")
    
    UserProfile.objects.using(alias).filter(user__in=users_to_delete).delete()
    UserBranch.objects.using(alias).filter(user__in=users_to_delete).delete()
    deleted_tenant_users_count, _ = users_to_delete.delete()
    print(f"Deleted {deleted_tenant_users_count} test member user objects from Tenant DB.")

    # In master DB, clean up AuthenticationIdentity for the deleted user IDs
    with transaction.atomic(using='default'):
        deleted_identities, _ = AuthenticationIdentity.objects.using('default').filter(subject_id__in=user_ids_to_delete).delete()
        print(f"Deleted {deleted_identities} AuthenticationIdentity records from master DB.")

print("\n--- ALL PURGE TRANSACTIONS COMMITTED SUCCESSFULLY! ---")
