from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    OrganizationViewSet, LocationViewSet, BranchViewSet, CompanyEntityViewSet,
    TenantUserViewSet, DepartmentViewSet, RoleViewSet, RoleAssignmentViewSet, UserBranchViewSet,
    ModuleCatalogViewSet, SubmoduleCatalogViewSet, PermissionViewSet,
    BranchModuleViewSet, RolePermissionSetViewSet,
    OrganizationSettingsViewSet, BranchSettingsViewSet,
    NotificationTemplateViewSet, TenantAuditEventViewSet,
    BranchWorkingHoursViewSet, BranchOperatingExceptionViewSet,
    VerifyAccessView, TenantDatabaseHealthView,
)
from .views_storage import (
    FileViewSet,
    StorageConfirmUploadView,
    StoragePresignDownloadView,
    StoragePresignUploadView,
)
from .views_privacy import (
    ProcessingPurposeViewSet,
    ConsentRecordViewSet,
    PrivacyRequestViewSet,
)
from .views_integrations import TenantIntegrationViewSet
from .views_security_policy import SecurityPolicyView
from .views_audit_outbox import (
    BusinessAuditEventViewSet,
    IdempotencyRecordViewSet,
    DomainOutboxEventViewSet,
    ReasonCodeViewSet,
)
from .views_workforce import (
    UserProfileViewSet,
    EmployeeProfileViewSet,
    TrainerProfileViewSet,
    SalesProfileViewSet,
    EmployeeWorkScheduleViewSet,
    EmployeeScheduleExceptionViewSet,
    TrainerSpecialtyViewSet,
    TrainerSpecialtyAssignmentViewSet,
)
from .views_crm import (
    LeadSourceViewSet,
    LeadViewSet,
    LeadStatusHistoryViewSet,
    LeadAssignmentViewSet,
    LeadNoteViewSet,
    LeadActivityViewSet,
    IntakeFormViewSet,
    IntakeQuestionViewSet,
    IntakeQuestionOptionViewSet,
    IntakeSubmissionViewSet,
    TrialBookingViewSet,
    TrialStatusHistoryViewSet,
    LeadConversionViewSet,
    SalesFollowupTaskViewSet,
)
from .views_catalog import (
    TermsDocumentViewSet,
    TermsDocumentVersionViewSet,
    TermsAcceptanceViewSet,
    ProgramCategoryViewSet,
    ProgramTypeViewSet,
    ProgramViewSet,
    PackageViewSet,
    PackageVersionViewSet,
    PackagePriceViewSet,
    PackageBranchAvailabilityViewSet,
    PackageEntitlementDefinitionViewSet,
)
from .views_classes import (
    ClassesMetadataView,
    ClassCategoryViewSet,
    ClassTemplateViewSet,
    ClassPriceViewSet,
    ClassBranchAvailabilityViewSet,
    ClassScheduleRuleViewSet,
    ClassOccurrenceViewSet,
    ClassOccurrenceTrainerViewSet,
    PackageClassAccessRuleViewSet,
    ClassSpecialtyRequirementViewSet,
    ClassScheduleImportBatchViewSet,
    ClassScheduleImportRowViewSet,
    ClassContentItemViewSet,
    ClassContentMappingViewSet,
    ClassContentAssignmentViewSet,
    ClassDemandEventViewSet,
    ClassDemandPlanningRunViewSet,
    ClassScheduleRecommendationViewSet,
)
from .views_appointments import (
    AppointmentTypeViewSet,
    AppointmentTypeSpecialtyRequirementViewSet,
    AppointmentViewSet,
    AppointmentTrainerViewSet,
)
from .views_commerce import (
    OrderViewSet,
    OrderItemViewSet,
    PaymentTransactionViewSet,
    RefundViewSet,
    MemberInvoiceViewSet,
    PaymentLinkViewSet,
)
from .views_discounts import (
    DiscountCampaignViewSet,
    DiscountCodeViewSet,
    DiscountEligibilityRuleViewSet,
    DiscountRedemptionViewSet,
)
from .views_memberships import (
    MembershipViewSet,
    MembershipEntitlementViewSet,
    MembershipEntitlementLedgerViewSet,
    MembershipFreezeViewSet,
    MembershipRenewalPolicyViewSet,
    MembershipChangePolicyViewSet,
    MembershipChangeRequestViewSet,
)
from .views_bookings import (
    BookingViewSet,
    BookingPolicySetViewSet,
    BookingCancellationRuleViewSet,
    RewardRedemptionRuleViewSet,
    AttendancePolicySetViewSet,
    AttendancePenaltyRuleViewSet,
    MemberAttendanceStateViewSet,
    AttendanceRecordViewSet,
    AccessEventViewSet,
)
from .views_rewards import (
    ReferralProgramViewSet,
    ReferralIdentifierViewSet,
    ReferralViewSet,
    ReferralQualificationRuleViewSet,
    ReferralBenefitRuleViewSet,
    RewardAccountViewSet,
    RewardLedgerViewSet,
    OrderRewardRedemptionViewSet,
)
from .views_approvals import (
    ApprovalRequestViewSet,
    ApprovalActionViewSet,
)

router = DefaultRouter()
router.register(r'approval-requests', ApprovalRequestViewSet, basename='approval-request')
router.register(r'approval-actions', ApprovalActionViewSet, basename='approval-action')

router.register(r'referral-programs', ReferralProgramViewSet, basename='referral-program')
router.register(r'referral-identifiers', ReferralIdentifierViewSet, basename='referral-identifier')
router.register(r'referrals', ReferralViewSet, basename='referral')
router.register(r'referral-qualification-rules', ReferralQualificationRuleViewSet, basename='referral-qualification-rule')
router.register(r'referral-benefit-rules', ReferralBenefitRuleViewSet, basename='referral-benefit-rule')
router.register(r'reward-accounts', RewardAccountViewSet, basename='reward-account')
router.register(r'reward-ledgers', RewardLedgerViewSet, basename='reward-ledger')
router.register(r'order-reward-redemptions', OrderRewardRedemptionViewSet, basename='order-reward-redemption')

router.register(r'bookings', BookingViewSet, basename='booking')
router.register(r'booking-policy-sets', BookingPolicySetViewSet, basename='booking-policy-set')
router.register(r'booking-cancellation-rules', BookingCancellationRuleViewSet, basename='booking-cancellation-rule')
router.register(r'reward-redemption-rules', RewardRedemptionRuleViewSet, basename='reward-redemption-rule')
router.register(r'attendance-policy-sets', AttendancePolicySetViewSet, basename='attendance-policy-set')
router.register(r'attendance-penalty-rules', AttendancePenaltyRuleViewSet, basename='attendance-penalty-rule')
router.register(r'member-attendance-states', MemberAttendanceStateViewSet, basename='member-attendance-state')
router.register(r'attendance-records', AttendanceRecordViewSet, basename='attendance-record')
router.register(r'access-events', AccessEventViewSet, basename='access-event')

router.register(r'memberships', MembershipViewSet, basename='membership')
router.register(r'membership-entitlements', MembershipEntitlementViewSet, basename='membership-entitlement')
router.register(r'membership-entitlement-ledgers', MembershipEntitlementLedgerViewSet, basename='membership-entitlement-ledger')
router.register(r'membership-freezes', MembershipFreezeViewSet, basename='membership-freeze')
router.register(r'membership-renewal-policies', MembershipRenewalPolicyViewSet, basename='membership-renewal-policy')
router.register(r'membership-change-policies', MembershipChangePolicyViewSet, basename='membership-change-policy')
router.register(r'membership-change-requests', MembershipChangeRequestViewSet, basename='membership-change-request')

router.register(r'discount-campaigns', DiscountCampaignViewSet, basename='discount-campaign')
router.register(r'discount-codes', DiscountCodeViewSet, basename='discount-code')
router.register(r'discount-eligibility-rules', DiscountEligibilityRuleViewSet, basename='discount-eligibility-rule')
router.register(r'discount-redemptions', DiscountRedemptionViewSet, basename='discount-redemption')

router.register(r'orders', OrderViewSet, basename='order')
router.register(r'order-items', OrderItemViewSet, basename='order-item')
router.register(r'payment-transactions', PaymentTransactionViewSet, basename='payment-transaction')
router.register(r'refunds', RefundViewSet, basename='refund')
router.register(r'member-invoices', MemberInvoiceViewSet, basename='member-invoice')
router.register(r'payment-links', PaymentLinkViewSet, basename='payment-link')

router.register(r'appointment-types', AppointmentTypeViewSet, basename='appointment-type')
router.register(r'appointment-type-specialties', AppointmentTypeSpecialtyRequirementViewSet, basename='appointment-type-specialty')
router.register(r'appointments', AppointmentViewSet, basename='appointment')
router.register(r'appointment-trainers', AppointmentTrainerViewSet, basename='appointment-trainer')
router.register(r'class-categories', ClassCategoryViewSet, basename='class-category')
router.register(r'class-templates', ClassTemplateViewSet, basename='class-template')
router.register(r'class-prices', ClassPriceViewSet, basename='class-price')
router.register(r'class-branch-availability', ClassBranchAvailabilityViewSet, basename='class-branch-availability')
router.register(r'class-branch-availabilities', ClassBranchAvailabilityViewSet, basename='class-branch-availabilities')
router.register(r'class-schedule-rules', ClassScheduleRuleViewSet, basename='class-schedule-rule')
router.register(r'class-occurrences', ClassOccurrenceViewSet, basename='class-occurrence')
router.register(r'class-occurrence-trainers', ClassOccurrenceTrainerViewSet, basename='class-occurrence-trainer')
router.register(r'package-class-access-rules', PackageClassAccessRuleViewSet, basename='package-class-access-rule')
router.register(r'class-specialty-requirements', ClassSpecialtyRequirementViewSet, basename='class-specialty-requirement')
router.register(r'class-schedule-import-batches', ClassScheduleImportBatchViewSet, basename='class-schedule-import-batch')
router.register(r'class-schedule-import-rows', ClassScheduleImportRowViewSet, basename='class-schedule-import-row')
router.register(r'class-content-items', ClassContentItemViewSet, basename='class-content-item')
router.register(r'class-content-mappings', ClassContentMappingViewSet, basename='class-content-mapping')
router.register(r'class-content-assignments', ClassContentAssignmentViewSet, basename='class-content-assignment')
router.register(r'class-demand-events', ClassDemandEventViewSet, basename='class-demand-event')
router.register(r'class-demand-planning-runs', ClassDemandPlanningRunViewSet, basename='class-demand-planning-run')
router.register(r'class-schedule-recommendations', ClassScheduleRecommendationViewSet, basename='class-schedule-recommendation')

router.register(r'terms-documents', TermsDocumentViewSet, basename='terms-document')
router.register(r'terms-document-versions', TermsDocumentVersionViewSet, basename='terms-document-version')
router.register(r'terms-acceptances', TermsAcceptanceViewSet, basename='terms-acceptance')
router.register(r'program-categories', ProgramCategoryViewSet, basename='program-category')
router.register(r'program-types', ProgramTypeViewSet, basename='program-type')
router.register(r'programs', ProgramViewSet, basename='program')
router.register(r'packages', PackageViewSet, basename='package')
router.register(r'package-versions', PackageVersionViewSet, basename='package-version')
router.register(r'package-prices', PackagePriceViewSet, basename='package-price')
router.register(r'package-branch-availability', PackageBranchAvailabilityViewSet, basename='package-branch-availability')

router.register(r'package-entitlement-definitions', PackageEntitlementDefinitionViewSet, basename='package-entitlement-definition')

router.register(r'business-audit-events', BusinessAuditEventViewSet, basename='business-audit-event')
router.register(r'idempotency-records', IdempotencyRecordViewSet, basename='idempotency-record')
router.register(r'outbox-events', DomainOutboxEventViewSet, basename='outbox-event')
router.register(r'reason-codes', ReasonCodeViewSet, basename='reason-code')
router.register(r'user-profiles', UserProfileViewSet, basename='user-profile')
router.register(r'employee-profiles', EmployeeProfileViewSet, basename='employee-profile')
router.register(r'trainer-profiles', TrainerProfileViewSet, basename='trainer-profile')
router.register(r'sales-profiles', SalesProfileViewSet, basename='sales-profile')
router.register(r'work-schedules', EmployeeWorkScheduleViewSet, basename='work-schedule')
router.register(r'schedule-exceptions', EmployeeScheduleExceptionViewSet, basename='schedule-exception')
router.register(r'trainer-specialties', TrainerSpecialtyViewSet, basename='trainer-specialty')
router.register(r'trainer-specialty-assignments', TrainerSpecialtyAssignmentViewSet, basename='trainer-specialty-assignment')
router.register(r'lead-sources', LeadSourceViewSet, basename='lead-source')
router.register(r'leads', LeadViewSet, basename='lead')
router.register(r'lead-status-history', LeadStatusHistoryViewSet, basename='lead-status-history')
router.register(r'lead-assignments', LeadAssignmentViewSet, basename='lead-assignment')
router.register(r'lead-notes', LeadNoteViewSet, basename='lead-note')
router.register(r'lead-activities', LeadActivityViewSet, basename='lead-activity')
router.register(r'intake-forms', IntakeFormViewSet, basename='intake-form')
router.register(r'intake-questions', IntakeQuestionViewSet, basename='intake-question')
router.register(r'intake-question-options', IntakeQuestionOptionViewSet, basename='intake-question-option')
router.register(r'intake-submissions', IntakeSubmissionViewSet, basename='intake-submission')
router.register(r'trial-bookings', TrialBookingViewSet, basename='trial-booking')
router.register(r'trial-status-history', TrialStatusHistoryViewSet, basename='trial-status-history')
router.register(r'lead-conversions', LeadConversionViewSet, basename='lead-conversion')
router.register(r'sales-followup-tasks', SalesFollowupTaskViewSet, basename='sales-followup-task')
router.register(r'organizations', OrganizationViewSet, basename='organization')
router.register(r'company-entities', CompanyEntityViewSet, basename='company-entity')
router.register(r'locations', LocationViewSet, basename='location')
router.register(r'branches', BranchViewSet, basename='branch')
router.register(r'users', TenantUserViewSet, basename='user')
router.register(r'user-branches', UserBranchViewSet, basename='user-branch')
router.register(r'departments', DepartmentViewSet, basename='department')
router.register(r'roles', RoleViewSet, basename='role')
router.register(r'role-assignments', RoleAssignmentViewSet, basename='role-assignment')
router.register(r'modules', ModuleCatalogViewSet, basename='module')
router.register(r'module-catalog', ModuleCatalogViewSet, basename='module-catalog')
router.register(r'submodules', SubmoduleCatalogViewSet, basename='submodule')
router.register(r'permissions', PermissionViewSet, basename='permission')
router.register(r'branch-modules', BranchModuleViewSet, basename='branch-module')
router.register(r'permission-sets', RolePermissionSetViewSet, basename='permission-set')
router.register(r'organization-settings', OrganizationSettingsViewSet, basename='organization-settings')
router.register(r'branch-settings', BranchSettingsViewSet, basename='branch-settings')
router.register(r'branch-working-hours', BranchWorkingHoursViewSet, basename='branch-working-hours')
router.register(r'branch-operating-exceptions', BranchOperatingExceptionViewSet, basename='branch-operating-exceptions')
router.register(r'notification-templates', NotificationTemplateViewSet, basename='notification-template')
router.register(r'audit-events', TenantAuditEventViewSet, basename='audit-event')
router.register(r'files', FileViewSet, basename='file')
router.register(r'processing-purposes', ProcessingPurposeViewSet, basename='processing-purpose')
router.register(r'consent-records', ConsentRecordViewSet, basename='consent-record')
router.register(r'privacy-requests', PrivacyRequestViewSet, basename='privacy-request')
router.register(r'integrations', TenantIntegrationViewSet, basename='integration')

app_name = 'tenant_core'

urlpatterns = [
    path('database-health/', TenantDatabaseHealthView.as_view(), name='tenant-database-health'),
    path('verify-access/', VerifyAccessView.as_view(), name='verify-access'),
    path('security-policy/current/', SecurityPolicyView.as_view(), name='tenant-security-policy'),
    path('storage/presign-upload/', StoragePresignUploadView.as_view(), name='storage-presign-upload'),
    path('storage/confirm-upload/', StorageConfirmUploadView.as_view(), name='storage-confirm-upload'),
    path('storage/presign-download/<uuid:id>/', StoragePresignDownloadView.as_view(), name='storage-presign-download'),
    path('classes/metadata/', ClassesMetadataView.as_view(), name='classes-metadata'),
    path('', include(router.urls)),
]
