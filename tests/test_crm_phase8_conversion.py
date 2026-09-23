"""
tests/test_crm_phase8_conversion.py

CRM Phase 8: Lead -> Member Conversion Tests

Covers:
- get_conversion_eligibility: eligible / already_converted / no_branch
- get_conversion_quote: catalog validation, price resolution, coupon preview
- execute_conversion: full happy path, idempotency replay, already-converted guard
- Identity resolution: new identity, existing phone match, identity conflict
- API endpoints: conversion-eligibility, conversion-quote, convert
- Payment amount guard: amount < quoted total raises ValidationError
"""

import uuid
from decimal import Decimal
from datetime import date
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import (
    Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
)
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_rbac import (
    Role, RoleAssignment, ModuleCatalog, SubmoduleCatalog,
    Permission, RolePermissionSet, RolePermissionSetItem,
    RoleModuleAccess, RoleSubmoduleAccess,
)
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import (
    ProgramCategory, Program, Package, PackageVersion, PackagePrice,
    PackageEntitlementDefinition, PackageBranchAvailability,
)
from apps.tenant_core.models_crm import Lead, LeadConversion
from apps.tenant_core.models_commerce import Order, PaymentTransaction, MemberInvoice
from apps.tenant_core.models_memberships import (
    Membership, MembershipContractSnapshot, MembershipEntitlement, MembershipEntitlementLedger
)
from apps.tenant_core.models_discounts import DiscountCampaign, DiscountCode, DiscountRedemption
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent, DomainOutboxEvent
from apps.tenant_core.services_crm import (
    LeadConversionService, CRMLeadService, IdentityConflictError, LeadAlreadyConvertedError
)
from apps.tenant_core.services_attention import LeadAttentionService


DB = 'tenant_test'


class CRMPhase8ConversionTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def setUp(self):
        set_tenant_db_alias(DB)

        # Master DB
        self.tenant = Tenant.objects.using('default').create(
            code='P8-TENANT', name='Phase8 Gym', slug='phase8-gym', status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant, db_name='test_fitness_tenant',
            database_name='test_fitness_tenant', status='ACTIVE', database_engine='POSTGRESQL',
        )
        plan = SaasPlan.objects.using('default').create(
            name='Phase8 Plan', code='P8-PLAN', tier='ENTERPRISE', status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant, plan=plan, status='ACTIVE',
        )
        prod_mod_core, _ = ProductModule.objects.using('default').get_or_create(
            code='core', defaults={'name': 'Core Module', 'status': 'ACTIVE'},
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant, module=prod_mod_core, is_enabled=True, availability_mode='ALL_BRANCHES',
        )
        prod_mod_crm, _ = ProductModule.objects.using('default').get_or_create(
            code='crm', defaults={'name': 'CRM Module', 'status': 'ACTIVE'},
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant, module=prod_mod_crm, is_enabled=True, availability_mode='ALL_BRANCHES',
        )
        prod_mod_fin, _ = ProductModule.objects.using('default').get_or_create(
            code='finance', defaults={'name': 'Finance Module', 'status': 'ACTIVE'},
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant, module=prod_mod_fin, is_enabled=True, availability_mode='ALL_BRANCHES',
        )

        # Tenant DB - Org + Branch
        self.org = Organization.objects.using(DB).create(
            code='P8-ORG', name='Phase8 Organization', status='ACTIVE',
        )
        loc = Location.objects.using(DB).create(
            organization=self.org, code='P8-LOC', name='Main Location', status='ACTIVE',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-P8', name='Phase8 Branch', status='ACTIVE',
        )

        # Admin user (sales staff for conversions)
        self.admin = TenantUser.objects.using(DB).create(
            organization=self.org, email='admin@phase8.test',
            first_name='Admin', last_name='User', status='ACTIVE',
        )
        self.admin.set_password('Secret123!')
        self.admin.save(using=DB)

        # RBAC — minimal setup; give admin superuser-like access
        mod_cat, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        sub_cat, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_cat, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )
        perm_edit, _ = Permission.objects.using(DB).get_or_create(
            module=mod_cat, submodule=sub_cat, action='edit',
            defaults={'permission_code': 'crm.leads.edit', 'label': 'Edit Leads'}
        )
        perm_convert, _ = Permission.objects.using(DB).get_or_create(
            module=mod_cat, submodule=sub_cat, action='convert',
            defaults={'permission_code': 'crm.leads.convert', 'label': 'Convert Leads'}
        )
        mod_cat_fin, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='finance', defaults={'name': 'Finance', 'is_enabled': True}
        )
        sub_cat_fin, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_cat_fin, submodule_code='payments', defaults={'name': 'Payments', 'is_enabled': True}
        )
        self.perm_payment, _ = Permission.objects.using(DB).get_or_create(
            module=mod_cat_fin, submodule=sub_cat_fin, action='create',
            defaults={'permission_code': 'finance.payments.create', 'label': 'Record Payments'}
        )

        role = Role.objects.using(DB).create(
            organization=self.org, name='CRM Admin', code='CRM_ADMIN_P8',
            is_system_role=True, scope='ORG', is_active=True,
        )
        RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod_cat, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub_cat, defaults={'can_access': True})
        RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod_cat_fin, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub_cat_fin, defaults={'can_access': True})
        pset = RolePermissionSet.objects.using(DB).create(role=role, name='CRM Admin Perms')
        for perm in [perm_edit, perm_convert, self.perm_payment]:
            RolePermissionSetItem.objects.using(DB).get_or_create(
                permission_set=pset, permission=perm, defaults={'granted': True}
            )
        RoleAssignment.objects.using(DB).create(
            user=self.admin, role=role, organization=self.org, is_active=True
        )

        # Catalog
        cat = ProgramCategory.objects.using(DB).create(
            organization=self.org, code='P8-CAT', name='P8 Category', status='ACTIVE',
        )
        self.program = Program.objects.using(DB).create(
            organization=self.org, category=cat, code='P8-PROG', name='Elite Fitness', status='ACTIVE',
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org, program=self.program,
            name='Monthly Basic', code='PKG-P8-MONTHLY', status='ACTIVE',
        )
        self.pkg_version = PackageVersion.objects.using(DB).create(
            package=self.package, version_number=1, name_snapshot='Monthly Basic v1',
            duration_value=1, duration_unit='MONTH', effective_from=timezone.now(),
            status='ACTIVE', created_by_user=self.admin,
        )
        self.pkg_price = PackagePrice.objects.using(DB).create(
            package_version=self.pkg_version, currency='INR',
            base_price=Decimal('5000.00'), tax_percent=Decimal('0.000'),
            prices_include_tax=True,
            effective_from=timezone.now(), created_by_user=self.admin, status='ACTIVE',
        )
        PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.pkg_version, entitlement_type='CLASS_SESSIONS',
            allocated_units=Decimal('20.00'), is_unlimited=False, status='ACTIVE',
        )

        # Lead (NEW_LEAD status, with branch assigned)
        self.lead = Lead.objects.using(DB).create(
            organization=self.org,
            branch=self.branch,
            first_name='Priya',
            last_name='Kapoor',
            phone_normalized='+919876543210',
            email_normalized='priya.kapoor@example.com',
            current_status='NEW_LEAD',
        )

        # Auth token
        from apps.authentication.views import _build_tenant_token
        refresh = _build_tenant_token(user=self.admin, tenant=self.tenant, db_alias=DB)
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    # ------------------------------------------------------------------
    # Eligibility Tests
    # ------------------------------------------------------------------

    def test_eligibility_new_lead(self):
        """A fresh lead with a branch is eligible."""
        result = LeadConversionService.get_conversion_eligibility(self.lead, db_alias=DB)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['reason'], 'eligible')

    def test_eligibility_no_branch(self):
        """A lead without a branch is not eligible."""
        lead_no_branch = Lead.objects.using(DB).create(
            organization=self.org, first_name='No', last_name='Branch',
            current_status='NEW_LEAD',
        )
        result = LeadConversionService.get_conversion_eligibility(lead_no_branch, db_alias=DB)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'], 'no_branch')

    def test_eligibility_already_converted(self):
        """A CONVERTED lead is not eligible."""
        self.lead.current_status = 'CONVERTED'
        self.lead.save(using=DB)
        result = LeadConversionService.get_conversion_eligibility(self.lead, db_alias=DB)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['reason'], 'already_converted')

    # ------------------------------------------------------------------
    # Quote Tests
    # ------------------------------------------------------------------

    def test_conversion_quote_happy_path(self):
        """Quote returns correct catalog + pricing data."""
        quote = LeadConversionService.get_conversion_quote(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            db_alias=DB,
        )
        self.assertEqual(quote['program']['name'], 'Elite Fitness')
        self.assertEqual(quote['package']['name'], 'Monthly Basic')
        self.assertEqual(quote['pricing']['subtotal'], '5000.00')
        self.assertEqual(quote['pricing']['total_payable'], '5000.00')
        self.assertEqual(quote['pricing']['discount_amount'], '0.00')
        self.assertIsInstance(quote['entitlements'], list)
        self.assertEqual(len(quote['entitlements']), 1)
        self.assertEqual(quote['entitlements'][0]['entitlement_type'], 'CLASS_SESSIONS')

    def test_conversion_quote_inactive_package_raises(self):
        """Quoting a retired package version raises ValidationError."""
        self.pkg_version.status = 'RETIRED'
        self.pkg_version.save(using=DB)
        with self.assertRaises(ValidationError):
            LeadConversionService.get_conversion_quote(
                lead=self.lead,
                package_version_id=str(self.pkg_version.id),
                branch_id=str(self.branch.id),
                db_alias=DB,
            )

    def test_conversion_quote_no_price_raises(self):
        """Quoting a version with no active price raises ValidationError."""
        self.pkg_price.status = 'INACTIVE'
        self.pkg_price.save(using=DB)
        with self.assertRaises(ValidationError):
            LeadConversionService.get_conversion_quote(
                lead=self.lead,
                package_version_id=str(self.pkg_version.id),
                branch_id=str(self.branch.id),
                db_alias=DB,
            )

    def test_conversion_quote_payment_providers_availability(self):
        """
        Quote returns only offline methods and active Integration payment gateways.
        Unconfigured gateways (e.g. STRIPE, RAZORPAY) are not exposed unless an
        Integration row with status='ACTIVE' exists for the tenant.
        """
        from apps.tenant_core.models_infra import Integration

        # 1. Without active gateway integrations, only offline methods are exposed
        quote = LeadConversionService.get_conversion_quote(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            db_alias=DB,
        )
        self.assertIn('CASH', quote['payment_providers'])
        self.assertIn('BANK_TRANSFER', quote['payment_providers'])
        self.assertIn('OTHER', quote['payment_providers'])
        self.assertNotIn('RAZORPAY', quote['payment_providers'])
        self.assertNotIn('STRIPE', quote['payment_providers'])
        self.assertNotIn('ICICI_POS', quote['payment_providers'])

        # 2. Add an ACTIVE Razorpay integration -> RAZORPAY becomes available
        Integration.objects.using(DB).create(
            integration_type='PAYMENT',
            provider='Razorpay',
            status='ACTIVE',
        )
        quote_with_gateway = LeadConversionService.get_conversion_quote(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            db_alias=DB,
        )
        self.assertIn('RAZORPAY', quote_with_gateway['payment_providers'])
        self.assertNotIn('STRIPE', quote_with_gateway['payment_providers'])
        self.assertNotIn('ICICI_POS', quote_with_gateway['payment_providers'])

    # ------------------------------------------------------------------
    # Identity Resolution Tests
    # ------------------------------------------------------------------

    def test_identity_resolution_creates_new_user(self):
        """Brand new lead with no matching user → new TenantUser created as INVITED."""
        profile, created = LeadConversionService.resolve_or_create_member_identity(
            lead=self.lead, branch=self.branch, db_alias=DB,
        )
        self.assertTrue(created)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.user.status, 'INVITED')
        self.assertEqual(profile.user.is_login_allowed, True)
        self.assertEqual(profile.user.password_hash, '!unusable')

    def test_identity_resolution_reuses_existing_phone_user(self):
        """Lead phone matches existing TenantUser → reused without creating new user."""
        existing = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='existing@phase8.test',
            phone='+919876543210',
            first_name='Priya',
            last_name='Kapoor',
            status='ACTIVE',
        )
        UserProfile.objects.using(DB).create(
            user=existing,
            first_name_snapshot='Priya',
            last_name_snapshot='Kapoor',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )
        profile, created = LeadConversionService.resolve_or_create_member_identity(
            lead=self.lead, branch=self.branch, db_alias=DB,
        )
        self.assertFalse(created)
        self.assertEqual(profile.user.pk, existing.pk)

    def test_identity_conflict_raises(self):
        """Phone → UserA, Email → UserB → IdentityConflictError raised."""
        user_a = TenantUser.objects.using(DB).create(
            organization=self.org, email='usera@phase8.test', phone='+919876543210',
            first_name='A', last_name='User', status='ACTIVE',
        )
        user_b = TenantUser.objects.using(DB).create(
            organization=self.org, email='priya.kapoor@example.com', phone='+911111111111',
            first_name='B', last_name='User', status='ACTIVE',
        )
        with self.assertRaises(IdentityConflictError):
            LeadConversionService.resolve_or_create_member_identity(
                lead=self.lead, branch=self.branch, db_alias=DB,
            )

    # ------------------------------------------------------------------
    # Full Conversion Execute Tests
    # ------------------------------------------------------------------

    def test_execute_conversion_happy_path(self):
        """Full conversion: order PAID, membership ACTIVE, lead CONVERTED."""
        result = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            db_alias=DB,
        )
        # Check result structure
        self.assertIn('conversion_id', result)
        self.assertIn('order_id', result)
        self.assertIn('membership_id', result)
        self.assertIn('order_number', result)
        self.assertEqual(result['lead_status'], 'CONVERTED')

        # Lead status updated
        self.lead.refresh_from_db(using=DB)
        self.assertEqual(self.lead.current_status, 'CONVERTED')

        # LeadConversion record created
        conversion = LeadConversion.objects.using(DB).get(id=result['conversion_id'])
        self.assertEqual(str(conversion.lead.id), str(self.lead.id))
        self.assertIsNotNone(conversion.order_id)
        self.assertIsNotNone(conversion.membership_id)

        # Membership is active
        membership = Membership.objects.using(DB).get(id=result['membership_id'])
        self.assertEqual(membership.status, 'ACTIVE')

    def test_execute_conversion_already_converted_raises(self):
        """Converting an already-CONVERTED lead raises LeadAlreadyConvertedError."""
        self.lead.current_status = 'CONVERTED'
        self.lead.save(using=DB)
        with self.assertRaises(LeadAlreadyConvertedError):
            LeadConversionService.execute_conversion(
                lead=self.lead,
                package_version_id=str(self.pkg_version.id),
                branch_id=str(self.branch.id),
                payment_provider='CASH',
                payment_amount=Decimal('5000.00'),
                actor_user=self.admin,
                db_alias=DB,
            )

    def test_execute_conversion_short_payment_raises(self):
        """Payment amount below quoted total raises ValidationError."""
        with self.assertRaises(ValidationError):
            LeadConversionService.execute_conversion(
                lead=self.lead,
                package_version_id=str(self.pkg_version.id),
                branch_id=str(self.branch.id),
                payment_provider='CASH',
                payment_amount=Decimal('100.00'),  # Way below 5000
                actor_user=self.admin,
                db_alias=DB,
            )

    def test_execute_conversion_idempotency_replay(self):
        """Calling execute_conversion twice with same idempotency_key returns cached result."""
        idem_key = f'test-idem-{uuid.uuid4().hex[:8]}'
        result1 = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            idempotency_key=idem_key,
            db_alias=DB,
        )
        # Reload lead (now CONVERTED) — second call should replay
        self.lead.refresh_from_db(using=DB)
        result2 = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            idempotency_key=idem_key,
            db_alias=DB,
        )
        # Both results should return the same conversion_id
        self.assertEqual(result1['conversion_id'], result2['conversion_id'])
        # Only one LeadConversion record created
        count = LeadConversion.objects.using(DB).filter(lead=self.lead).count()
        self.assertEqual(count, 1)

    def test_execute_conversion_identity_created_flag(self):
        """identity_created=True when no matching user existed."""
        result = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            db_alias=DB,
        )
        self.assertTrue(result['identity_created'])

    # ------------------------------------------------------------------
    # API Endpoint Tests
    # ------------------------------------------------------------------

    def test_api_conversion_eligibility(self):
        """GET conversion-eligibility returns eligible=True for fresh lead."""
        url = f'/api/v1/tenant/leads/{self.lead.id}/conversion-eligibility/'
        response = self.client.get(url, HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(data['eligible'])

    def test_api_conversion_quote(self):
        """POST conversion-quote returns pricing breakdown."""
        url = f'/api/v1/tenant/leads/{self.lead.id}/conversion-quote/'
        payload = {
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
        }
        response = self.client.post(url, data=payload, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn('pricing', data)
        self.assertEqual(data['pricing']['total_payable'], '5000.00')
        self.assertIn('entitlements', data)

    def test_api_convert_happy_path(self):
        """POST convert returns 201 with full conversion result."""
        url = f'/api/v1/tenant/leads/{self.lead.id}/convert/'
        payload = {
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'CASH',
            'payment_amount': '5000.00',
        }
        response = self.client.post(url, data=payload, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertIn('conversion_id', data)
        self.assertIn('membership_id', data)
        self.assertIn('order_id', data)
        self.assertEqual(data['lead_status'], 'CONVERTED')

        # Lead is now CONVERTED in DB
        self.lead.refresh_from_db(using=DB)
        self.assertEqual(self.lead.current_status, 'CONVERTED')

    def test_api_convert_already_converted_returns_409(self):
        """POST convert on CONVERTED lead returns 409 with ALREADY_CONVERTED code."""
        self.lead.current_status = 'CONVERTED'
        self.lead.save(using=DB)
        url = f'/api/v1/tenant/leads/{self.lead.id}/convert/'
        payload = {
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'CASH',
            'payment_amount': '5000.00',
        }
        response = self.client.post(url, data=payload, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.json().get('code'), 'ALREADY_CONVERTED')

    def test_api_convert_missing_fields_returns_400(self):
        """POST convert with missing required fields returns 400."""
        url = f'/api/v1/tenant/leads/{self.lead.id}/convert/'
        response = self.client.post(url, data={}, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_convert_short_payment_returns_400(self):
        """POST convert with payment below total returns 400."""
        url = f'/api/v1/tenant/leads/{self.lead.id}/convert/'
        payload = {
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'CASH',
            'payment_amount': '10.00',
        }
        response = self.client.post(url, data=payload, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_convert_invalid_provider_returns_400(self):
        """POST convert with invalid payment_provider returns 400."""
        url = f'/api/v1/tenant/leads/{self.lead.id}/convert/'
        payload = {
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'BITCOIN',
            'payment_amount': '5000.00',
        }
        response = self.client.post(url, data=payload, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ------------------------------------------------------------------
    # Audit Checklist Closeout Tests (Items 1 - 32)
    # ------------------------------------------------------------------

    def test_canonical_lead_converted_status_value(self):
        """Canonical converted status must be CONVERTED in model choices."""
        status_dict = dict(Lead.STATUSES)
        self.assertIn('CONVERTED', status_dict)
        self.assertEqual(status_dict['CONVERTED'], 'Converted')

    def test_manual_status_bypass_blocked_in_transition(self):
        """Direct transition_lead_status to CONVERTED is blocked without commercial conversion."""
        with self.assertRaises(ValidationError) as ctx:
            CRMLeadService.transition_lead_status(
                lead=self.lead,
                new_status='CONVERTED',
                reason_code='MANUAL_OVERRIDE',
                actor_user=self.admin,
                db_alias=DB,
            )
        self.assertIn("Manual transition to CONVERTED is forbidden", str(ctx.exception))

    def test_manual_status_bypass_blocked_in_update_lead(self):
        """Direct update_lead to CONVERTED is blocked without commercial conversion."""
        with self.assertRaises(ValidationError) as ctx:
            CRMLeadService.update_lead(
                lead=self.lead,
                data={'current_status': 'CONVERTED'},
                actor_user=self.admin,
                db_alias=DB,
            )
        self.assertIn("Manual update to CONVERTED is forbidden", str(ctx.exception))

    def test_package_version_lifecycle_and_effective_dates(self):
        """PackageVersion must be ACTIVE and within effective dates."""
        future_dt = timezone.now() + timezone.timedelta(days=10)
        self.pkg_version.effective_from = future_dt
        self.pkg_version.save(using=DB)

        with self.assertRaises(ValidationError) as ctx:
            LeadConversionService._resolve_purchasable_version(str(self.pkg_version.id), self.branch, DB)
        self.assertIn("not yet effective", str(ctx.exception))

        # Reset effective_from, test expired
        self.pkg_version.effective_from = timezone.now() - timezone.timedelta(days=60)
        self.pkg_version.effective_until = timezone.now() - timezone.timedelta(days=1)
        self.pkg_version.save(using=DB)

        with self.assertRaises(ValidationError) as ctx:
            LeadConversionService._resolve_purchasable_version(str(self.pkg_version.id), self.branch, DB)
        self.assertIn("has expired", str(ctx.exception))

        # Restore
        self.pkg_version.effective_until = None
        self.pkg_version.save(using=DB)

    def test_package_branch_availability_enforcement(self):
        """PackageBranchAvailability restricts sale to enabled branches only."""
        loc2 = Location.objects.using(DB).create(
            organization=self.org, code='P8-LOC2', name='Second Loc', status='ACTIVE',
        )
        branch2 = Branch.objects.using(DB).create(
            organization=self.org, location=loc2, code='BR-P8-2', name='Branch 2', status='ACTIVE',
        )

        # 1. Enable package exclusively for branch2
        PackageBranchAvailability.objects.using(DB).create(
            package=self.package, branch=branch2, status='ENABLED',
        )

        # Now self.branch has no enabled record while package has branch restrictions
        with self.assertRaises(ValidationError) as ctx:
            LeadConversionService._resolve_purchasable_version(str(self.pkg_version.id), self.branch, DB)
        self.assertIn("not available for sale at branch", str(ctx.exception))

        # 2. Add disabled record for self.branch
        avail_br1 = PackageBranchAvailability.objects.using(DB).create(
            package=self.package, branch=self.branch, status='DISABLED',
        )
        with self.assertRaises(ValidationError) as ctx:
            LeadConversionService._resolve_purchasable_version(str(self.pkg_version.id), self.branch, DB)
        self.assertIn("not available for sale at branch", str(ctx.exception))

        # 3. Clean up so other tests remain clean
        PackageBranchAvailability.objects.using(DB).filter(package=self.package).delete()

    def test_coupon_preview_no_redemption_write_and_order_discount_integrity(self):
        """Quote with coupon writes ZERO DiscountRedemption rows; execute writes exactly 1."""
        campaign = DiscountCampaign.objects.using(DB).create(
            organization=self.org,
            name='Conversion Special',
            discount_type='PERCENTAGE',
            discount_value=Decimal('10.00'),
            valid_from=timezone.now() - timezone.timedelta(days=1),
            status='ACTIVE',
        )
        code = DiscountCode.objects.using(DB).create(
            campaign=campaign,
            code='SAVE10P8',
            status='ACTIVE',
        )

        # 1. Quote preview - must NOT create DiscountRedemption
        redemptions_before = DiscountRedemption.objects.using(DB).count()
        quote = LeadConversionService.get_conversion_quote(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            coupon_code='SAVE10P8',
            db_alias=DB,
        )
        redemptions_after_quote = DiscountRedemption.objects.using(DB).count()
        self.assertEqual(redemptions_before, redemptions_after_quote)
        self.assertEqual(redemptions_after_quote, 0)

        # Quote pricing check: 5000 - 500 = 4500
        self.assertEqual(quote['pricing']['discount_amount'], '500.00')
        self.assertEqual(quote['pricing']['total_payable'], '4500.00')

        # 2. Execute conversion with coupon - writes exactly 1 redemption
        res = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('4500.00'),
            coupon_code='SAVE10P8',
            actor_user=self.admin,
            db_alias=DB,
        )
        self.assertEqual(DiscountRedemption.objects.using(DB).count(), 1)
        redemption = DiscountRedemption.objects.using(DB).first()
        self.assertEqual(redemption.discount_amount, Decimal('500.00'))

        # Check Order discount integrity
        order = Order.objects.using(DB).get(id=res['order_id'])
        self.assertEqual(order.subtotal, Decimal('5000.00'))
        self.assertEqual(order.discount_amount, Decimal('500.00'))
        self.assertEqual(order.total_amount, Decimal('4500.00'))
        self.assertEqual(order.status, 'PAID')

    def test_invoice_lifecycle_and_contract_snapshot(self):
        """Invoice is issued on payment success; ContractSnapshot captures immutable terms."""
        res = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            db_alias=DB,
        )
        # 1. Invoice created at payment success
        self.assertIsNotNone(res['invoice_id'])
        invoice = MemberInvoice.objects.using(DB).get(id=res['invoice_id'])
        self.assertEqual(invoice.status, 'PAID')
        self.assertEqual(invoice.total_amount, Decimal('5000.00'))

        # 2. MembershipContractSnapshot captures exact commercial terms
        snapshot = MembershipContractSnapshot.objects.using(DB).get(membership_id=res['membership_id'])
        self.assertEqual(snapshot.package_id, self.package.id)
        self.assertEqual(snapshot.package_version_id, self.pkg_version.id)
        self.assertEqual(snapshot.purchase_price, Decimal('5000.00'))
        self.assertEqual(snapshot.purchase_branch_id, self.branch.id)
        self.assertTrue(len(snapshot.entitlements_snapshot) > 0)

    def test_package_version_freeze_no_silent_migration(self):
        """Publishing V2 does NOT alter existing Membership linked to V1."""
        res = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            db_alias=DB,
        )
        membership = Membership.objects.using(DB).get(id=res['membership_id'])
        self.assertEqual(membership.package_version_id, self.pkg_version.id)

        # Create & publish V2
        pkg_v2 = PackageVersion.objects.using(DB).create(
            package=self.package,
            version_number=2,
            name_snapshot='Phase8 Gold v2',
            duration_value=60,
            duration_unit='DAY',
            total_days=60,
            status='ACTIVE',
            effective_from=timezone.now(),
            created_by_user=self.admin,
        )

        membership.refresh_from_db(using=DB)
        self.assertEqual(membership.package_version_id, self.pkg_version.id)
        self.assertNotEqual(membership.package_version_id, pkg_v2.id)

    def test_membership_entitlements_provisioned_accurately(self):
        """MembershipEntitlement rows match PackageEntitlementDefinition with exact allocated units."""
        res = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            db_alias=DB,
        )
        ents = MembershipEntitlement.objects.using(DB).filter(membership_id=res['membership_id'])
        self.assertEqual(ents.count(), 1)
        ent = ents.first()
        self.assertEqual(ent.entitlement_type, 'CLASS_SESSIONS')
        self.assertEqual(ent.allocated_units, Decimal('20.00'))
        self.assertEqual(ent.consumed_units, Decimal('0.00'))

        # Ledger row
        ledgers = MembershipEntitlementLedger.objects.using(DB).filter(membership_entitlement=ent)
        self.assertEqual(ledgers.count(), 1)
        self.assertEqual(ledgers.first().units, Decimal('20.00'))

    def test_identity_onboarding_unusable_password(self):
        """Newly created member identity has unusable password and status INVITED."""
        profile, created = LeadConversionService.resolve_or_create_member_identity(
            lead=self.lead,
            branch=self.branch,
            actor_user=self.admin,
            db_alias=DB,
        )
        self.assertTrue(created)
        self.assertEqual(profile.user.status, 'INVITED')
        self.assertEqual(profile.user.password_hash, '!unusable')

    def test_existing_member_reuse_no_duplicate(self):
        """Existing TenantUser with matching phone is reused without duplicate creation."""
        existing_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='member.prior@phase8.test',
            phone=self.lead.phone_normalized,
            first_name='Priya',
            last_name='Kapoor',
            status='ACTIVE',
            is_login_allowed=True,
        )
        profile_orig = UserProfile.objects.using(DB).create(
            user=existing_user,
            preferred_branch=self.branch,
            joining_date=date(2025, 1, 1),
            member_type='MEMBER',
            member_status='ACTIVE',
        )

        users_before = TenantUser.objects.using(DB).count()
        profiles_before = UserProfile.objects.using(DB).count()

        resolved_profile, created = LeadConversionService.resolve_or_create_member_identity(
            lead=self.lead,
            branch=self.branch,
            actor_user=self.admin,
            db_alias=DB,
        )
        self.assertFalse(created)
        self.assertEqual(resolved_profile.id, profile_orig.id)
        self.assertEqual(TenantUser.objects.using(DB).count(), users_before)
        self.assertEqual(UserProfile.objects.using(DB).count(), profiles_before)

    def test_post_payment_recovery_flow(self):
        """If order is already paid from prior interrupted attempt, conversion recovers and activates membership."""
        # Setup an existing paid order for this lead
        profile, _ = LeadConversionService.resolve_or_create_member_identity(
            lead=self.lead, branch=self.branch, actor_user=self.admin, db_alias=DB,
        )
        from apps.tenant_core.services_commerce import CommerceService
        order = CommerceService.create_order(
            branch=self.branch,
            items_data=[{
                'item_type': 'PACKAGE',
                'package_id': str(self.package.id),
                'package_version_id': str(self.pkg_version.id),
                'package_price_id': str(self.pkg_price.id),
                'item_name_snapshot': 'Phase8 Gold v1',
                'quantity': '1.00',
                'unit_price': '5000.00',
                'tax_percent': '0.000',
                'discount_amount': '0.00',
            }],
            user_profile=profile,
            lead=self.lead,
            sold_by=self.admin,
            order_type='NEW_MEMBERSHIP',
            source='SALES',
            created_by=self.admin,
            db_alias=DB,
        )
        CommerceService.record_payment(
            order_id=str(order.id),
            amount=Decimal('5000.00'),
            provider='CASH',
            db_alias=DB,
        )
        order.refresh_from_db(using=DB)
        self.assertEqual(order.status, 'PAID')

        orders_count = Order.objects.using(DB).filter(lead=self.lead).count()
        self.assertEqual(orders_count, 1)

        # Now call conversion service - it must reuse existing paid order and finalize
        res = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            db_alias=DB,
        )
        self.assertEqual(res['order_id'], str(order.id))
        self.assertEqual(res['lead_status'], 'CONVERTED')
        # Proves no second order was created
        self.assertEqual(Order.objects.using(DB).filter(lead=self.lead).count(), 1)
        self.assertEqual(Membership.objects.using(DB).filter(source_order=order).count(), 1)

    def test_audit_and_outbox_events_emitted(self):
        """Conversion transaction emits canonical BusinessAuditEvent and DomainOutboxEvent."""
        audits_before = BusinessAuditEvent.objects.using(DB).count()
        outbox_before = DomainOutboxEvent.objects.using(DB).count()

        res = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            db_alias=DB,
        )

        audits = BusinessAuditEvent.objects.using(DB).filter(action_code='LEAD_CONVERTED')
        self.assertTrue(audits.exists())
        self.assertEqual(str(audits.first().entity_id), res['conversion_id'])

        outbox_events = DomainOutboxEvent.objects.using(DB).filter(event_type='crm.lead.converted')
        self.assertTrue(outbox_events.exists())
        self.assertEqual(str(outbox_events.first().aggregate_id), res['conversion_id'])

    def test_attention_engine_compatibility_removes_lead_from_attention(self):
        """Lead in CONVERTED state returns needs_attention=False and disappears from Attention Queue."""
        # Before conversion: lead is fresh NEW_LEAD
        att_before = LeadAttentionService.evaluate_lead(self.lead, db_alias=DB)
        # Should not be in terminal stage
        self.assertNotIn(self.lead.current_status, ['CONVERTED', 'NOT_INTERESTED', 'LOST'])

        # Execute conversion
        LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            db_alias=DB,
        )
        self.lead.refresh_from_db(using=DB)
        self.assertEqual(self.lead.current_status, 'CONVERTED')

        att_after = LeadAttentionService.evaluate_lead(self.lead, db_alias=DB)
        self.assertFalse(att_after['is_stuck'])
        self.assertEqual(len(att_after['reasons']), 0)

    def test_unauthorized_user_lacking_convert_permission_forbidden(self):
        """User lacking crm.leads.convert permission receives 403 on convert endpoint."""
        unauth_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='unauth@phase8.test',
            first_name='Unauth',
            last_name='User',
            status='ACTIVE',
            is_login_allowed=True,
        )
        refresh = _build_tenant_token(user=unauth_user, tenant=self.tenant, db_alias=DB)
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        url = f'/api/v1/tenant/leads/{self.lead.id}/convert/'
        payload = {
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'CASH',
            'payment_amount': '5000.00',
        }
        res = self.client.post(url, data=payload, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------------
    # Architectural Hardening: Payment Authority Separation
    # ------------------------------------------------------------------

    def test_user_with_convert_permission_without_payment_permission_cannot_record_payment(self):
        """User with crm.leads.convert but without finance.payments.create cannot record payment."""
        sales_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='sales.rep@phase8.test',
            first_name='Sales',
            last_name='Rep',
            status='ACTIVE',
            is_login_allowed=True,
        )
        mod_cat = ModuleCatalog.objects.using(DB).get(module_code='crm')
        sub_cat = SubmoduleCatalog.objects.using(DB).get(module=mod_cat, submodule_code='leads')
        perm_convert = Permission.objects.using(DB).get(permission_code='crm.leads.convert')
        perm_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_cat, submodule=sub_cat, action='view',
            defaults={'permission_code': 'crm.leads.view', 'label': 'View Leads'}
        )

        role = Role.objects.using(DB).create(
            organization=self.org, name='Sales Rep Role', code='SALES_REP_P8',
            is_system_role=True, scope='ORG', is_active=True,
        )
        RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod_cat, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub_cat, defaults={'can_access': True})
        pset = RolePermissionSet.objects.using(DB).create(role=role, name='Sales Rep Perms')
        for perm in [perm_view, perm_convert]:
            RolePermissionSetItem.objects.using(DB).get_or_create(
                permission_set=pset, permission=perm, defaults={'granted': True}
            )
        RoleAssignment.objects.using(DB).create(
            user=sales_user, role=role, organization=self.org, is_active=True
        )

        refresh = _build_tenant_token(user=sales_user, tenant=self.tenant, db_alias=DB)
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        url = f'/api/v1/tenant/leads/{self.lead.id}/convert/'
        payload = {
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'CASH',
            'payment_amount': '5000.00',
        }
        res = self.client.post(url, data=payload, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json().get('code'), 'PAYMENT_AUTHORITY_REQUIRED')

    def test_user_without_payment_permission_can_prepare_conversion_quote(self):
        """User with crm.leads.convert can prepare conversion quote and check eligibility without payment authority."""
        sales_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='sales.prep@phase8.test',
            first_name='SalesPrep',
            last_name='User',
            status='ACTIVE',
            is_login_allowed=True,
        )
        mod_cat = ModuleCatalog.objects.using(DB).get(module_code='crm')
        sub_cat = SubmoduleCatalog.objects.using(DB).get(module=mod_cat, submodule_code='leads')
        perm_convert = Permission.objects.using(DB).get(permission_code='crm.leads.convert')
        perm_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_cat, submodule=sub_cat, action='view',
            defaults={'permission_code': 'crm.leads.view', 'label': 'View Leads'}
        )

        role = Role.objects.using(DB).create(
            organization=self.org, name='Sales Prep Role', code='SALES_PREP_P8',
            is_system_role=True, scope='ORG', is_active=True,
        )
        RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod_cat, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub_cat, defaults={'can_access': True})
        pset = RolePermissionSet.objects.using(DB).create(role=role, name='Sales Prep Perms')
        for perm in [perm_view, perm_convert]:
            RolePermissionSetItem.objects.using(DB).get_or_create(
                permission_set=pset, permission=perm, defaults={'granted': True}
            )
        RoleAssignment.objects.using(DB).create(
            user=sales_user, role=role, organization=self.org, is_active=True
        )

        refresh = _build_tenant_token(user=sales_user, tenant=self.tenant, db_alias=DB)
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # 1. Eligibility check succeeds
        elig_url = f'/api/v1/tenant/leads/{self.lead.id}/conversion-eligibility/'
        elig_res = self.client.get(elig_url, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(elig_res.status_code, status.HTTP_200_OK)
        self.assertTrue(elig_res.json()['eligible'])

        # 2. Conversion quote succeeds
        quote_url = f'/api/v1/tenant/leads/{self.lead.id}/conversion-quote/'
        quote_res = self.client.post(quote_url, data={
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
        }, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(quote_res.status_code, status.HTTP_200_OK)
        self.assertIn('pricing', quote_res.json())

    def test_user_with_payment_permission_can_record_valid_offline_payment(self):
        """User with both crm.leads.convert and finance.payments.create can successfully convert."""
        auth_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='authorized.sales@phase8.test',
            first_name='Authorized',
            last_name='Sales',
            status='ACTIVE',
            is_login_allowed=True,
        )
        mod_crm = ModuleCatalog.objects.using(DB).get(module_code='crm')
        sub_leads = SubmoduleCatalog.objects.using(DB).get(module=mod_crm, submodule_code='leads')
        perm_convert = Permission.objects.using(DB).get(permission_code='crm.leads.convert')
        mod_fin = ModuleCatalog.objects.using(DB).get(module_code='finance')
        sub_pay = SubmoduleCatalog.objects.using(DB).get(module=mod_fin, submodule_code='payments')
        perm_payment = Permission.objects.using(DB).get(permission_code='finance.payments.create')
        perm_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_crm, submodule=sub_leads, action='view',
            defaults={'permission_code': 'crm.leads.view', 'label': 'View Leads'}
        )

        role = Role.objects.using(DB).create(
            organization=self.org, name='Full Sales & Cashier', code='FULL_SALES_CASHIER',
            is_system_role=True, scope='ORG', is_active=True,
        )
        RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod_crm, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub_leads, defaults={'can_access': True})
        RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod_fin, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub_pay, defaults={'can_access': True})
        pset = RolePermissionSet.objects.using(DB).create(role=role, name='Full Cashier Perms')
        for perm in [perm_view, perm_convert, perm_payment]:
            RolePermissionSetItem.objects.using(DB).get_or_create(
                permission_set=pset, permission=perm, defaults={'granted': True}
            )
        RoleAssignment.objects.using(DB).create(
            user=auth_user, role=role, organization=self.org, is_active=True
        )

        refresh = _build_tenant_token(user=auth_user, tenant=self.tenant, db_alias=DB)
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='Cash', last_name='Buyer', phone_normalized='+919876540001',
            email_normalized='cash.buyer@phase8.test', current_status='TRIAL_COMPLETED',
        )

        url = f'/api/v1/tenant/leads/{lead.id}/convert/'
        payload = {
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'CASH',
            'payment_amount': '5000.00',
        }
        res = self.client.post(url, data=payload, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.json()['lead_status'], 'CONVERTED')

    def test_lead_edit_permission_alone_cannot_record_payment(self):
        """User with only crm.leads.edit cannot convert or record payment."""
        editor_user = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='editor.only@phase8.test',
            first_name='Editor',
            last_name='Only',
            status='ACTIVE',
            is_login_allowed=True,
        )
        mod_crm = ModuleCatalog.objects.using(DB).get(module_code='crm')
        sub_leads = SubmoduleCatalog.objects.using(DB).get(module=mod_crm, submodule_code='leads')
        perm_edit = Permission.objects.using(DB).get(permission_code='crm.leads.edit')
        perm_view, _ = Permission.objects.using(DB).get_or_create(
            module=mod_crm, submodule=sub_leads, action='view',
            defaults={'permission_code': 'crm.leads.view', 'label': 'View Leads'}
        )

        role = Role.objects.using(DB).create(
            organization=self.org, name='Editor Only', code='EDITOR_ONLY_P8',
            is_system_role=True, scope='ORG', is_active=True,
        )
        RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod_crm, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub_leads, defaults={'can_access': True})
        pset = RolePermissionSet.objects.using(DB).create(role=role, name='Editor Perms')
        for perm in [perm_view, perm_edit]:
            RolePermissionSetItem.objects.using(DB).get_or_create(
                permission_set=pset, permission=perm, defaults={'granted': True}
            )
        RoleAssignment.objects.using(DB).create(
            user=editor_user, role=role, organization=self.org, is_active=True
        )

        refresh = _build_tenant_token(user=editor_user, tenant=self.tenant, db_alias=DB)
        token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        url = f'/api/v1/tenant/leads/{self.lead.id}/convert/'
        payload = {
            'package_version_id': str(self.pkg_version.id),
            'branch_id': str(self.branch.id),
            'payment_provider': 'CASH',
            'payment_amount': '5000.00',
        }
        res = self.client.post(url, data=payload, format='json', HTTP_X_TENANT_DB='tenant_test')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_direct_service_bypass_rejected_without_payment_permission(self):
        """Calling LeadConversionService.execute_conversion with unauthorized actor raises PermissionDenied."""
        from rest_framework.exceptions import PermissionDenied
        unauth_actor = TenantUser.objects.using(DB).create(
            organization=self.org, email='direct.unauth@phase8.test',
            first_name='Direct', last_name='Unauth', status='ACTIVE',
        )
        lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='Direct', last_name='Lead', phone_normalized='+919876540002',
            email_normalized='direct.lead@phase8.test', current_status='NEW_LEAD',
        )
        with self.assertRaises(PermissionDenied):
            LeadConversionService.execute_conversion(
                lead=lead,
                package_version_id=str(self.pkg_version.id),
                branch_id=str(self.branch.id),
                payment_provider='CASH',
                payment_amount=Decimal('5000.00'),
                actor_user=unauth_actor,
                db_alias=DB,
            )

    # ------------------------------------------------------------------
    # Architectural Hardening: Attempt-Scoped Idempotency & Conflict Guard
    # ------------------------------------------------------------------

    def test_idempotency_same_attempt_replay_preserves_single_order_and_membership(self):
        """Same idempotency_key with identical payload safely replays cached result with 0 duplicate orders."""
        lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='Idem', last_name='Test', phone_normalized='+919876540003',
            email_normalized='idem.test@phase8.test', current_status='TRIAL_COMPLETED',
        )
        attempt_key = f"attempt-{uuid.uuid4().hex[:8]}"

        res1 = LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            idempotency_key=attempt_key,
            db_alias=DB,
        )

        orders_count1 = Order.objects.using(DB).filter(lead=lead).count()
        memberships_count1 = Membership.objects.using(DB).filter(source_order__lead=lead).count()
        conversions_count1 = LeadConversion.objects.using(DB).filter(lead=lead).count()

        self.assertEqual(orders_count1, 1)
        self.assertEqual(memberships_count1, 1)
        self.assertEqual(conversions_count1, 1)

        # Retry with the exact same idempotency_key
        res2 = LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            idempotency_key=attempt_key,
            db_alias=DB,
        )

        self.assertEqual(res2['conversion_id'], res1['conversion_id'])
        self.assertEqual(res2['order_id'], res1['order_id'])
        self.assertEqual(res2['membership_id'], res1['membership_id'])

        # Assert zero duplicate rows created
        self.assertEqual(Order.objects.using(DB).filter(lead=lead).count(), 1)
        self.assertEqual(Membership.objects.using(DB).filter(source_order__lead=lead).count(), 1)
        self.assertEqual(LeadConversion.objects.using(DB).filter(lead=lead).count(), 1)

    def test_idempotency_payload_mismatch_package_version_rejected(self):
        """Same idempotency_key with different PackageVersion is rejected as idempotency conflict."""
        from apps.tenant_core.services_reliability import IdempotencyConflictError

        lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='Mismatch', last_name='Pkg', phone_normalized='+919876540004',
            email_normalized='mismatch.pkg@phase8.test', current_status='NEW_LEAD',
        )
        pkg_v2 = PackageVersion.objects.using(DB).create(
            package=self.package, version_number=2, name_snapshot='Phase8 Gold v2',
            duration_value=30, duration_unit='DAY', total_days=30, status='ACTIVE',
            effective_from=timezone.now() - timezone.timedelta(days=1),
            created_by_user=self.admin,
        )
        PackagePrice.objects.using(DB).create(
            package_version=pkg_v2, branch=self.branch, currency='INR', base_price=Decimal('5000.00'),
            tax_percent=Decimal('0.000'), prices_include_tax=True, status='ACTIVE',
            effective_from=timezone.now() - timezone.timedelta(days=1), created_by_user=self.admin,
        )

        attempt_key = f"attempt-pkg-{uuid.uuid4().hex[:8]}"

        # Attempt 1: package_version v1
        LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            idempotency_key=attempt_key,
            db_alias=DB,
        )

        # Attempt 2: same key, but different package_version v2
        with self.assertRaises(IdempotencyConflictError):
            LeadConversionService.execute_conversion(
                lead=lead,
                package_version_id=str(pkg_v2.id),
                branch_id=str(self.branch.id),
                payment_provider='CASH',
                payment_amount=Decimal('5000.00'),
                actor_user=self.admin,
                idempotency_key=attempt_key,
                db_alias=DB,
            )

    def test_idempotency_payload_mismatch_amount_rejected(self):
        """Same idempotency_key with different payment amount is rejected as idempotency conflict."""
        from apps.tenant_core.services_reliability import IdempotencyConflictError

        lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='Mismatch', last_name='Amt', phone_normalized='+919876540005',
            email_normalized='mismatch.amt@phase8.test', current_status='NEW_LEAD',
        )
        attempt_key = f"attempt-amt-{uuid.uuid4().hex[:8]}"

        LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            idempotency_key=attempt_key,
            db_alias=DB,
        )

        # Attempt 2: same key, different amount
        with self.assertRaises(IdempotencyConflictError):
            LeadConversionService.execute_conversion(
                lead=lead,
                package_version_id=str(self.pkg_version.id),
                branch_id=str(self.branch.id),
                payment_provider='CASH',
                payment_amount=Decimal('6000.00'),
                actor_user=self.admin,
                idempotency_key=attempt_key,
                db_alias=DB,
            )

    def test_idempotency_new_attempt_key_not_blocked_by_prior_record(self):
        """New legitimate attempt key creates distinct IdempotencyRecord and is not blocked by old record."""
        lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='NewKey', last_name='Lead', phone_normalized='+919876540006',
            email_normalized='newkey.lead@phase8.test', current_status='NEW_LEAD',
        )
        attempt1 = f"key1-{uuid.uuid4().hex[:8]}"
        attempt2 = f"key2-{uuid.uuid4().hex[:8]}"

        res1 = LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(self.pkg_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('5000.00'),
            actor_user=self.admin,
            idempotency_key=attempt1,
            db_alias=DB,
        )
        self.assertIsNotNone(res1['conversion_id'])

        # Verify attempt1 has an IdempotencyRecord
        from apps.tenant_core.models_audit_outbox import IdempotencyRecord
        rec1 = IdempotencyRecord.objects.using(DB).filter(
            idempotency_key=f"lead_conversion:{lead.id}:{attempt1}"
        ).first()
        self.assertIsNotNone(rec1)
        self.assertEqual(rec1.status, 'COMPLETED')

        # When attempt2 is presented for the same lead, verify attempt2 is evaluated as fresh
        # (It will encounter lead.current_status == 'CONVERTED' business rule, NOT an idempotency collision!)
        with self.assertRaises(LeadAlreadyConvertedError):
            LeadConversionService.execute_conversion(
                lead=lead,
                package_version_id=str(self.pkg_version.id),
                branch_id=str(self.branch.id),
                payment_provider='CASH',
                payment_amount=Decimal('5000.00'),
                actor_user=self.admin,
                idempotency_key=attempt2,
                db_alias=DB,
            )


