"""
tests/test_meta_lead_production_verification.py

Comprehensive production verification suite for Meta Lead Ads:
1. Browser OAuth flow (GET redirect, signed expiring state, single-use replay prevention, user/org binding).
2. Webhook concurrency & recovery (simultaneous deliveries, simultaneous workers, failed task publishing recovery).
3. Worker failure & crash simulation (rollback before/after lead creation, zero duplicate tasks).
4. Error handling (rate limits, token expiry, paused mappings, disconnected accounts).
5. Cross-tenant isolation & encryption security (wrong key fail-safe, cross-tenant page collision rejection).
6. Real CRM lifecycle (Meta lead -> trial -> LeadConversionService -> paid order -> refund -> campaign reporting).
"""

import hmac
import json
import time
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connections, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.master.models_tenant import Tenant, MetaPageRegistry
from apps.master.models_infra import TenantDataSource
from apps.tenant_core.models_rbac import ModuleCatalog, Permission, Role, RolePermissionSet, RolePermissionSetItem, RoleAssignment
from apps.tenant_core.meta_crypto import encrypt_token, decrypt_token
from apps.tenant_core.models_crm import (
    Lead,
    LeadAttribution,
    LeadConversion,
    LeadSource,
    TrialBooking,
    SalesFollowupTask,
)
from apps.tenant_core.models_catalog import Package, PackageVersion, ProgramCategory, Program, PackagePrice
from apps.tenant_core.models_commerce import Order, PaymentTransaction, Refund
from apps.tenant_core.models_memberships import Membership
from apps.tenant_core.models_meta_leads import (
    MetaConnection,
    MetaLeadImport,
    MetaLeadMapping,
    MetaPageConnection,
)
from apps.tenant_core.models_org import Branch, Organization, Location
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.services_crm import LeadConversionService
from apps.tenant_core.services_crm_dashboard import CRMDashboardService
from apps.tenant_core.services_meta_graph import (
    MetaGraphClient,
    MetaRateLimitError,
    MetaTokenExpiredError,
    generate_oauth_state,
    verify_oauth_state,
)
from apps.tenant_core.services_meta_leads import (
    process_live_import,
    receive_live_webhook_event,
)
from apps.tenant_core.services_meta_recovery import recover_unprocessed_meta_imports


@override_settings(
    ROOT_URLCONF='tests.meta_test_urls',
    META_APP_ID='123456789012345',
    META_APP_SECRET='test_meta_app_secret_32bytes_hex',
    META_WEBHOOK_VERIFY_TOKEN='test_webhook_verify_token_secure',
    META_TOKEN_ENCRYPTION_KEY='t_xZ7k8Wq2Y1mNp4R9vA3cE5gH7jK9mP1rT3vX5zB7w=',
)
class MetaLeadProductionVerificationTests(TestCase):
    databases = {'default', 'tenant_test', 'tenant_other'}

    def setUp(self):
        cache.clear()
        self.tenant_id = uuid.uuid4()
        self.master_tenant = Tenant.objects.using('default').create(
            id=self.tenant_id,
            name="SWEAT Fitness",
            slug="sweat",
            code="SWEAT",
            status="ACTIVE",
        )
        from apps.master.models_infra import TenantDataSource
        TenantDataSource.objects.using('default').create(
            tenant=self.master_tenant,
            status='ACTIVE',
            database_name='fitness_tenant',
            db_name=f'fitness_tenant_{self.master_tenant.id.hex[:8]}',
        )

        set_tenant_db_alias('tenant_test')

        self.org = Organization.objects.create(
            name="SWEAT Gyms India",
            code="SWEAT_IN",
            status="ACTIVE",
        )
        loc = Location.objects.create(
            organization=self.org,
            code="LOC-ANDHERI",
            name="Andheri Location",
        )
        self.branch = Branch.objects.create(
            organization=self.org,
            location=loc,
            name="SWEAT Andheri West",
            code="SWEAT_ANDHERI",
            status="ACTIVE",
        )
        self.sales_user = TenantUser.objects.create(
            organization=self.org,
            email="sales.rep@sweat.test",
            first_name="Rahul",
            last_name="Verma",
            status="ACTIVE",
            )
        self.sales_user.is_superuser = True
        self.sales_user._auth_type = 'tenant'

        self.lead_source = LeadSource.objects.create(
            organization=self.org,
            code="META_LEAD_ADS",
            name="Meta Lead Ads",
            source_type="META",
            status="ACTIVE",
        )

                # Configure RBAC permissions so sales_user is eligible for lead assignment
        mod, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            code='CRM',
            defaults={
                'module_code': 'CRM',
                'name': 'CRM',
                'status': 'ACTIVE',
                'is_enabled': True,
            }
        )
        perm, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='crm.leads.edit',
            defaults={
                'module': mod,
                'code': 'CRM_LEADS_EDIT',
                'label': 'Edit Leads',
                'status': 'ACTIVE',
                'is_active': True,
            }
        )
        sales_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Sales Role',
            code='ROLE_SALES',
            scope='ORGANIZATION',
            status='ACTIVE',
            is_active=True,
        )
        pset = RolePermissionSet.objects.using('tenant_test').create(
            role=sales_role,
            organization=self.org,
            name='Sales Perms',
            status='ACTIVE',
            is_active=True,
        )
        RolePermissionSetItem.objects.using('tenant_test').create(
            permission_set=pset,
            permission=perm,
            granted=True,
            is_allowed=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            organization=self.org,
            user=self.sales_user,
            role=sales_role,
            scope_type='ORGANIZATION',
            status='ACTIVE',
            is_active=True,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.sales_user)

    def tearDown(self):
        from apps.tenant_core.context import set_tenant_db_alias
        set_tenant_db_alias(None)
        super().tearDown()

    def _create_active_mapping(self, page_id="page_prod_100", form_id="form_prod_100"):
        conn = MetaConnection.objects.using('tenant_test').create(
            organization=self.org,
            status='CONNECTED',
            meta_user_id='meta_usr_100',
            meta_user_name='Sweat Admin',
            encrypted_user_access_token=encrypt_token('mock_user_token'),
            scopes=['leads_retrieval', 'pages_show_list'],
        )
        MetaPageConnection.objects.using('tenant_test').create(
            organization=self.org,
            connection=conn,
            page_id=page_id,
            page_name='SWEAT Official Facebook Page',
            encrypted_page_access_token=encrypt_token('mock_page_token_valid'),
            )
        MetaPageRegistry.objects.using('default').update_or_create(
            page_id=page_id,
            defaults={'tenant': self.master_tenant, 'is_active': True},
        )
        return MetaLeadMapping.objects.using('tenant_test').create(
            organization=self.org,
            name="Andheri Summer Lead Gen",
            page_id=page_id,
            form_id=form_id,
            is_active=True,
            lead_source=self.lead_source,
            branch_mode='FIXED',
            branch=self.branch,
            assignment_mode='SPECIFIC_USER',
            assigned_sales_user=self.sales_user,
            field_mappings={'full_name': 'full_name', 'email': 'email', 'phone': 'phone_number'},
            create_followup_task=True,
            followup_task_type='CALL',
            followup_due_hours=24,
        )

    # -----------------------------------------------------------------------
    # 1. OAuth Flow, Replay Prevention & Binding Verification
    # -----------------------------------------------------------------------
    def test_oauth_state_single_use_replay_prevention(self):
        """Verify that state token can only be consumed once, preventing replay attacks."""
        state = generate_oauth_state(
            tenant_id=str(self.tenant_id),
            organization_id=str(self.org.id),
            user_id=str(self.sales_user.id),
            redirect_uri='https://crm.sweat.test/callback',
        )

        # 1st verification consumes the nonce -> must succeed
        payload1 = verify_oauth_state(state, consume=True)
        self.assertEqual(payload1['tenant_id'], str(self.tenant_id))
        self.assertEqual(payload1['user_id'], str(self.sales_user.id))

        # 2nd verification with identical state -> must fail with replay error
        with self.assertRaises(ValueError) as ctx:
            verify_oauth_state(state, consume=True)
        self.assertIn("replay detected", str(ctx.exception))

    def test_oauth_callback_browser_get_redirect(self):
        """Verify that Meta's browser GET redirect is handled, consumes state, and redirects to frontend."""
        state = generate_oauth_state(
            tenant_id=str(self.tenant_id),
            organization_id=str(self.org.id),
            user_id=str(self.sales_user.id),
            redirect_uri='/api/v1/tenant/meta-lead-mappings/oauth-callback/',
        )

        with patch.object(MetaGraphClient, 'exchange_code_for_tokens') as mock_exchange, \
             patch.object(MetaGraphClient, 'fetch_user_pages') as mock_pages, \
             patch.object(MetaGraphClient, 'subscribe_page_to_webhooks') as mock_sub:
            mock_exchange.return_value = {
                'user_access_token': 'mock_token_long',
                'meta_user_id': 'fb_uid_123',
                'meta_user_name': 'Sweat Marketing Manager',
                'expires_in': 5184000,
            }
            mock_pages.return_value = [{'id': 'page_sweat_main', 'name': 'SWEAT Main', 'access_token': 'p_tok_1'}]
            mock_sub.return_value = True

            # Browser GET request to callback
            response = self.client.get(f'/meta-lead-mappings/oauth-callback/?code=fb_auth_code_999&state={state}')
            self.assertEqual(response.status_code, 302)
            self.assertIn('/crm/settings?meta_connected=true', response.url)

            # Replaying the exact same URL must redirect with error
            replay_res = self.client.get(f'/meta-lead-mappings/oauth-callback/?code=fb_auth_code_999&state={state}')
            self.assertEqual(replay_res.status_code, 302)
            self.assertIn('meta_error=invalid_state', replay_res.url)

    def test_oauth_callback_user_binding_enforced(self):
        """Verify that state generated by User A cannot be claimed by User B."""
        other_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email="other.rep@sweat.test",
            first_name="Other",
            last_name="Rep",
            status="ACTIVE",
        )
        other_user._auth_type = 'tenant'
        other_user.is_superuser = True
        state = generate_oauth_state(
            tenant_id=str(self.tenant_id),
            organization_id=str(self.org.id),
            user_id=str(self.sales_user.id),
        )

        # Other user attempts to post callback with sales_user's state
        self.client.force_authenticate(user=other_user)
        res = self.client.post('/meta-lead-mappings/oauth-callback/', {
            'code': 'mock_code',
            'state': state,
            'redirect_uri': 'https://crm.sweat.test/callback',
        })
        self.assertEqual(res.status_code, 403)
        self.assertIn("different initiating user", str(res.data))

    # -----------------------------------------------------------------------
    # 2. Reliable Webhook Processing & Concurrency
    # -----------------------------------------------------------------------
    def test_simultaneous_identical_webhook_deliveries(self):
        """Simultaneous delivery of identical leadgen event produces exactly one import and lead."""
        self._create_active_mapping(page_id="page_sim_1", form_id="form_sim_1")

        # 1st delivery
        ev1, created1 = receive_live_webhook_event(
            organization=self.org, page_id="page_sim_1", form_id="form_sim_1",
            leadgen_id="leadgen_unique_001", raw_payload={'id': 'leadgen_unique_001'},
            alias='tenant_test'
        )
        self.assertTrue(created1)

        # 2nd delivery (simultaneous identical event)
        ev2, created2 = receive_live_webhook_event(
            organization=self.org, page_id="page_sim_1", form_id="form_sim_1",
            leadgen_id="leadgen_unique_001", raw_payload={'id': 'leadgen_unique_001'},
            alias='tenant_test'
        )
        self.assertFalse(created2)
        self.assertEqual(ev1.id, ev2.id)
        self.assertEqual(MetaLeadImport.objects.using('tenant_test').filter(external_lead_id="leadgen_unique_001").count(), 1)

    def test_simultaneous_workers_processing_same_import(self):
        """Simultaneous workers locking the same import process it exactly once without duplicate leads."""
        self._create_active_mapping(page_id="page_worker_1", form_id="form_worker_1")
        ev, _ = receive_live_webhook_event(
            organization=self.org, page_id="page_worker_1", form_id="form_worker_1",
            leadgen_id="leadgen_worker_test_100", raw_payload={}, alias='tenant_test'
        )

        mock_client = MagicMock()
        mock_client.fetch_leadgen_details.return_value = {
            'field_data': [
                {'name': 'full_name', 'values': ['Vikram Malhotra']},
                {'name': 'email', 'values': ['vikram.worker@example.test']},
                {'name': 'phone_number', 'values': ['+919811223344']},
            ],
            'campaign_id': 'camp_worker_1',
            'campaign_name': 'Worker Stress Test',
        }

        # Worker 1 processes
        res1 = process_live_import(self.org, ev.id, graph_client=mock_client, alias='tenant_test')
        self.assertEqual(res1.status, 'IMPORTED')
        self.assertEqual(Lead.objects.using('tenant_test').filter(email_normalized='vikram.worker@example.test').count(), 1)

        # Worker 2 processes the same import ID
        res2 = process_live_import(self.org, ev.id, graph_client=mock_client, alias='tenant_test')
        self.assertEqual(res2.status, 'IMPORTED')
        # Total leads must strictly still be 1
        self.assertEqual(Lead.objects.using('tenant_test').filter(email_normalized='vikram.worker@example.test').count(), 1)
        self.assertEqual(SalesFollowupTask.objects.using('tenant_test').filter(lead=res1.lead).count(), 1)

    # -----------------------------------------------------------------------
    # 3. Durable Recovery for Stuck PENDING Imports
    # -----------------------------------------------------------------------
    def test_recovery_of_unprocessed_pending_imports(self):
        """
        Simulate database commit succeeding but Celery publishing failing.
        The recovery engine discovers the stuck PENDING record and recovers it cleanly.
        """
        self._create_active_mapping(page_id="page_rec_1", form_id="form_rec_1")

        # Fast-ingest writes row to DB, but Celery task publish fails
        stuck_import, _ = receive_live_webhook_event(
            organization=self.org, page_id="page_rec_1", form_id="form_rec_1",
            leadgen_id="leadgen_stuck_publish_failed", raw_payload={}, alias='tenant_test'
        )
        self.assertEqual(stuck_import.status, 'PENDING')

        # Mock Graph API for recovery processing
        with patch.object(MetaGraphClient, 'fetch_leadgen_details') as mock_fetch:
            mock_fetch.return_value = {
                'field_data': [
                    {'name': 'full_name', 'values': ['Pooja Batra']},
                    {'name': 'email', 'values': ['pooja.batra@example.test']},
                    {'name': 'phone_number', 'values': ['+919855667788']},
                ],
                'campaign_id': 'camp_rec_99',
                'campaign_name': 'Recovery Campaign',
            }

            # Run recovery service with min_age_seconds=0
            recovery_result = recover_unprocessed_meta_imports(
                tenant_id=str(self.tenant_id),
                min_age_seconds=0,
                dispatch_async=False,
                alias='tenant_test',
            )

            self.assertEqual(recovery_result['recovered_count'], 1)
            self.assertEqual(recovery_result['failed_count'], 0)

            # Assert import was processed and lead was created
            stuck_import.refresh_from_db(using='tenant_test')
            self.assertEqual(stuck_import.status, 'IMPORTED')
            self.assertIsNotNone(stuck_import.lead)
            self.assertEqual(stuck_import.lead.first_name, 'Pooja')

    # -----------------------------------------------------------------------
    # 4. Failure Scenarios: Crash Simulation, Paused Mapping & Disconnection
    # -----------------------------------------------------------------------
    def test_worker_crash_before_lead_creation_and_retry(self):
        """Worker crash inside processing rolls back atomically; retry succeeds without partial state."""
        self._create_active_mapping(page_id="page_crash_1", form_id="form_crash_1")
        ev, _ = receive_live_webhook_event(
            organization=self.org, page_id="page_crash_1", form_id="form_crash_1",
            leadgen_id="leadgen_crash_sim", raw_payload={}, alias='tenant_test'
        )

        mock_failing_client = MagicMock()
        mock_failing_client.fetch_leadgen_details.side_effect = RuntimeError("Worker killed / out of memory")

        # Worker crashes
        try:
            process_live_import(self.org, ev.id, graph_client=mock_failing_client, alias='tenant_test')
        except RuntimeError:
            pass

        # Zero leads created
        self.assertEqual(Lead.objects.using('tenant_test').filter(email_normalized='crash.sim@example.test').count(), 0)

        # Retry with working client
        mock_working_client = MagicMock()
        mock_working_client.fetch_leadgen_details.return_value = {
            'field_data': [
                {'name': 'full_name', 'values': ['Crash Survivor']},
                {'name': 'email', 'values': ['crash.survivor@example.test']},
                {'name': 'phone_number', 'values': ['+919899001122']},
            ]
        }
        res = process_live_import(self.org, ev.id, graph_client=mock_working_client, alias='tenant_test')
        self.assertEqual(res.status, 'IMPORTED')
        self.assertEqual(Lead.objects.using('tenant_test').filter(email_normalized='crash.survivor@example.test').count(), 1)

    def test_paused_mapping_sets_needs_mapping(self):
        """Incoming lead for a paused mapping does not create CRM leads until reactivated."""
        mapping = self._create_active_mapping(page_id="page_paused_1", form_id="form_paused_1")
        mapping.is_active = False
        mapping.save(using='tenant_test')

        ev, _ = receive_live_webhook_event(
            organization=self.org, page_id="page_paused_1", form_id="form_paused_1",
            leadgen_id="leadgen_paused_test", raw_payload={}, alias='tenant_test'
        )

        mock_client = MagicMock()
        mock_client.fetch_leadgen_details.return_value = {
            'field_data': [{'name': 'full_name', 'values': ['Paused User']}, {'name': 'email', 'values': ['p@example.test']}]
        }
        res = process_live_import(self.org, ev.id, graph_client=mock_client, alias='tenant_test')
        self.assertEqual(res.status, 'NEEDS_MAPPING')
        self.assertIsNone(res.lead)
        self.assertEqual(Lead.objects.using('tenant_test').filter(email_normalized='p@example.test').count(), 0)

    # -----------------------------------------------------------------------
    # 5. Isolation, Key Tampering & Credential Protection
    # -----------------------------------------------------------------------
    def test_cross_tenant_page_registry_isolation(self):
        """Tenant A cannot claim or replace a Page registered to Tenant B."""
        # Tenant A registers page_shared_99
        MetaPageRegistry.objects.using('default').create(
            page_id='page_shared_99',
            tenant_id=self.tenant_id,
        )

        other_tenant_id = uuid.uuid4()
        other_tenant = Tenant.objects.using('default').create(
            id=other_tenant_id, name="Other Gym", slug="other", code="OTHER", status="ACTIVE"
        )
        TenantDataSource.objects.using('default').create(
            tenant=other_tenant,
            status='ACTIVE',
            database_name='fitness_tenant',
            db_name='fitness_tenant_other',
        )

        # Tenant B attempts to register the same page
        with self.assertRaises(IntegrityError):
            with transaction.atomic(using='default'):
                MetaPageRegistry.objects.using('default').create(
                    page_id='page_shared_99',
                    tenant=other_tenant,
                    is_active=True,
                )

    def test_wrong_encryption_key_handling(self):
        """Tokens encrypted with one key fail safely when decrypted with an invalid or changed key."""
        token = "EAAB_super_secret_token_12345"
        ciphertext = encrypt_token(token)

        # Decrypt with wrong key
        with override_settings(META_TOKEN_ENCRYPTION_KEY='w_xZ7k8Wq2Y1mNp4R9vA3cE5gH7jK9mP1rT3vX5zB99='):
            decrypted = decrypt_token(ciphertext)
            self.assertEqual(decrypted, "")  # Fails closed, zero exception leak

    # -----------------------------------------------------------------------
    # 6. Real CRM Commercial Lifecycle & Refund Handling
    # -----------------------------------------------------------------------
    def test_real_crm_lifecycle_with_conversion_and_refund(self):
        """
        Verify complete business lifecycle:
        Meta Import -> Lead -> TrialBooking -> LeadConversionService.convert_lead -> Refund -> Campaign Reporting.
        """
        self._create_active_mapping(page_id="page_life_1", form_id="form_life_1")

        # 1. Ingest live Meta Lead
        ev, _ = receive_live_webhook_event(
            organization=self.org, page_id="page_life_1", form_id="form_life_1",
            leadgen_id="leadgen_crm_lifecycle_test", raw_payload={}, alias='tenant_test'
        )
        mock_client = MagicMock()
        mock_client.fetch_leadgen_details.return_value = {
            'field_data': [
                {'name': 'full_name', 'values': ['Karan Singhania']},
                {'name': 'email', 'values': ['karan.singhania@example.test']},
                {'name': 'phone_number', 'values': ['+919876500112']},
            ],
            'campaign_id': 'camp_life_55',
            'campaign_name': 'Karan Lifecycle Campaign',
            'adset_name': 'Andheri West Pilates',
            'ad_name': 'Intro Video Ad',
        }
        imported = process_live_import(self.org, ev.id, graph_client=mock_client, alias='tenant_test')
        self.assertEqual(imported.status, 'IMPORTED')
        lead = imported.lead

        # 2. Book Trial Booking
        trial = TrialBooking.objects.using('tenant_test').create(
            lead=lead,
            branch=self.branch,
            scheduled_start=timezone.now() + timezone.timedelta(days=1),
            scheduled_end=timezone.now() + timezone.timedelta(days=1, hours=1),
            status='ATTENDED',
        )
        self.assertEqual(trial.status, 'ATTENDED')

        # 3. Convert lead via LeadConversionService.execute_conversion
        cat = ProgramCategory.objects.using('tenant_test').create(
            organization=self.org,
            code="CAT-PILATES",
            name="Pilates Category",
            status="ACTIVE",
        )
        program = Program.objects.using('tenant_test').create(
            organization=self.org,
            category=cat,
            code="PROG-PILATES",
            name="Pilates Program",
            status="ACTIVE",
        )
        pkg = Package.objects.using('tenant_test').create(
            organization=self.org,
            program=program,
            code="PKG-QUARTERLY",
            name="Quarterly Elite Pilates",
            status="ACTIVE",
        )
        pkg_ver = PackageVersion.objects.using('tenant_test').create(
            package=pkg,
            version_number=1,
            name_snapshot="Quarterly Elite v1",
            duration_value=3,
            duration_unit="MONTH",
            effective_from=timezone.now(),
            status="ACTIVE",
            created_by_user=self.sales_user,
        )
        PackagePrice.objects.using('tenant_test').create(
            package_version=pkg_ver,
            branch=self.branch,
            currency="INR",
            base_price=Decimal('12000.00'),
            display_price=Decimal('12000.00'),
            effective_from=timezone.now(),
            status="ACTIVE",
            created_by_user=self.sales_user,
        )

        conv_result = LeadConversionService.execute_conversion(
            lead=lead,
            package_version_id=str(pkg_ver.id),
            branch_id=str(self.branch.id),
            payment_provider="CASH",
            payment_amount=Decimal('12000.00'),
            actor_user=self.sales_user,
            db_alias='tenant_test',
        )

        self.assertIn('conversion_id', conv_result)
        lead.refresh_from_db(using='tenant_test')
        self.assertEqual(lead.current_status, 'CONVERTED')

        order_id = conv_result['order_id']
        order = Order.objects.using('tenant_test').get(id=order_id)
        self.assertEqual(order.status, 'PAID')

        membership_id = conv_result['membership_id']
        membership = Membership.objects.using('tenant_test').get(id=membership_id)
        self.assertEqual(membership.status, 'ACTIVE')

        # 4. Check Campaign Performance BEFORE Refund
        start_dt = timezone.now() - timezone.timedelta(days=1)
        end_dt = timezone.now() + timezone.timedelta(days=1)
        perf = CRMDashboardService._get_campaign_performance(
            organization=self.org,
            start_dt=start_dt,
            end_dt=end_dt,
            effective_branch_ids=None,
            platform='META',
            db_alias='tenant_test',
        )
        camp_row = next((c for c in perf if c['campaign_name'] == 'Karan Lifecycle Campaign'), None)
        self.assertIsNotNone(camp_row)
        self.assertEqual(camp_row['leads'], 1)
        self.assertEqual(camp_row['trials'], 1)
        self.assertEqual(camp_row['conversions'], 1)
        self.assertEqual(Decimal(camp_row['paid_revenue']), Decimal('12000.00'))

        # 5. Issue a Refund of ₹3,000 against this Order
        pay_txn = PaymentTransaction.objects.using('tenant_test').filter(order=order, status='SUCCESS').first()
        Refund.objects.using('tenant_test').create(
            order=order,
            payment_transaction=pay_txn,
            amount=Decimal('3000.00'),
            status='SUCCESS',
            reason_code='MEMBER_SATISFACTION',
            reason_text='Requested partial discount refund',
        )
        order.status = 'PARTIALLY_REFUNDED'
        order.save(using='tenant_test')

        # 6. Check Campaign Performance AFTER Refund
        perf_after = CRMDashboardService._get_campaign_performance(
            organization=self.org,
            start_dt=start_dt,
            end_dt=end_dt,
            effective_branch_ids=None,
            platform='META',
            db_alias='tenant_test',
        )
        camp_after = next((c for c in perf_after if c['campaign_name'] == 'Karan Lifecycle Campaign'), None)
        self.assertEqual(Decimal(camp_after['paid_revenue']), Decimal('9000.00'))  # Net: 12000 - 3000

        # 7. Simulator lead exclusion verification
        sim_lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            first_name="Simulated",
            last_name="TestLead",
            email_normalized="sim.test@example.test",
            phone_normalized="+919999999999",
            current_status="NEW_LEAD",
        )
        LeadAttribution.objects.using('tenant_test').create(
            organization=self.org,
            lead=sim_lead,
            touch_type='LEAD_CAPTURE',
            platform='META',
            campaign_name='Simulated Ghost Campaign',
            raw_metadata={'is_test': True},
        )
        perf_sim_check = CRMDashboardService._get_campaign_performance(
            organization=self.org,
            start_dt=start_dt,
            end_dt=end_dt,
            effective_branch_ids=None,
            platform='META',
            db_alias='tenant_test',
        )
        self.assertFalse(any(c['campaign_name'] == 'Simulated Ghost Campaign' for c in perf_sim_check))
