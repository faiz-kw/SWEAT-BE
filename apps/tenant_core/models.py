"""
Tenant core app models — re-exports from all model modules.
"""

from .models_org import Organization, CompanyEntity, Location, Branch

from .models_users import TenantUser, Department, UserBranch, UserDepartment

from .models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role,
    RolePermissionSet, RoleModuleAccess, RoleSubmoduleAccess,
    RolePermissionSetItem, RoleAssignment, BranchModule,
)

from .models_govern import OrganizationSettings, BranchSettings, NotificationTemplate, InAppNotification

from .models_privacy import (
    ProcessingPurpose, ConsentRecord, PrivacyRequest, TenantAuditEvent,
)

from .models_infra import File, Integration, LegacyEntityMap

from .models_audit_outbox import (
    BusinessAuditEvent, IdempotencyRecord, DomainOutboxEvent, ReasonCode,
)

from .models_workforce import (
    UserProfile, EmployeeProfile, TrainerProfile, SalesProfile,
    EmployeeWorkSchedule, EmployeeScheduleException, TrainerSpecialty, TrainerSpecialtyAssignment,
)

from .models_crm import (
    LeadSource, Lead, LeadStatusHistory, LeadAssignment, LeadNote,
    LeadActivity, IntakeForm, IntakeQuestion, IntakeQuestionOption,
    IntakeSubmission, IntakeAnswer, TrialBooking, TrialStatusHistory,
    LeadConversion, SalesFollowupTask, LeadCommercialProfile,
)

from .models_catalog import (
    TermsDocument, TermsDocumentVersion, TermsAcceptance,
    ProgramCategory, ProgramType, Program, Package, PackageVersion,
    PackagePrice, PackageBranchAvailability, PackageEntitlementDefinition,
)

from .models_classes import (
    ClassCategory, ClassTemplate, ClassPrice, ClassBranchAvailability,
    ClassScheduleRule, ClassOccurrence, ClassOccurrenceTrainer,
    PackageClassAccessRule, ClassSpecialtyRequirement,
    ClassScheduleImportBatch, ClassScheduleImportRow,
    ClassContentItem, ClassContentMapping, ClassContentAssignment,
    ClassDemandEvent, ClassDemandPlanningRun, ClassScheduleRecommendation,
)

__all__ = [
    # Org
    'Organization', 'CompanyEntity', 'Location', 'Branch',
    # Users
    'TenantUser', 'Department', 'UserBranch', 'UserDepartment',
    # RBAC
    'ModuleCatalog', 'SubmoduleCatalog', 'Permission', 'Role',
    'RolePermissionSet', 'RoleModuleAccess', 'RoleSubmoduleAccess',
    'RolePermissionSetItem', 'RoleAssignment', 'BranchModule',
    # Govern
    'OrganizationSettings', 'BranchSettings', 'NotificationTemplate', 'InAppNotification',
    # Privacy
    'ProcessingPurpose', 'ConsentRecord', 'PrivacyRequest', 'TenantAuditEvent',
    # Infra
    'File', 'Integration', 'LegacyEntityMap',
    # Module N: Audit & Reliability
    'BusinessAuditEvent', 'IdempotencyRecord', 'DomainOutboxEvent', 'ReasonCode',
    # Module A: Workforce & Trainers
    'UserProfile', 'EmployeeProfile', 'TrainerProfile', 'SalesProfile',
    'EmployeeWorkSchedule', 'EmployeeScheduleException', 'TrainerSpecialty', 'TrainerSpecialtyAssignment',
    # Module B: CRM & Sales
    'LeadSource', 'Lead', 'LeadStatusHistory', 'LeadAssignment', 'LeadNote',
    'LeadActivity', 'IntakeForm', 'IntakeQuestion', 'IntakeQuestionOption',
    'IntakeSubmission', 'IntakeAnswer', 'TrialBooking', 'TrialStatusHistory',
    'LeadConversion', 'SalesFollowupTask',
    # Module C: Terms & Legal
    'TermsDocument', 'TermsDocumentVersion', 'TermsAcceptance',
    # Module D: Programs, Packages & Catalog
    'ProgramCategory', 'ProgramType', 'Program', 'Package', 'PackageVersion',
    'PackagePrice', 'PackageBranchAvailability', 'PackageEntitlementDefinition',
    # Module E: Group Classes, Scheduling, Content Studio & Demand Planning
    'ClassCategory', 'ClassTemplate', 'ClassPrice', 'ClassBranchAvailability',
    'ClassScheduleRule', 'ClassOccurrence', 'ClassOccurrenceTrainer',
    'PackageClassAccessRule', 'ClassSpecialtyRequirement',
    'ClassScheduleImportBatch', 'ClassScheduleImportRow',
    'ClassContentItem', 'ClassContentMapping', 'ClassContentAssignment',
    'ClassDemandEvent', 'ClassDemandPlanningRun', 'ClassScheduleRecommendation',
]

from .models_appointments import (
    AppointmentType,
    Appointment,
    AppointmentTrainer,
    AppointmentTypeSpecialtyRequirement,
)

__all__ += [
    'AppointmentType',
    'Appointment',
    'AppointmentTrainer',
    'AppointmentTypeSpecialtyRequirement',
]

from .models_commerce import (
    Order,
    OrderItem,
    PaymentTransaction,
    Refund,
    MemberInvoice,
    PaymentLink,
    PaymentLinkEvent,
)

__all__ += [
    'Order',
    'OrderItem',
    'PaymentTransaction',
    'Refund',
    'MemberInvoice',
    'PaymentLink',
    'PaymentLinkEvent',
]

from .models_discounts import (
    DiscountCampaign,
    DiscountCode,
    DiscountEligibilityRule,
    DiscountRuleCondition,
    DiscountRuleAction,
    DiscountRedemption,
)

__all__ += [
    'DiscountCampaign',
    'DiscountCode',
    'DiscountEligibilityRule',
    'DiscountRuleCondition',
    'DiscountRuleAction',
    'DiscountRedemption',
]

from .models_memberships import (
    Membership,
    MembershipContractSnapshot,
    MembershipEntitlement,
    MembershipEntitlementLedger,
    MembershipBranchHistory,
    MembershipStatusHistory,
    MembershipFreeze,
    MembershipRenewalPolicy,
    MembershipChangePolicy,
    MembershipChangePolicyRule,
    MembershipChangeRequest,
    MembershipPackageHistory,
)

__all__ += [
    'Membership',
    'MembershipContractSnapshot',
    'MembershipEntitlement',
    'MembershipEntitlementLedger',
    'MembershipBranchHistory',
    'MembershipStatusHistory',
    'MembershipFreeze',
    'MembershipRenewalPolicy',
    'MembershipChangePolicy',
    'MembershipChangePolicyRule',
    'MembershipChangeRequest',
    'MembershipPackageHistory',
]

from .models_bookings import (
    BookingPolicySet,
    BookingCancellationRule,
    RewardRedemptionRule,
    AttendancePolicySet,
    AttendancePenaltyRule,
    MemberAttendanceState,
    Booking,
    BookingStatusHistory,
    BookingReschedule,
    BookingCancellation,
    BookingWaitlistEvent,
    AttendancePolicyEvent,
)

__all__ += [
    'BookingPolicySet',
    'BookingCancellationRule',
    'RewardRedemptionRule',
    'AttendancePolicySet',
    'AttendancePenaltyRule',
    'MemberAttendanceState',
    'Booking',
    'BookingStatusHistory',
    'BookingReschedule',
    'BookingCancellation',
    'BookingWaitlistEvent',
    'AttendancePolicyEvent',
]

from .models_attendance import (
    AttendanceRecord,
    AccessEvent,
)

__all__ += [
    'AttendanceRecord',
    'AccessEvent',
]

from .models_referrals import (
    ReferralProgram,
    ReferralIdentifier,
    Referral,
    ReferralQualificationRule,
    ReferralBenefitRule,
)

__all__ += [
    'ReferralProgram',
    'ReferralIdentifier',
    'Referral',
    'ReferralQualificationRule',
    'ReferralBenefitRule',
]

from .models_rewards import (
    RewardAccount,
    RewardLedger,
    OrderRewardRedemption,
)

__all__ += [
    'RewardAccount',
    'RewardLedger',
    'OrderRewardRedemption',
]

from .models_approvals import (
    ApprovalRequest,
    ApprovalAction,
)

__all__ += [
    'ApprovalRequest',
    'ApprovalAction',
]

from .models_communication import (
    CommunicationMessage,
    CommunicationStatusEvent,
)

__all__ += [
    'CommunicationMessage',
    'CommunicationStatusEvent',
]

from .models_automation import (
    AutomationWorkflow,
    AutomationWorkflowVersion,
    AutomationExecution,
    AutomationStepExecution,
)

__all__ += [
    'AutomationWorkflow',
    'AutomationWorkflowVersion',
    'AutomationExecution',
    'AutomationStepExecution',
]

from .models_attention import (
    CRMAttentionPolicy,
)

__all__ += [
    'CRMAttentionPolicy',
]


