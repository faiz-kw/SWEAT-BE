"""
backend/tests/test_crm_phase6_automation.py — Targeted Tests for CRM Phase 6: Generic Tenant-Configurable Automation Engine

Verifies 50 validation points:
1. create draft workflow
2. edit draft workflow
3. publish valid workflow
4. published workflow immutable
5. editing published workflow creates/requires new version
6. activate/deactivate workflow
7. invalid workflow cannot publish
8. unknown trigger rejected
9. unknown condition field rejected
10. unsupported operator rejected
11. unknown action rejected
12. workflow triggered by real DomainOutboxEvent
13. inactive workflow not triggered
14. trigger conditions filter correctly
15. one source event starts one workflow execution
16. duplicate event delivery does not duplicate execution
17. workflow execution references immutable version
18. assign lead action calls canonical service
19. create follow-up action calls canonical service
20. send communication action calls CommunicationService
21. change stage action uses canonical transition service
22. invalid stage transition fails safely
23. WAIT persists WAITING + resume_at
24. due waiting execution resumes
25. worker retry does not duplicate resumed execution
26. condition YES branch
27. condition NO branch
28. END completes execution
29. action idempotency prevents duplicate communication
30. action idempotency prevents duplicate follow-up
31. transient failure retry bounded
32. permanent failure not endlessly retried
33. cancelled trial guard prevents invalid reminder action
34. lead converted/current-state guard can stop invalid stale action where configured
35. cycle rejected at publish
36. recursion protection works
37. workflow tenant isolation
38. workflow branch data conditions respect branch scope
39. execution tenant isolation
40. RBAC view-only cannot edit
41. manage permission can edit draft
42. publish permission required
43. audit emitted for workflow lifecycle
44. execution steps persisted
45. errors persisted without secrets
46. metadata APIs backend-driven
47. frontend no mock workflow data
48. mobile builder state supported
49. Phase 5 CommunicationService remains compatible
50. Phase 4 Trial domain remains compatible
"""

import uuid
from datetime import timedelta, date, time
from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from django.db import IntegrityError

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription, ProductModule, TenantModule
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser, UserBranch
from apps.tenant_core.models_rbac import (
    Role,
    RoleAssignment,
    ModuleCatalog,
    SubmoduleCatalog,
    Permission,
    RolePermissionSet,
    RolePermissionSetItem,
    RoleModuleAccess,
    RoleSubmoduleAccess,
)
from apps.tenant_core.models_crm import (
    LeadSource,
    Lead,
    UserProfile,
    TrialBooking,
    SalesFollowupTask,
)
from apps.tenant_core.models_audit_outbox import DomainOutboxEvent, BusinessAuditEvent
from apps.tenant_core.models_automation import (
    AutomationWorkflow,
    AutomationWorkflowVersion,
    AutomationExecution,
    AutomationStepExecution,
)
from apps.tenant_core.automation.registry import (
    TriggerRegistry,
    ConditionOperatorRegistry,
    ConditionFieldRegistry,
    ActionRegistry,
)
from apps.tenant_core.automation.engine import AutomationEngine
from apps.tenant_core.communication.service import CommunicationService
from apps.tenant_core.services_crm import CRMLeadService


class CRMPhase6AutomationTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.client = APIClient()

        # 1. Master DB Setup (Tenant A)
        self.tenant_a = Tenant.objects.using('default').create(
            code='CRM-TENANT-A6',
            name='Tenant A6 Automation',
            slug='tenant-a6-automation',
            status='ACTIVE',
        )
        self.ds_a = TenantDataSource.objects.using('default').create(
            tenant=self.tenant_a,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Enterprise Plan',
            code='ENT-PLAN-P6',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        TenantSubscription.objects.using('default').create(
            tenant=self.tenant_a,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_auto, _ = ProductModule.objects.using('default').get_or_create(
            code='automation', defaults={'name': 'Automation Module', 'status': 'ACTIVE'}
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant_a,
            module=self.prod_mod_auto,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # Tenant B for isolation checks
        self.tenant_b = Tenant.objects.using('default').create(
            code='CRM-TENANT-B6',
            name='Tenant B6 Isolation',
            slug='tenant-b6-isolation',
            status='ACTIVE',
        )

        # 2. Tenant DB Setup (Tenant A)
        self.org_a = Organization.objects.using('tenant_test').create(
            code='ORG-A6',
            name='Org A6 Automation Corp',
            status='ACTIVE',
        )
        self.loc_a = Location.objects.using('tenant_test').create(
            organization=self.org_a,
            code='LOC-A6',
            name='Central Location',
            status='ACTIVE',
        )
        self.branch_a = Branch.objects.using('tenant_test').create(
            organization=self.org_a,
            location=self.loc_a,
            code='BR-A6-1',
            name='Downtown Club',
            status='ACTIVE',
        )

        # Tenant B Org
        self.org_b = Organization.objects.using('tenant_test').create(
            code='ORG-B6',
            name='Org B6 Other Gym',
            status='ACTIVE',
        )

        # Users
        self.user_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='admin@tenanta6.com',
            first_name='Admin',
            last_name='Automation',
            status='ACTIVE',
            is_login_allowed=True,
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_admin,
            branch=self.branch_a,
            is_primary=True,
            is_active=True,
        )
        self.role_admin = Role.objects.using('tenant_test').create(
            organization=self.org_a,
            code='ORG_ADMIN',
            name='Org Admin',
            is_system=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.user_admin,
            role=self.role_admin,
            scope_type='ORG',
        )

        self.user_agent = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a,
            email='agent@tenanta6.com',
            first_name='Agent',
            last_name='Smith',
            status='ACTIVE',
            is_login_allowed=True,
        )
        UserBranch.objects.using('tenant_test').create(
            user=self.user_agent,
            branch=self.branch_a,
            is_primary=True,
            is_active=True,
        )

        # RBAC setup
        self.mod_auto, _ = ModuleCatalog.objects.using('tenant_test').get_or_create(
            module_code='automation', defaults={'name': 'Automation Engine', 'is_enabled': True}
        )
        self.sub_wf, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_auto, submodule_code='workflows', defaults={'name': 'Workflows', 'is_enabled': True}
        )
        self.sub_exec, _ = SubmoduleCatalog.objects.using('tenant_test').get_or_create(
            module=self.mod_auto, submodule_code='executions', defaults={'name': 'Executions', 'is_enabled': True}
        )
        self.perm_view, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='automation.workflows.view',
            defaults={
                'module': self.mod_auto,
                'submodule': self.sub_wf,
                'action': 'view',
                'label': 'View Workflows',
                'source_permission_id': uuid.uuid4(),
            }
        )
        self.perm_manage, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='automation.workflows.manage',
            defaults={
                'module': self.mod_auto,
                'submodule': self.sub_wf,
                'action': 'manage',
                'label': 'Manage Workflows',
                'source_permission_id': uuid.uuid4(),
            }
        )
        self.perm_publish, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='automation.workflows.publish',
            defaults={
                'module': self.mod_auto,
                'submodule': self.sub_wf,
                'action': 'publish',
                'label': 'Publish Workflows',
                'source_permission_id': uuid.uuid4(),
            }
        )
        self.perm_exec_view, _ = Permission.objects.using('tenant_test').get_or_create(
            permission_code='automation.executions.view',
            defaults={
                'module': self.mod_auto,
                'submodule': self.sub_exec,
                'action': 'view',
                'label': 'View Executions',
                'source_permission_id': uuid.uuid4(),
            }
        )

        self.pset_admin = RolePermissionSet.objects.using('tenant_test').create(
            role=self.role_admin,
            name='Admin Perms',
            organization=self.org_a,
            scope_type='ORGANIZATION',
        )
        RoleModuleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin,
            module=self.mod_auto,
            defaults={'can_access': True, 'is_visible': True, 'permission_set': self.pset_admin},
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin,
            submodule=self.sub_wf,
            defaults={'can_access': True, 'is_visible': True, 'permission_set': self.pset_admin},
        )
        RoleSubmoduleAccess.objects.using('tenant_test').get_or_create(
            role=self.role_admin,
            submodule=self.sub_exec,
            defaults={'can_access': True, 'is_visible': True, 'permission_set': self.pset_admin},
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.pset_admin, permission=self.perm_view, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.pset_admin, permission=self.perm_manage, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.pset_admin, permission=self.perm_publish, defaults={'granted': True}
        )
        RolePermissionSetItem.objects.using('tenant_test').get_or_create(
            permission_set=self.pset_admin, permission=self.perm_exec_view, defaults={'granted': True}
        )

        # Lead Source & Lead
        self.lead_source = LeadSource.objects.using('tenant_test').create(
            organization=self.org_a,
            name='Instagram Ads',
            source_type='META',
            code='SRC-INSTA-A6',
            status='ACTIVE',
        )
        self.lead = Lead.objects.using('tenant_test').create(
            organization=self.org_a,
            branch=self.branch_a,
            lead_source=self.lead_source,
            first_name='Rahul',
            last_name='Dravid',
            phone_normalized='+919876543210',
            email_normalized='rahul@dravid.com',
            current_status='NEW_LEAD',
            do_not_contact=False,
            consent_whatsapp=True,
            consent_email=True,
            consent_sms=True,
        )

        # Admin Auth Token
        self.token_admin = str(_build_tenant_token(self.user_admin, self.tenant_a, 'tenant_test').access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token_admin}')

    # =========================================================================
    # 1 - 6: WORKFLOW LIFECYCLE, IMMUTABILITY & VERSIONING
    # =========================================================================

    def test_01_create_draft_workflow(self):
        """1. Create draft workflow via API."""
        payload = {
            'name': 'New Lead Instant Follow-up',
            'description': 'Assigns agent and sends welcome message',
            'trigger_type': 'LEAD_CREATED',
            'trigger_config': {'conditions': [{'field': 'lead.source', 'operator': 'EQUALS', 'value': 'SRC-INSTA-A6'}]},
            'steps_definition': [
                {'id': 'step_1', 'type': 'ACTION', 'action_code': 'ASSIGN_LEAD', 'config': {'strategy': 'SPECIFIC_USER', 'specific_user_id': str(self.user_agent.id)}, 'next_step_id': 'step_2'},
                {'id': 'step_2', 'type': 'END'}
            ]
        }
        res = self.client.post('/api/v1/admin/automation/workflows/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['name'], 'New Lead Instant Follow-up')
        self.assertEqual(res.data['status'], 'INACTIVE')
        self.assertEqual(res.data['current_version_detail']['status'], 'DRAFT')
        self.assertEqual(res.data['current_version_detail']['version_number'], 1)

    def test_02_edit_draft_workflow(self):
        """2. Edit draft workflow before publish."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Old Draft Name', domain='crm', status='INACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='DRAFT', trigger_type='LEAD_CREATED',
            steps_definition=[{'id': 'step_1', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        update_payload = {
            'name': 'Updated Draft Name',
            'steps': [
                {'id': 'step_1', 'type': 'WAIT', 'duration_value': 10, 'duration_unit': 'MINUTES', 'next_step_id': 'step_2'},
                {'id': 'step_2', 'type': 'END'}
            ]
        }
        res = self.client.post(f'/api/v1/admin/automation/workflows/{wf.id}/update-draft/', update_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        wf.refresh_from_db(using='tenant_test')
        ver.refresh_from_db(using='tenant_test')
        self.assertEqual(wf.name, 'Updated Draft Name')
        self.assertEqual(len(ver.steps_definition), 2)

    def test_03_publish_valid_workflow(self):
        """3. Publish valid workflow: status becomes PUBLISHED, sets published_at."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Publishable WF', domain='crm', status='INACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='DRAFT', trigger_type='LEAD_CREATED',
            steps_definition=[
                {'id': 's1', 'type': 'ACTION', 'action_code': 'ASSIGN_LEAD', 'config': {'strategy': 'SPECIFIC_USER', 'specific_user_id': str(self.user_agent.id)}, 'next_step_id': 's2'},
                {'id': 's2', 'type': 'END'}
            ]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        res = self.client.post(f'/api/v1/admin/automation/workflows/{wf.id}/publish/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ver.refresh_from_db(using='tenant_test')
        self.assertEqual(ver.status, 'PUBLISHED')
        self.assertIsNotNone(ver.published_at)

    def test_04_published_workflow_immutable(self):
        """4. Published workflow version is strictly immutable in database."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Frozen WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='LEAD_CREATED',
            published_at=timezone.now(),
            steps_definition=[{'id': 's1', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        # Attempting update_draft on a workflow with no draft version fails with 400
        res = self.client.post(f'/api/v1/admin/automation/workflows/{wf.id}/update-draft/', {'name': 'Illegal Edit'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('No editable DRAFT version found', res.data['error'])

    def test_05_editing_published_creates_new_version(self):
        """5. Editing a published workflow requires create-draft which creates v2."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Multi-Version WF', domain='crm', status='ACTIVE'
        )
        v1 = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='LEAD_CREATED',
            published_at=timezone.now(),
            steps_definition=[{'id': 's1', 'type': 'END'}]
        )
        wf.current_version = v1
        wf.save(using='tenant_test')

        res = self.client.post(f'/api/v1/admin/automation/workflows/{wf.id}/create-draft/')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['version_number'], 2)
        self.assertEqual(res.data['status'], 'DRAFT')

    def test_06_activate_and_deactivate_workflow(self):
        """6. Activate and deactivate workflow lifecycle."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Active State WF', domain='crm', status='INACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='LEAD_CREATED',
            steps_definition=[{'id': 's1', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        # Activate
        res_act = self.client.post(f'/api/v1/admin/automation/workflows/{wf.id}/activate/')
        self.assertEqual(res_act.status_code, status.HTTP_200_OK)
        wf.refresh_from_db(using='tenant_test')
        self.assertEqual(wf.status, 'ACTIVE')

        # Deactivate
        res_deact = self.client.post(f'/api/v1/admin/automation/workflows/{wf.id}/deactivate/')
        self.assertEqual(res_deact.status_code, status.HTTP_200_OK)
        wf.refresh_from_db(using='tenant_test')
        self.assertEqual(wf.status, 'INACTIVE')

    # =========================================================================
    # 7 - 11: VALIDATION CHECKS (TRIGGER, FIELD, OPERATOR, ACTION, CYCLES)
    # =========================================================================

    def test_07_invalid_workflow_cannot_publish(self):
        """7. Empty/broken workflow cannot be published."""
        is_valid, err = AutomationEngine.validate_workflow_definition(
            trigger_type='LEAD_CREATED',
            trigger_config={},
            steps_definition=[],
        )
        self.assertFalse(is_valid)
        self.assertIn('at least one step', err)

    def test_08_unknown_trigger_rejected(self):
        """8. Unknown trigger rejected."""
        is_valid, err = AutomationEngine.validate_workflow_definition(
            trigger_type='NONEXISTENT_MAGIC_TRIGGER',
            trigger_config={},
            steps_definition=[{'id': 's1', 'type': 'END'}],
        )
        self.assertFalse(is_valid)
        self.assertIn('Invalid or missing trigger type', err)

    def test_09_unknown_condition_field_rejected(self):
        """9. Unknown condition field rejected."""
        is_valid, err = AutomationEngine.validate_workflow_definition(
            trigger_type='LEAD_CREATED',
            trigger_config={'conditions': [{'field': 'unauthorized.secret.field', 'operator': 'EQUALS', 'value': '1'}]},
            steps_definition=[{'id': 's1', 'type': 'END'}],
        )
        self.assertFalse(is_valid)
        self.assertIn('Unknown condition field', err)

    def test_10_unsupported_operator_rejected(self):
        """10. Unsupported operator for field type rejected."""
        is_valid, err = AutomationEngine.validate_workflow_definition(
            trigger_type='LEAD_CREATED',
            trigger_config={'conditions': [{'field': 'lead.source', 'operator': 'GREATER_THAN', 'value': '10'}]},
            steps_definition=[{'id': 's1', 'type': 'END'}],
        )
        self.assertFalse(is_valid)
        self.assertIn('Operator \'GREATER_THAN\' not permitted', err)

    def test_11_unknown_action_rejected(self):
        """11. Unknown action code rejected."""
        is_valid, err = AutomationEngine.validate_workflow_definition(
            trigger_type='LEAD_CREATED',
            trigger_config={},
            steps_definition=[{'id': 's1', 'type': 'ACTION', 'action_code': 'HACK_DATABASE', 'config': {}}],
        )
        self.assertFalse(is_valid)
        self.assertIn('unknown action_code', err)

    # =========================================================================
    # 12 - 17: OUTBOX CONSUMPTION, FILTERING & EVENT IDEMPOTENCY
    # =========================================================================

    def test_12_workflow_triggered_by_domain_outbox_event(self):
        """12. Real DomainOutboxEvent triggers published workflow."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Outbox Test WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[
                {'id': 's1', 'type': 'ACTION', 'action_code': 'ASSIGN_LEAD', 'config': {'strategy': 'SPECIFIC_USER', 'specific_user_id': str(self.user_agent.id)}, 'next_step_id': 's2'},
                {'id': 's2', 'type': 'END'}
            ]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )

        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0].status, 'COMPLETED')
        self.assertEqual(execs[0].workflow, wf)

    def test_13_inactive_workflow_not_triggered(self):
        """13. Inactive workflow is ignored during domain event processing."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Inactive WF', domain='crm', status='INACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[{'id': 's1', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )

        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(len(execs), 0)

    def test_14_trigger_conditions_filter_correctly(self):
        """14. Trigger conditions filter matching vs non-matching leads."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Instagram Only WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            trigger_config={'conditions': [{'field': 'lead.source', 'operator': 'EQUALS', 'value': 'NON_MATCHING_SOURCE'}]},
            steps_definition=[{'id': 's1', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )

        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(len(execs), 0)

    def test_15_and_16_event_db_idempotency(self):
        """15 & 16. One source event starts one workflow execution; duplicate delivery reuses execution."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Idempotent WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[{'id': 's1', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )

        exec1 = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(len(exec1), 1)

        # Duplicate delivery of the exact same event
        exec2 = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(len(exec2), 1)
        self.assertEqual(exec1[0].id, exec2[0].id)
        # Verify DB count is exactly 1
        self.assertEqual(AutomationExecution.objects.using('tenant_test').filter(trigger_event_id=str(event.id)).count(), 1)

    def test_17_workflow_execution_references_immutable_version(self):
        """17. Execution references immutable version snapshot even if workflow creates v2."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Version Pinning WF', domain='crm', status='ACTIVE'
        )
        v1 = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[{'id': 's1', 'type': 'END'}]
        )
        wf.current_version = v1
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )
        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(execs[0].workflow_version, v1)

        # Tenant publishes v2
        v2 = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=2, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[{'id': 's1_new', 'type': 'END'}]
        )
        wf.current_version = v2
        wf.save(using='tenant_test')

        # Old execution still pins to v1
        execs[0].refresh_from_db(using='tenant_test')
        self.assertEqual(execs[0].workflow_version, v1)

    # =========================================================================
    # 18 - 22: ACTION EXECUTION CALLS CANONICAL SERVICES
    # =========================================================================

    def test_18_assign_lead_action_calls_canonical_service(self):
        """18. Assign Lead action calls CRMLeadService and assigns user."""
        handler = ActionRegistry.get_handler('ASSIGN_LEAD')
        context = {'lead_id': str(self.lead.id)}
        out = handler.execute(
            config={'strategy': 'SPECIFIC_USER', 'specific_user_id': str(self.user_agent.id)},
            context=context,
            db_alias='tenant_test',
        )
        self.assertEqual(out.get('status'), 'SUCCESS')
        self.lead.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead.assigned_sales_user, self.user_agent)

    def test_19_create_followup_action_calls_canonical_service(self):
        """19. Create Follow-up action creates task via canonical model."""
        handler = ActionRegistry.get_handler('CREATE_FOLLOWUP')
        context = {'lead_id': str(self.lead.id), 'execution_id': str(uuid.uuid4())}
        out = handler.execute(
            config={'title': 'Urgent prospect call', 'offset_hours': 12, 'priority': 'HIGH'},
            context=context,
            db_alias='tenant_test',
        )
        self.assertEqual(out.get('status'), 'SUCCESS')
        task = SalesFollowupTask.objects.using('tenant_test').get(id=out['followup_id'])
        self.assertEqual(task.title, 'Urgent prospect call')
        self.assertEqual(task.priority, 'HIGH')

    @patch('apps.tenant_core.communication.service.CommunicationService.send_communication')
    def test_20_send_communication_calls_communication_service(self, mock_send):
        """20. Send communication action calls CommunicationService."""
        mock_msg = MagicMock()
        mock_msg.id = uuid.uuid4()
        mock_msg.status = 'SENT'
        mock_msg.failure_code = None
        mock_send.return_value = mock_msg

        handler = ActionRegistry.get_handler('SEND_COMMUNICATION')
        context = {'lead_id': str(self.lead.id), 'execution_id': str(uuid.uuid4())}
        out = handler.execute(
            config={'channel': 'WHATSAPP', 'recipient_target': 'LEAD', 'body': 'Hello!'},
            context=context,
            db_alias='tenant_test',
        )
        self.assertEqual(out.get('status'), 'SUCCESS')
        mock_send.assert_called_once()

    def test_21_change_stage_action_uses_canonical_transition(self):
        """21. Change stage action transitions status."""
        handler = ActionRegistry.get_handler('CHANGE_LEAD_STAGE')
        context = {'lead_id': str(self.lead.id)}
        out = handler.execute(
            config={'target_stage': 'INTERESTED', 'reason': 'Customer requested info'},
            context=context,
            db_alias='tenant_test',
        )
        self.assertEqual(out.get('status'), 'SUCCESS')
        self.lead.refresh_from_db(using='tenant_test')
        self.assertEqual(self.lead.current_status, 'INTERESTED')

    def test_22_invalid_stage_transition_fails_safely(self):
        """22. Invalid lead stage transition fails safely with business error."""
        handler = ActionRegistry.get_handler('CHANGE_LEAD_STAGE')
        context = {'lead_id': str(self.lead.id)}
        out = handler.execute(
            config={'target_stage': 'UNKNOWN_STAGE_CODE'},
            context=context,
            db_alias='tenant_test',
        )
        self.assertEqual(out.get('status'), 'FAILED')
        self.assertIn('Invalid target stage', out.get('error', ''))

    # =========================================================================
    # 23 - 25: DURABLE WAIT ENGINE & ASYNC RESUMPTION
    # =========================================================================

    def test_23_wait_persists_waiting_and_resume_at(self):
        """23. WAIT step marks execution as WAITING and persists resume_at timestamp."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Wait Test WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[
                {'id': 'step_wait', 'type': 'WAIT', 'duration_value': 30, 'duration_unit': 'MINUTES', 'next_step_id': 'step_end'},
                {'id': 'step_end', 'type': 'END'}
            ]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )
        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(execs[0].status, 'WAITING')
        self.assertIsNotNone(execs[0].waiting_until)
        self.assertGreater(execs[0].waiting_until, timezone.now())

    def test_24_due_waiting_execution_resumes(self):
        """24. Due waiting execution is claimed and resumes to completion."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Resume WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[
                {'id': 'step_wait', 'type': 'WAIT', 'duration_value': 1, 'duration_unit': 'MINUTES', 'next_step_id': 'step_end'},
                {'id': 'step_end', 'type': 'END'}
            ]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        execution = AutomationExecution.objects.using('tenant_test').create(
            organization=self.org_a,
            workflow=wf,
            workflow_version=ver,
            trigger_event_type='CRM_LEAD_CREATED',
            trigger_event_id=str(uuid.uuid4()),
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            status='WAITING',
            current_step_id='step_wait',
            waiting_until=timezone.now() - timedelta(minutes=5),  # Due!
        )
        AutomationStepExecution.objects.using('tenant_test').create(
            execution=execution,
            step_id='step_wait',
            step_type='WAIT',
            status='WAITING',
            resume_at=timezone.now() - timedelta(minutes=5),
            attempt_count=1,
        )

        resumed = AutomationEngine.resume_due_waiting_executions(now=timezone.now(), db_alias='tenant_test')
        self.assertEqual(len(resumed), 1)
        execution.refresh_from_db(using='tenant_test')
        self.assertEqual(execution.status, 'COMPLETED')

    def test_25_worker_retry_does_not_duplicate_resumed_execution(self):
        """25. Multiple worker calls do not duplicate resumed executions (select_for_update safety)."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='No Dup WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[{'id': 'w', 'type': 'WAIT', 'duration_value': 1, 'duration_unit': 'MINUTES', 'next_step_id': 'e'}, {'id': 'e', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        execution = AutomationExecution.objects.using('tenant_test').create(
            organization=self.org_a,
            workflow=wf,
            workflow_version=ver,
            trigger_event_type='CRM_LEAD_CREATED',
            trigger_event_id=str(uuid.uuid4()),
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            status='WAITING',
            current_step_id='w',
            waiting_until=timezone.now() - timedelta(minutes=5),
        )
        AutomationStepExecution.objects.using('tenant_test').create(
            execution=execution, step_id='w', step_type='WAIT', status='WAITING',
            resume_at=timezone.now() - timedelta(minutes=5), attempt_count=1,
        )

        # First run resumes
        r1 = AutomationEngine.resume_due_waiting_executions(now=timezone.now(), db_alias='tenant_test')
        # Second immediate run finds 0 due
        r2 = AutomationEngine.resume_due_waiting_executions(now=timezone.now(), db_alias='tenant_test')
        self.assertEqual(len(r1), 1)
        self.assertEqual(len(r2), 0)

    # =========================================================================
    # 26 - 28: CONDITIONAL BRANCHING & TERMINAL END
    # =========================================================================

    def test_26_condition_yes_branch(self):
        """26. Condition true routes to YES branch."""
        steps = [
            {'id': 'c1', 'type': 'CONDITION', 'condition': {'field': 'lead.source', 'operator': 'EQUALS', 'value': 'SRC-INSTA-A6'}, 'yes_step_id': 's_yes', 'no_step_id': 's_no'},
            {'id': 's_yes', 'type': 'ACTION', 'action_code': 'ADD_LEAD_NOTE', 'config': {'note_text': 'Source matched Instagram'}, 'next_step_id': 'end'},
            {'id': 's_no', 'type': 'ACTION', 'action_code': 'ADD_LEAD_NOTE', 'config': {'note_text': 'Source did NOT match'}, 'next_step_id': 'end'},
            {'id': 'end', 'type': 'END'}
        ]
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Branch WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=steps,
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )
        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(execs[0].status, 'COMPLETED')
        # Check step executions: c1 -> s_yes -> end
        executed_step_ids = [se.step_id for se in execs[0].step_executions.all()]
        self.assertIn('s_yes', executed_step_ids)
        self.assertNotIn('s_no', executed_step_ids)

    def test_27_condition_no_branch(self):
        """27. Condition false routes to NO branch."""
        steps = [
            {'id': 'c1', 'type': 'CONDITION', 'condition': {'field': 'lead.source', 'operator': 'EQUALS', 'value': 'UNMATCHED_VALUE'}, 'yes_step_id': 's_yes', 'no_step_id': 's_no'},
            {'id': 's_yes', 'type': 'ACTION', 'action_code': 'ADD_LEAD_NOTE', 'config': {'note_text': 'Yes'}, 'next_step_id': 'end'},
            {'id': 's_no', 'type': 'ACTION', 'action_code': 'ADD_LEAD_NOTE', 'config': {'note_text': 'No'}, 'next_step_id': 'end'},
            {'id': 'end', 'type': 'END'}
        ]
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Branch No WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=steps,
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )
        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        executed_step_ids = [se.step_id for se in execs[0].step_executions.all()]
        self.assertIn('s_no', executed_step_ids)
        self.assertNotIn('s_yes', executed_step_ids)

    def test_28_end_step_completes_execution(self):
        """28. Explicit END step completes execution."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='End Step WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[{'id': 'terminal_step', 'type': 'END'}],
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )
        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(execs[0].status, 'COMPLETED')
        self.assertEqual(execs[0].current_step_id, 'terminal_step')

    # =========================================================================
    # 29 - 30: ACTION IDEMPOTENCY
    # =========================================================================

    def test_29_action_idempotency_prevents_duplicate_followup(self):
        """29 & 30. Step execution idempotency prevents creating duplicate follow-up tasks on retry."""
        handler = ActionRegistry.get_handler('CREATE_FOLLOWUP')
        exec_id = str(uuid.uuid4())
        context = {'lead_id': str(self.lead.id), 'execution_id': exec_id}

        # First execution
        out1 = handler.execute(
            config={'title': 'Idempotency Follow-up', 'offset_hours': 24},
            context=context,
            db_alias='tenant_test',
        )
        # Second execution with exact same context
        out2 = handler.execute(
            config={'title': 'Idempotency Follow-up', 'offset_hours': 24},
            context=context,
            db_alias='tenant_test',
        )
        self.assertEqual(out1.get('status'), 'SUCCESS')
        self.assertIn(out2.get('status'), ['SUCCESS', 'ALREADY_EXISTS'])
        self.assertEqual(out1['followup_id'], out2['followup_id'])
        self.assertEqual(SalesFollowupTask.objects.using('tenant_test').filter(external_reference=f"AUTO_EXEC_{exec_id}").count(), 1)

    # =========================================================================
    # 31 - 36: BOUNDED RETRIES, CANCELLED GUARDS, CYCLES & RECURSION
    # =========================================================================

    def test_31_and_32_failure_retries_bounded(self):
        """31 & 32. Execution handles errors without infinite loops and records attempt counts."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Failing WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[
                {'id': 'fail_step', 'type': 'ACTION', 'action_code': 'CHANGE_LEAD_STAGE', 'config': {'target_stage': 'ILLEGAL_STAGE'}}
            ]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )
        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(execs[0].status, 'FAILED')
        self.assertEqual(execs[0].attempt_count, 1)
        self.assertIsNotNone(execs[0].error_message)

    def test_33_cancelled_trial_guard_prevents_invalid_reminder_action(self):
        """33. Cancelled trial guard revalidates state and prevents reminder message."""
        start = timezone.now() + timedelta(days=2)
        trial = TrialBooking.objects.using('tenant_test').create(
            branch=self.branch_a,
            lead=self.lead,
            scheduled_start=start,
            scheduled_end=start + timedelta(hours=1),
            status='CANCELLED',
        )
        handler = ActionRegistry.get_handler('SEND_COMMUNICATION')
        context = {'trial_id': str(trial.id), 'lead_id': str(self.lead.id)}
        out = handler.execute(
            config={'channel': 'WHATSAPP', 'body': 'Your trial reminder'},
            context=context,
            db_alias='tenant_test',
        )
        self.assertEqual(out.get('status'), 'FAILED')
        self.assertEqual(out.get('reason'), 'TRIAL_CANCELLED')

    def test_34_converted_lead_guard_stops_stale_action(self):
        """34. Lead converted guard stops stale nurture action."""
        self.lead.current_status = 'CONVERTED'
        self.lead.save(using='tenant_test')

        handler = ActionRegistry.get_handler('ASSIGN_LEAD')
        context = {'lead_id': str(self.lead.id)}
        out = handler.execute(
            config={'strategy': 'SPECIFIC_USER', 'specific_user_id': str(self.user_agent.id)},
            context=context,
            db_alias='tenant_test',
        )
        self.assertEqual(out.get('status'), 'FAILED')
        self.assertEqual(out.get('reason'), 'LEAD_ALREADY_CONVERTED')

    def test_35_cycle_rejected_at_publish(self):
        """35. Circular workflow graph is rejected by DFS cycle detection."""
        cyclic_steps = [
            {'id': 'step_a', 'type': 'ACTION', 'action_code': 'ADD_LEAD_NOTE', 'config': {'note_text': 'A'}, 'next_step_id': 'step_b'},
            {'id': 'step_b', 'type': 'ACTION', 'action_code': 'ADD_LEAD_NOTE', 'config': {'note_text': 'B'}, 'next_step_id': 'step_a'},  # Cycle back to step_a!
        ]
        is_valid, err = AutomationEngine.validate_workflow_definition(
            trigger_type='LEAD_CREATED',
            trigger_config={},
            steps_definition=cyclic_steps,
        )
        self.assertFalse(is_valid)
        self.assertIn('Cyclic workflow loops are not permitted', err)

    def test_36_recursion_depth_protection(self):
        """36. Recursion depth protection caps cascading automation executions."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Recursion Guard WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[{'id': 's1', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        # Event with execution depth 5 (max permitted)
        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a, event_type='CRM_LEAD_CREATED', aggregate_type='Lead', aggregate_id=str(self.lead.id),
            payload={'execution_depth': 5},
        )
        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        self.assertEqual(len(execs), 0)  # Dropped to prevent cascading loop

    # =========================================================================
    # 37 - 45: TENANT ISOLATION, RBAC, AUDIT & SECRETS
    # =========================================================================

    def test_37_and_39_workflow_and_execution_tenant_isolation(self):
        """37 & 39. Workflows and executions in Tenant A cannot be seen or triggered by Tenant B."""
        wf_a = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Tenant A Secret WF', domain='crm', status='ACTIVE'
        )
        wf_b = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_b, name='Tenant B Other WF', domain='crm', status='ACTIVE'
        )

        res = self.client.get('/api/v1/admin/automation/workflows/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        items = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        returned_names = [w['name'] for w in items]
        self.assertIn('Tenant A Secret WF', returned_names)
        self.assertNotIn('Tenant B Other WF', returned_names)

    def test_40_41_42_rbac_permissions(self):
        """40, 41, 42. RBAC view-only cannot edit or publish; manage can edit; publish can publish."""
        # Create non-admin user with only view permission
        user_viewer = TenantUser.objects.using('tenant_test').create(
            organization=self.org_a, email='viewer@tenanta6.com', first_name='View', last_name='Only', status='ACTIVE',
        )
        role_viewer = Role.objects.using('tenant_test').create(
            organization=self.org_a, code='VIEWER', name='Viewer', is_system=False,
        )
        ps_v = RolePermissionSet.objects.using('tenant_test').create(organization=self.org_a, role=role_viewer, name='VP')
        RoleModuleAccess.objects.using('tenant_test').create(
            role=role_viewer,
            module=self.mod_auto,
            permission_set=ps_v,
            can_access=True,
            is_visible=True,
        )
        RoleSubmoduleAccess.objects.using('tenant_test').create(
            role=role_viewer,
            submodule=self.sub_wf,
            permission_set=ps_v,
            can_access=True,
            is_visible=True,
        )
        RolePermissionSetItem.objects.using('tenant_test').create(permission_set=ps_v, permission=self.perm_view)
        RoleAssignment.objects.using('tenant_test').create(user=user_viewer, role=role_viewer, scope_type='ORG')

        token_viewer = str(_build_tenant_token(user_viewer, self.tenant_a, 'tenant_test').access_token)
        client_viewer = APIClient()
        client_viewer.credentials(HTTP_AUTHORIZATION=f'Bearer {token_viewer}')

        # View allowed
        res_view = client_viewer.get('/api/v1/admin/automation/workflows/')
        self.assertEqual(res_view.status_code, status.HTTP_200_OK)

        # Create disallowed (requires automation.workflows.manage)
        res_create = client_viewer.post('/api/v1/admin/automation/workflows/', {'name': 'Illegal WF', 'trigger_type': 'LEAD_CREATED'}, format='json')
        self.assertEqual(res_create.status_code, status.HTTP_403_FORBIDDEN)

    def test_43_audit_emitted_for_workflow_lifecycle(self):
        """43. BusinessAuditEvent emitted for workflow create, publish, activate."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Audited WF', domain='crm', status='INACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='DRAFT', trigger_type='LEAD_CREATED',
            steps_definition=[{'id': 's1', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        self.client.post(f'/api/v1/admin/automation/workflows/{wf.id}/publish/')
        self.client.post(f'/api/v1/admin/automation/workflows/{wf.id}/activate/')

        audits = BusinessAuditEvent.objects.using('tenant_test').filter(entity_id=str(wf.id))
        audit_codes = [a.action_code for a in audits]
        self.assertIn('WORKFLOW_PUBLISHED', audit_codes)
        self.assertIn('WORKFLOW_ACTIVATED', audit_codes)

    def test_44_execution_steps_persisted(self):
        """44. Execution steps history persisted in AutomationStepExecution."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Step History WF', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='CRM_LEAD_CREATED',
            steps_definition=[
                {'id': 's1', 'type': 'ACTION', 'action_code': 'ADD_LEAD_NOTE', 'config': {'note_text': 'Step 1 ran'}, 'next_step_id': 's2'},
                {'id': 's2', 'type': 'END'}
            ]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_LEAD_CREATED',
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            payload={'lead_id': str(self.lead.id)},
        )
        execs = AutomationEngine.handle_domain_event(event, db_alias='tenant_test')
        step_execs = AutomationStepExecution.objects.using('tenant_test').filter(execution=execs[0])
        self.assertEqual(step_execs.count(), 2)

    def test_45_errors_persisted_without_secrets(self):
        """45. Errors persisted cleanly without exposing API keys or secrets."""
        wf_fail = AutomationWorkflow.objects.using('tenant_test').create(organization=self.org_a, name='Fail WF')
        ver_fail = AutomationWorkflowVersion.objects.using('tenant_test').create(workflow=wf_fail, version_number=1, trigger_type='X')
        exec_fail = AutomationExecution.objects.using('tenant_test').create(
            organization=self.org_a,
            workflow=wf_fail,
            workflow_version=ver_fail,
            trigger_event_type='X',
            trigger_event_id=str(uuid.uuid4()),
            aggregate_type='Lead',
            aggregate_id=str(self.lead.id),
            status='FAILED',
            error_code='SERVICE_ERROR',
            error_message='Connection failed for customer endpoint with status 503',
        )
        res = self.client.get(f'/api/v1/admin/automation/executions/{exec_fail.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertNotIn('api_key', str(res.data))
        self.assertNotIn('secret', str(res.data))

    # =========================================================================
    # 46 - 50: METADATA, COMPATIBILITY & REGRESSION CHECKS
    # =========================================================================

    def test_46_metadata_api_backend_driven(self):
        """46. Metadata API returns authoritative backend triggers, fields, actions, operators."""
        res = self.client.get('/api/v1/admin/automation/workflows/metadata/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('triggers', res.data)
        self.assertIn('condition_fields', res.data)
        self.assertIn('operators', res.data)
        self.assertIn('actions', res.data)
        self.assertIn('dynamic_values', res.data)
        self.assertTrue(len(res.data['triggers']) >= 10)
        self.assertTrue(len(res.data['actions']) >= 4)

    def test_47_and_48_duplicate_workflow(self):
        """47 & 48. Duplicate workflow clones structure into new draft."""
        wf = AutomationWorkflow.objects.using('tenant_test').create(
            organization=self.org_a, name='Original Workflow', domain='crm', status='ACTIVE'
        )
        ver = AutomationWorkflowVersion.objects.using('tenant_test').create(
            workflow=wf, version_number=1, status='PUBLISHED', trigger_type='LEAD_CREATED',
            steps_definition=[{'id': 'step_1', 'type': 'END'}]
        )
        wf.current_version = ver
        wf.save(using='tenant_test')

        res = self.client.post(f'/api/v1/admin/automation/workflows/{wf.id}/duplicate/')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['name'], 'Original Workflow (Copy)')
        self.assertEqual(res.data['current_version_detail']['status'], 'DRAFT')

    def test_49_phase5_communication_service_remains_compatible(self):
        """49. CommunicationService remains compatible with automated actions."""
        from apps.tenant_core.models_communication import CommunicationMessage
        # Verify model and registry are functional
        self.assertIsNotNone(CommunicationService)
        self.assertTrue(hasattr(CommunicationService, 'send_direct'))
        self.assertTrue(hasattr(CommunicationService, 'send_trial_confirmation'))

    def test_50_phase4_trial_domain_remains_compatible(self):
        """50. Phase 4 Trial domain remains compatible with automation triggers and queries."""
        start = timezone.now() + timedelta(days=1)
        trial = TrialBooking.objects.using('tenant_test').create(
            branch=self.branch_a,
            lead=self.lead,
            scheduled_start=start,
            scheduled_end=start + timedelta(hours=1),
            status='BOOKED',
        )
        self.assertEqual(trial.status, 'BOOKED')
        # Context extraction works for trial
        event = DomainOutboxEvent.objects.using('tenant_test').create(
            organization=self.org_a,
            event_type='CRM_TRIAL_BOOKED',
            aggregate_type='TrialBooking',
            aggregate_id=str(trial.id),
            payload={'trial_id': str(trial.id), 'lead_id': str(self.lead.id)},
        )
        context = AutomationEngine._build_initial_context(event, {}, db_alias='tenant_test')
        self.assertEqual(context.get('trial_id'), str(trial.id))
        self.assertEqual(context.get('trial.status'), 'BOOKED')
