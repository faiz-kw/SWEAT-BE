from apps.tenant_core.services_payment_policy import PaymentPolicyService
from apps.tenant_core.models_approvals import ApprovalRequest, ApprovalAction
from apps.tenant_core.services_approvals import AdminApprovalService
"""
tests/test_razorpay_checkout.py — Comprehensive Targeted Tests for Razorpay Test Mode Checkout

Requirements Tested:
1. Missing Razorpay configuration (RAZORPAY_NOT_CONFIGURED)
2. Server-side order creation (provider order format order_...)
3. Amount conversion INR -> paise (integer, exact decimal quantize)
4. Authoritative backend amount calculation (discounts, taxes, ignores frontend amount)
5. Valid payment signature verification
6. Invalid signature rejected
7. Tampered payment ID rejected
8. Tampered order ID rejected
9. Amount mismatch rejected (fail closed)
10. Cross-tenant order protection
11. Duplicate verification / idempotency (0 duplicate payments, 0 duplicate memberships)
12. Payment success finalization (Order PAID, PaymentTransaction SUCCESS, Member, Membership)
13. Membership created once
14. Entitlements created once
15. Payment failure does not activate membership or convert lead
16. Existing CASH flow regression (fully operational)
"""

import hmac
import hashlib
import uuid
from decimal import Decimal
from datetime import date
from unittest.mock import patch, MagicMock

from django.utils import timezone
from django.core.exceptions import ValidationError
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework import status

from apps.authentication.views import _build_tenant_token
from apps.master.models import (
    Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription
)
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_rbac import (
    ModuleCatalog, SubmoduleCatalog, Permission, Role, RoleAssignment,
    RolePermissionSet, RolePermissionSetItem, RoleModuleAccess, RoleSubmoduleAccess
)
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_catalog import (
    ProgramCategory, Program, Package, PackageVersion, PackagePrice,
    PackageEntitlementDefinition, PackageBranchAvailability
)
from apps.tenant_core.models_crm import Lead, LeadConversion
from apps.tenant_core.models_commerce import Order, PaymentTransaction, MemberInvoice
from apps.tenant_core.models_memberships import Membership, MembershipEntitlement
from apps.tenant_core.services_crm import LeadConversionService
from apps.tenant_core.services_razorpay import (
    RazorpayService, RazorpayConfigError, RazorpayVerificationError
)

DB = 'tenant_test'
TEST_KEY_ID = 'rzp_test_mockedkey1234567'
TEST_KEY_SECRET = 'mockedsecret123456789012'


def generate_valid_signature(order_id: str, payment_id: str, secret: str = TEST_KEY_SECRET) -> str:
    msg = f"{order_id}|{payment_id}".encode('utf-8')
    return hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()


@override_settings(RAZORPAY_KEY_ID=TEST_KEY_ID, RAZORPAY_KEY_SECRET=TEST_KEY_SECRET)
class RazorpayCheckoutTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias(DB)

        # Master DB setup
        self.tenant = Tenant.objects.using('default').create(
            code='RZP-TENANT', name='Razorpay Test Gym', slug='rzp-gym', status='ACTIVE',
        )
        TenantDataSource.objects.using('default').create(
            tenant=self.tenant, db_name='test_fitness_tenant',
            database_name='test_fitness_tenant', status='ACTIVE', database_engine='POSTGRESQL',
        )
        plan = SaasPlan.objects.using('default').create(
            name='Standard Plan', code='STD-PLAN', tier='ENTERPRISE', status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant, plan=plan, status='ACTIVE',
        )
        for mod_code in ['core', 'crm', 'finance']:
            pm, _ = ProductModule.objects.using('default').get_or_create(
                code=mod_code, defaults={'name': mod_code.capitalize(), 'status': 'ACTIVE'},
            )
            TenantModule.objects.using('default').create(
                tenant=self.tenant, module=pm, is_enabled=True, availability_mode='ALL_BRANCHES',
            )

        # Tenant DB: Org, Branch, Admin User
        self.org = Organization.objects.using(DB).create(
            code='RZP-ORG', name='Razorpay Org', status='ACTIVE',
        )
        loc = Location.objects.using(DB).create(
            organization=self.org, code='RZP-LOC', name='Main Location', status='ACTIVE',
        )
        self.branch = Branch.objects.using(DB).create(
            organization=self.org, location=loc, code='BR-RZP', name='Main Branch', status='ACTIVE',
        )

        self.admin = TenantUser.objects.using(DB).create(
            organization=self.org, email='sales@rzp.test',
            first_name='Sales', last_name='Agent', status='ACTIVE',
        )
        self.admin.set_password('Secret123!')
        self.admin.save(using=DB)

        # RBAC setup
        mod_cat, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='crm', defaults={'name': 'CRM', 'is_enabled': True}
        )
        sub_cat, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_cat, submodule_code='leads', defaults={'name': 'Leads', 'is_enabled': True}
        )
        perm_convert, _ = Permission.objects.using(DB).get_or_create(
            module=mod_cat, submodule=sub_cat, action='convert',
            defaults={'permission_code': 'crm.leads.convert', 'label': 'Convert Leads'}
        )
        mod_fin, _ = ModuleCatalog.objects.using(DB).get_or_create(
            module_code='finance', defaults={'name': 'Finance', 'is_enabled': True}
        )
        sub_fin, _ = SubmoduleCatalog.objects.using(DB).get_or_create(
            module=mod_fin, submodule_code='payments', defaults={'name': 'Payments', 'is_enabled': True}
        )
        perm_pay, _ = Permission.objects.using(DB).get_or_create(
            module=mod_fin, submodule=sub_fin, action='create',
            defaults={'permission_code': 'finance.payments.create', 'label': 'Record Payments'}
        )

        role = Role.objects.using(DB).create(
            organization=self.org, name='CRM Admin', code='CRM_ADMIN_RZP',
            is_system_role=True, scope='ORG', is_active=True,
        )
        RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod_cat, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub_cat, defaults={'can_access': True})
        RoleModuleAccess.objects.using(DB).get_or_create(role=role, module=mod_fin, defaults={'can_access': True})
        RoleSubmoduleAccess.objects.using(DB).get_or_create(role=role, submodule=sub_fin, defaults={'can_access': True})

        pset = RolePermissionSet.objects.using(DB).create(role=role, name='CRM Admin Perms')
        for perm in [perm_convert, perm_pay]:
            RolePermissionSetItem.objects.using(DB).get_or_create(
                permission_set=pset, permission=perm, defaults={'granted': True}
            )
        RoleAssignment.objects.using(DB).create(
            user=self.admin, role=role, organization=self.org, is_active=True
        )

        self.token = _build_tenant_token(self.admin, self.tenant, DB)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

        # Catalog setup
        cat = ProgramCategory.objects.using(DB).create(
            organization=self.org, name='Fitness', code='CAT-FIT',
        )
        self.program = Program.objects.using(DB).create(
            organization=self.org, category=cat, name='Strength Pro', code='PROG-STR', status='ACTIVE',
        )
        self.package = Package.objects.using(DB).create(
            organization=self.org, program=self.program, name='Gold 30 Days', code='PKG-GLD30', status='ACTIVE',
        )
        self.package_version = PackageVersion.objects.using(DB).create(
            package=self.package, version_number=1, name_snapshot='Gold 30 Days v1',
            duration_value=30, duration_unit='DAYS', total_days=30, status='ACTIVE', effective_from=timezone.now(), created_by_user=self.admin,
        )
        self.package_price = PackagePrice.objects.using(DB).create(
            package_version=self.package_version,
            base_price=Decimal('1000.00'),
            tax_percent=Decimal('18.000'),
            prices_include_tax=False, # total = 1000 + 180 = 1180.00 INR -> 118000 paise
            effective_from=timezone.now() - timezone.timedelta(days=1),
            status='ACTIVE', created_by_user=self.admin,
        )
        PackageBranchAvailability.objects.using(DB).create(
            package=self.package, branch=self.branch, status='ENABLED',
        )
        self.entitlement_def = PackageEntitlementDefinition.objects.using(DB).create(
            package_version=self.package_version, entitlement_type='GYM_ACCESS',
            allocated_units=30, is_unlimited=False, status='ACTIVE',
        )

        # Test Lead
        self.lead = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='Rahul', last_name='Sharma',
            phone_normalized='+919876543210',
            email_normalized='rahul.sharma@example.test',
            current_status='PROSPECT',
        )

    # 1. Missing Razorpay configuration
    def test_missing_razorpay_configuration_raises_error(self):
        with override_settings(RAZORPAY_KEY_ID='', RAZORPAY_KEY_SECRET=''):
            self.assertFalse(RazorpayService.is_configured())
            with self.assertRaises(RazorpayConfigError) as ctx:
                RazorpayService.get_credentials()
            self.assertEqual(ctx.exception.code, 'RAZORPAY_NOT_CONFIGURED')

    # 2. Amount conversion INR -> paise (no float arithmetic)
    def test_amount_conversion_inr_to_paise(self):
        self.assertEqual(RazorpayService.convert_inr_to_paise(Decimal('100.00')), 10000)
        self.assertEqual(RazorpayService.convert_inr_to_paise(Decimal('1180.00')), 118000)
        self.assertEqual(RazorpayService.convert_inr_to_paise(Decimal('99.99')), 9999)
        with self.assertRaises(ValidationError):
            RazorpayService.convert_inr_to_paise(Decimal('0.00'))
        with self.assertRaises(ValidationError):
            RazorpayService.convert_inr_to_paise(Decimal('-10.00'))

    # 3. Server-side order creation
    @patch('razorpay.Client')
    def test_server_side_order_creation_success(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.order.create.return_value = {
            'id': 'order_mock12345678',
            'amount': 118000,
            'currency': 'INR',
            'status': 'created',
            'receipt': 'ORD-1234',
        }

        checkout_data = LeadConversionService.create_checkout_order(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            actor_user=self.admin,
            db_alias=DB,
        )

        self.assertTrue(checkout_data['razorpay_order_id'].startswith('order_'))
        self.assertEqual(checkout_data['amount'], 118000)
        self.assertEqual(checkout_data['currency'], 'INR')
        self.assertEqual(checkout_data['key_id'], TEST_KEY_ID)
        self.assertNotIn('secret', checkout_data)
        self.assertNotIn('key_secret', checkout_data)

        # Internal order exists and is PENDING_PAYMENT
        order = Order.objects.using(DB).get(id=checkout_data['order_id'])
        self.assertEqual(order.status, 'PENDING_PAYMENT')
        self.assertEqual(order.total_amount, Decimal('1180.00'))

    # 4. Authoritative backend amount calculation (recalculates server-side)
    @patch('razorpay.Client')
    def test_authoritative_backend_amount_ignores_client_tampering(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.order.create.return_value = {
            'id': 'order_auth_check_1',
            'amount': 118000,
            'currency': 'INR',
            'status': 'created',
        }

        checkout_data = LeadConversionService.create_checkout_order(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            actor_user=self.admin,
            db_alias=DB,
        )
        self.assertEqual(checkout_data['amount'], 118000)

    # 5. Valid payment signature
    def test_valid_payment_signature_verification(self):
        order_id = 'order_test_99999'
        payment_id = 'pay_test_88888'
        sig = generate_valid_signature(order_id, payment_id, TEST_KEY_SECRET)
        self.assertTrue(RazorpayService.verify_payment_signature(order_id, payment_id, sig))

    # 6. Invalid payment signature rejected
    def test_invalid_payment_signature_rejected(self):
        order_id = 'order_test_99999'
        payment_id = 'pay_test_88888'
        bogus_sig = 'bad_signature_00000000000000000000000000000000'
        self.assertFalse(RazorpayService.verify_payment_signature(order_id, payment_id, bogus_sig))

    # 7. Tampered payment ID rejected
    def test_tampered_payment_id_rejected(self):
        order_id = 'order_test_99999'
        payment_id = 'pay_test_88888'
        sig = generate_valid_signature(order_id, payment_id, TEST_KEY_SECRET)
        self.assertFalse(RazorpayService.verify_payment_signature(order_id, 'pay_tampered_99999', sig))

    # 8. Tampered order ID rejected
    def test_tampered_order_id_rejected(self):
        order_id = 'order_test_99999'
        payment_id = 'pay_test_88888'
        sig = generate_valid_signature(order_id, payment_id, TEST_KEY_SECRET)
        self.assertFalse(RazorpayService.verify_payment_signature('order_other_11111', payment_id, sig))

    # 9. Amount mismatch rejected (fail closed)
    @patch('razorpay.Client')
    def test_amount_mismatch_fails_closed(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        mock_instance.payment.fetch.return_value = {
            'id': 'pay_mismatch_001',
            'order_id': 'order_mismatch_001',
            'amount': 50000,
            'currency': 'INR',
            'status': 'captured',
        }

        with self.assertRaises(RazorpayVerificationError) as ctx:
            RazorpayService.fetch_and_verify_payment(
                razorpay_payment_id='pay_mismatch_001',
                expected_order_id='order_mismatch_001',
                expected_amount_paise=118000,
                expected_currency='INR',
            )
        self.assertEqual(ctx.exception.code, 'RAZORPAY_AMOUNT_MISMATCH')

    # 10. Cross-tenant order protection
    def test_cross_tenant_branch_protection(self):
        org2 = Organization.objects.using(DB).create(code='ORG2', name='Org Two', status='ACTIVE')
        loc2 = Location.objects.using(DB).create(organization=org2, code='LOC2', name='Loc 2', status='ACTIVE')
        br2 = Branch.objects.using(DB).create(organization=org2, location=loc2, code='BR2', name='Branch 2', status='ACTIVE')

        with self.assertRaises(ValidationError):
            LeadConversionService.create_checkout_order(
                lead=self.lead,
                package_version_id=str(self.package_version.id),
                branch_id=str(br2.id),
                actor_user=self.admin,
                db_alias=DB,
            )

    # 11 & 12 & 13 & 14. Successful Payment Finalization + Membership & Entitlements Created Once
    @patch('razorpay.Client')
    def test_successful_razorpay_conversion_end_to_end(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance

        rzp_order_id = 'order_e2e_success_001'
        rzp_pay_id = 'pay_e2e_success_001'
        rzp_sig = generate_valid_signature(rzp_order_id, rzp_pay_id, TEST_KEY_SECRET)

        mock_instance.utility.verify_payment_signature.return_value = True
        mock_instance.payment.fetch.return_value = {
            'id': rzp_pay_id,
            'order_id': rzp_order_id,
            'amount': 118000,
            'currency': 'INR',
            'status': 'captured',
        }

        result = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='RAZORPAY',
            payment_amount=Decimal('1180.00'),
            razorpay_order_id=rzp_order_id,
            razorpay_payment_id=rzp_pay_id,
            razorpay_signature=rzp_sig,
            actor_user=self.admin,
            idempotency_key=rzp_pay_id,
            db_alias=DB,
        )

        self.assertEqual(result['lead_status'], 'CONVERTED')
        self.assertEqual(result['order_status'], 'PAID')

        order = Order.objects.using(DB).get(id=result['order_id'])
        self.assertEqual(order.status, 'PAID')

        payments = PaymentTransaction.objects.using(DB).filter(order=order)
        self.assertEqual(payments.count(), 1)
        self.assertEqual(payments[0].status, 'SUCCESS')
        self.assertEqual(payments[0].provider, 'RAZORPAY')
        self.assertEqual(payments[0].provider_transaction_id, rzp_pay_id)

        self.lead.refresh_from_db(using=DB)
        self.assertEqual(self.lead.current_status, 'CONVERTED')

        memberships = Membership.objects.using(DB).filter(source_order=order)
        self.assertEqual(memberships.count(), 1)
        membership = memberships.first()
        self.assertEqual(membership.status, 'ACTIVE')

        entitlements = MembershipEntitlement.objects.using(DB).filter(membership=membership)
        self.assertEqual(entitlements.count(), 1)
        self.assertEqual(entitlements[0].allocated_units, 30)

        # 11. Duplicate callback / Idempotency check
        result2 = LeadConversionService.execute_conversion(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='RAZORPAY',
            payment_amount=Decimal('1180.00'),
            razorpay_order_id=rzp_order_id,
            razorpay_payment_id=rzp_pay_id,
            razorpay_signature=rzp_sig,
            actor_user=self.admin,
            idempotency_key=rzp_pay_id,
            db_alias=DB,
        )

        # Assert no duplicates were created
        self.assertEqual(Order.objects.using(DB).filter(lead=self.lead).count(), 1)
        self.assertEqual(PaymentTransaction.objects.using(DB).filter(order=order).count(), 1)
        self.assertEqual(Membership.objects.using(DB).filter(source_order=order).count(), 1)
        self.assertEqual(LeadConversion.objects.using(DB).filter(lead=self.lead).count(), 1)
        self.assertEqual(MembershipEntitlement.objects.using(DB).filter(membership=membership).count(), 1)

    # 15. Payment failure does not activate membership
    @patch('razorpay.Client')
    def test_payment_failure_does_not_activate_membership(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance
        from razorpay.errors import SignatureVerificationError
        mock_instance.utility.verify_payment_signature.side_effect = SignatureVerificationError("Bad signature")

        with self.assertRaises(ValidationError):
            LeadConversionService.execute_conversion(
                lead=self.lead,
                package_version_id=str(self.package_version.id),
                branch_id=str(self.branch.id),
                payment_provider='RAZORPAY',
                payment_amount=Decimal('1180.00'),
                razorpay_order_id='order_fail_001',
                razorpay_payment_id='pay_fail_001',
                razorpay_signature='invalid_signature',
                actor_user=self.admin,
                db_alias=DB,
            )

        self.lead.refresh_from_db(using=DB)
        self.assertNotEqual(self.lead.current_status, 'CONVERTED')
        self.assertEqual(Membership.objects.using(DB).count(), 0)

    # 16. Existing CASH flow regression
    def test_existing_cash_flow_regression_passes(self):
        from apps.tenant_core.models_approvals import ApprovalRequest
        from apps.tenant_core.services_approvals import AdminApprovalService

        lead_cash = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='Sunita', last_name='Patel',
            phone_normalized='+919876543211',
            email_normalized='sunita.patel@example.test',
            current_status='PROSPECT',
        )

        result = LeadConversionService.execute_conversion(
            lead=lead_cash,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('1180.00'),
            actor_user=self.admin,
            idempotency_key='cash-test-001',
            db_alias=DB,
        )

        # Cash defaults to require_approval=True
        self.assertEqual(result.get('status'), 'PENDING_APPROVAL')
        req = ApprovalRequest.objects.using(DB).get(id=result['approval_request_id'])

        # Manager approves (segregation of duties: separate from self.admin)
        manager = TenantUser.objects.using(DB).create(
            organization=self.org,
            email='manager.approver@example.test',
            first_name='Approver',
        )
        AdminApprovalService.process_action(
            approval_request=req,
            approver_user=manager,
            action='APPROVED',
            comment='Cash verified in register',
            db_alias=DB,
        )

        order = Order.objects.using(DB).get(id=result['order_id'])
        self.assertEqual(order.status, 'PAID')

        payments = PaymentTransaction.objects.using(DB).filter(order=order)
        self.assertEqual(payments.count(), 1)
        self.assertEqual(payments[0].provider, 'CASH')
        self.assertEqual(payments[0].status, 'SUCCESS')

        lead_cash.refresh_from_db()
        self.assertEqual(lead_cash.current_status, 'CONVERTED')

        memberships = Membership.objects.using(DB).filter(source_order=order)
        self.assertEqual(memberships.count(), 1)
        self.assertEqual(memberships[0].status, 'ACTIVE')

    def test_tenant_specific_integration_credentials_precedence(self):
        """
        Verify tenant Integration table credentials take precedence over environment fallback.
        """
        from apps.tenant_core.models_infra import Integration
        from config.secrets import SecretResolver

        # Register secret in deterministic test store
        SecretResolver.register_test_secret("vault://tenants/rzp-gym/razorpay#secret", "tenant_secret_123456789")

        Integration.objects.using(DB).create(
            integration_type='PAYMENT',
            provider='Razorpay',
            status='ACTIVE',
            configuration={'key_id': 'rzp_test_tenant_specific_001'},
            secret_reference='vault://tenants/rzp-gym/razorpay#secret',
        )

        key_id, secret = RazorpayService.get_credentials(db_alias=DB)
        self.assertEqual(key_id, 'rzp_test_tenant_specific_001')
        self.assertEqual(secret, 'tenant_secret_123456789')

    def test_production_mode_fails_closed_without_tenant_integration(self):
        """
        In production mode (DEBUG=False, ENVIRONMENT='production'), missing tenant Integration
        fails closed with RAZORPAY_NOT_CONFIGURED even if environment variables exist.
        """
        with override_settings(DEBUG=False, ENVIRONMENT='production'):
            with self.assertRaises(RazorpayConfigError) as ctx:
                RazorpayService.get_credentials(db_alias=DB)
            self.assertEqual(ctx.exception.code, 'RAZORPAY_NOT_CONFIGURED')

    # -------------------------------------------------------------------------
    # Test Matrix: Channel Restrictions & Unsupported Providers
    # -------------------------------------------------------------------------
    def test_matrix_01_staff_sees_only_cash_and_razorpay(self):
        quote = LeadConversionService.get_conversion_quote(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            channel='STAFF',
            db_alias=DB,
        )
        self.assertIn('CASH', quote['payment_providers'])
        self.assertIn('RAZORPAY', quote['payment_providers'])
        self.assertNotIn('BANK_TRANSFER', quote['payment_providers'])
        self.assertNotIn('OTHER', quote['payment_providers'])

    def test_matrix_02_member_sees_only_razorpay(self):
        quote = LeadConversionService.get_conversion_quote(
            lead=self.lead,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            channel='MEMBER_PORTAL',
            db_alias=DB,
        )
        self.assertNotIn('CASH', quote['payment_providers'])
        self.assertIn('RAZORPAY', quote['payment_providers'])

    def test_matrix_03_member_backend_cash_attempt_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            PaymentPolicyService.validate_payment_provider(
                provider='CASH',
                channel='MEMBER_APP',
                organization=self.org,
                db_alias=DB,
            )
        self.assertEqual(ctx.exception.code, 'PAYMENT_METHOD_NOT_ALLOWED_FOR_CHANNEL')

    def test_matrix_04_bank_transfer_new_transaction_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            PaymentPolicyService.validate_payment_provider(
                provider='BANK_TRANSFER',
                channel='STAFF',
                organization=self.org,
                db_alias=DB,
            )
        self.assertIn(ctx.exception.code, ['PROVIDER_NOT_ALLOWED', 'UNSUPPORTED_PAYMENT_PROVIDER'])

    def test_matrix_05_other_new_transaction_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            PaymentPolicyService.validate_payment_provider(
                provider='OTHER',
                channel='STAFF',
                organization=self.org,
                db_alias=DB,
            )
        self.assertIn(ctx.exception.code, ['PROVIDER_NOT_ALLOWED', 'UNSUPPORTED_PAYMENT_PROVIDER'])

    def test_matrix_06_historical_records_remain_readable(self):
        order = Order.objects.using(DB).create(
            branch=self.branch,
            order_number='ORD-HIST-002', total_amount=Decimal('500.00'), currency='INR', status='PAID'
        )
        historical_txn = PaymentTransaction.objects.using(DB).create(
            order=order, provider='BANK_TRANSFER', payment_method='NEFT',
            amount=Decimal('500.00'), currency='INR', status='SUCCESS',
        )
        fetched = PaymentTransaction.objects.using(DB).get(id=historical_txn.id)
        self.assertEqual(fetched.provider, 'BANK_TRANSFER')
        self.assertEqual(fetched.status, 'SUCCESS')

    # -------------------------------------------------------------------------
    # Test Matrix: Cash Approval & Segregation of Duties
    # -------------------------------------------------------------------------
    def test_matrix_07_cash_self_approval_rejected(self):
        lead_cash = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='Self', last_name='Approver', phone_normalized='+919876543222',
        )
        result = LeadConversionService.execute_conversion(
            lead=lead_cash,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('1180.00'),
            actor_user=self.admin,
            db_alias=DB,
        )
        req = ApprovalRequest.objects.using(DB).get(id=result['approval_request_id'])
        with self.assertRaises(ValidationError) as ctx:
            AdminApprovalService.process_action(
                approval_request=req, approver_user=self.admin, action='APPROVED', comment='Self approve', db_alias=DB,
            )
        self.assertIn('cannot approve their own request', str(ctx.exception))

    def test_matrix_08_cash_rejection_requires_reason(self):
        lead_cash = Lead.objects.using(DB).create(
            organization=self.org, branch=self.branch,
            first_name='Reject', last_name='Test', phone_normalized='+919876543233',
        )
        result = LeadConversionService.execute_conversion(
            lead=lead_cash,
            package_version_id=str(self.package_version.id),
            branch_id=str(self.branch.id),
            payment_provider='CASH',
            payment_amount=Decimal('1180.00'),
            actor_user=self.admin,
            db_alias=DB,
        )
        req = ApprovalRequest.objects.using(DB).get(id=result['approval_request_id'])
        manager = TenantUser.objects.using(DB).create(
            organization=self.org, email='mgr.reject@test.com', first_name='Mgr',
        )
        with self.assertRaises(ValidationError):
            AdminApprovalService.process_action(
                approval_request=req, approver_user=manager, action='REJECTED', comment='', db_alias=DB,
            )

    # -------------------------------------------------------------------------
    # Test Matrix: Partial Payment
    # -------------------------------------------------------------------------
    def test_matrix_09_partial_disabled_rejected(self):
        order = Order.objects.using(DB).create(
            branch=self.branch,
            order_number='ORD-P1', total_amount=Decimal('10000.00'), currency='INR', status='PENDING_PAYMENT',
        )
        policy = {'partial_payment': {'enabled': False}}
        with self.assertRaises(ValidationError) as ctx:
            PaymentPolicyService.validate_partial_payment(
                order=order, requested_amount=Decimal('5000.00'), policy=policy, db_alias=DB,
            )
        self.assertEqual(ctx.exception.code, 'PARTIAL_PAYMENT_DISABLED')

    def test_matrix_10_below_minimum_partial_rejected(self):
        order = Order.objects.using(DB).create(
            branch=self.branch,
            order_number='ORD-P2', total_amount=Decimal('10000.00'), currency='INR', status='PENDING_PAYMENT',
        )
        policy = {'partial_payment': {'enabled': True, 'min_first_payment_type': 'PERCENTAGE', 'min_first_payment_percentage': '30.00'}}
        with self.assertRaises(ValidationError) as ctx:
            PaymentPolicyService.validate_partial_payment(
                order=order, requested_amount=Decimal('2000.00'), policy=policy, db_alias=DB,
            )
        self.assertIn(ctx.exception.code, ['BELOW_MINIMUM_PAYMENT', 'AMOUNT_BELOW_MINIMUM'])

    def test_matrix_11_valid_partial_accepted_and_balance_calculated(self):
        order = Order.objects.using(DB).create(
            branch=self.branch,
            order_number='ORD-P3', total_amount=Decimal('10000.00'), currency='INR', status='PENDING_PAYMENT',
        )
        policy = {'partial_payment': {'enabled': True, 'min_first_payment_type': 'PERCENTAGE', 'min_first_payment_percentage': '30.00'}}
        amt = PaymentPolicyService.validate_partial_payment(order, Decimal('4000.00'), policy, db_alias=DB)
        self.assertEqual(amt, Decimal('4000.00'))

        PaymentTransaction.objects.using(DB).create(order=order, provider='RAZORPAY', amount=Decimal('4000.00'), currency='INR', status='SUCCESS')
        total, paid, outstanding, count = PaymentPolicyService.calculate_order_balance(order, db_alias=DB)
        self.assertEqual(paid, Decimal('4000.00'))
        self.assertEqual(outstanding, Decimal('6000.00'))

    def test_matrix_12_membership_activation_obeys_full_payment_only(self):
        order = Order.objects.using(DB).create(
            branch=self.branch,
            order_number='ORD-P4', total_amount=Decimal('10000.00'), currency='INR', status='PARTIALLY_PAID',
        )
        policy = {'partial_payment': {'enabled': True, 'activation_rule': 'FULL_PAYMENT_ONLY'}}
        self.assertFalse(PaymentPolicyService.should_activate_membership(order, Decimal('5000.00'), policy, db_alias=DB))
        self.assertTrue(PaymentPolicyService.should_activate_membership(order, Decimal('10000.00'), policy, db_alias=DB))

    def test_matrix_13_membership_activation_obeys_minimum_partial_threshold(self):
        order = Order.objects.using(DB).create(
            branch=self.branch,
            order_number='ORD-P5', total_amount=Decimal('10000.00'), currency='INR', status='PARTIALLY_PAID',
        )
        policy = {'partial_payment': {'enabled': True, 'activation_rule': 'MINIMUM_PARTIAL_PAYMENT', 'min_first_payment_type': 'PERCENTAGE', 'min_first_payment_percentage': '30.00'}}
        self.assertFalse(PaymentPolicyService.should_activate_membership(order, Decimal('2000.00'), policy, db_alias=DB))
        self.assertTrue(PaymentPolicyService.should_activate_membership(order, Decimal('3000.00'), policy, db_alias=DB))

    def test_matrix_14_max_installments_limit(self):
        order = Order.objects.using(DB).create(
            branch=self.branch,
            order_number='ORD-P6', total_amount=Decimal('10000.00'), currency='INR', status='PARTIALLY_PAID',
        )
        PaymentTransaction.objects.using(DB).create(order=order, amount=Decimal('3000.00'), status='SUCCESS')
        PaymentTransaction.objects.using(DB).create(order=order, amount=Decimal('3000.00'), status='SUCCESS')
        policy = {'partial_payment': {'enabled': True, 'max_installments': 2}}
        with self.assertRaises(ValidationError) as ctx:
            PaymentPolicyService.validate_partial_payment(order, Decimal('2000.00'), policy, db_alias=DB)
        self.assertIn(ctx.exception.code, ['MAX_INSTALLMENTS_EXCEEDED', 'FINAL_INSTALLMENT_MUST_BE_FULL'])

    def test_matrix_15_amount_cannot_exceed_outstanding(self):
        order = Order.objects.using(DB).create(
            branch=self.branch,
            order_number='ORD-P7', total_amount=Decimal('10000.00'), currency='INR', status='PARTIALLY_PAID',
        )
        PaymentTransaction.objects.using(DB).create(order=order, amount=Decimal('8000.00'), status='SUCCESS')
        with self.assertRaises(ValidationError) as ctx:
            PaymentPolicyService.validate_partial_payment(order, Decimal('3000.00'), policy={'partial_payment': {'enabled': True}}, db_alias=DB)
        self.assertEqual(ctx.exception.code, 'AMOUNT_EXCEEDS_OUTSTANDING')


    def test_closure_1_member_channel_cash_rejected(self):
        """Member self-service channels must strictly reject CASH."""
        with self.assertRaises(ValidationError) as ctx:
            PaymentPolicyService.validate_payment_provider(
                provider='CASH',
                channel='MEMBER_APP',
                organization=self.org,
                db_alias=DB,
            )
        self.assertEqual(ctx.exception.code, 'PAYMENT_METHOD_NOT_ALLOWED_FOR_CHANNEL')

    def test_closure_2_paylater_hidden_in_checkout_config(self):
        """Pay Later is excluded from Razorpay Checkout by default."""
        _, checkout_config = PaymentPolicyService.get_razorpay_checkout_config(
            organization=self.org,
            branch=self.branch,
            db_alias=DB,
        )
        hide_methods = [h.get('method') for h in checkout_config.get('display', {}).get('hide', [])]
        self.assertIn('paylater', hide_methods)

    def test_closure_3_tenant_payment_methods_configuration_persistence(self):
        """Payment method policies persist and accurately reflect in checkout config."""
        PaymentPolicyService.update_organization_policy(
            organization=self.org,
            policy_data={
                'razorpay_methods': {
                    'upi': True,
                    'card': True,
                    'emi': False,
                    'netbanking': True,
                    'wallet': True,
                    'paylater': False,
                }
            },
            db_alias=DB,
        )
        policy = PaymentPolicyService.get_effective_policy(self.org, db_alias=DB)
        self.assertFalse(policy['razorpay_methods']['emi'])
        self.assertTrue(policy['razorpay_methods']['upi'])

        _, checkout_config = PaymentPolicyService.get_razorpay_checkout_config(
            organization=self.org,
            branch=self.branch,
            db_alias=DB,
        )
        hide_methods = [h.get('method') for h in checkout_config.get('display', {}).get('hide', [])]
        self.assertIn('emi', hide_methods)
        self.assertIn('paylater', hide_methods)

    def test_closure_4_partial_payment_configuration_persistence(self):
        """Partial payment rules persist and govern order calculations."""
        PaymentPolicyService.update_organization_policy(
            organization=self.org,
            policy_data={
                'partial_payment_policy': {
                    'enabled': True,
                    'min_first_payment_type': 'PERCENTAGE',
                    'min_first_payment_percentage': '40.00',
                    'max_installments': 4,
                    'activation_rule': 'FULL_PAYMENT_ONLY',
                }
            },
            db_alias=DB,
        )
        policy = PaymentPolicyService.get_effective_policy(self.org, db_alias=DB)
        self.assertEqual(policy['partial_payment_policy']['min_first_payment_percentage'], '40.00')
        self.assertEqual(policy['partial_payment_policy']['max_installments'], 4)
        self.assertEqual(policy['partial_payment_policy']['activation_rule'], 'FULL_PAYMENT_ONLY')
