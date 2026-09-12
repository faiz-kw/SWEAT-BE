from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from apps.tenants.models import Tenant, Location
from apps.users.models import User, Role, RoleDefinition, PermissionDefinition, RolePermission
from apps.administration.models import Service, TenantSettings, AuditLog, AuditAction


class AdministrationTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(
            id='TEN-TEST',
            name='Test Fitness Studio',
            slug='test-fitness'
        )
        self.location = Location.objects.create(
            id='LOC-TEST',
            tenant=self.tenant,
            name='Test Branch',
            city='Bengaluru'
        )
        self.admin_user = User.objects.create_user(
            id='USR-TEST-ADMIN',
            email='admin@testfitness.com',
            password='TestPassword123!',
            tenant=self.tenant,
            role=Role.ADMIN,
            active_location=self.location
        )
        self.client.force_authenticate(user=self.admin_user)

    def test_service_crud(self):
        payload = {
            'name': 'Reformer Pilates 1-on-1',
            'category': 'Pilates',
            'duration_minutes': 50,
            'price': '1500.00',
            'capacity': 1,
            'description': 'Core strength and postural realignment'
        }
        res = self.client.post('/api/v1/admin-config/services/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Service.objects.filter(tenant=self.tenant).count(), 1)

    def test_tenant_settings_update(self):
        payload = {
            'tax_rate_gst': 18.00,
            'booking_cancellation_window_hours': 8,
            'late_cancellation_fee': 300.00
        }
        res = self.client.put('/api/v1/admin-config/settings/current/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        settings_obj = TenantSettings.objects.get(tenant=self.tenant)
        self.assertEqual(settings_obj.booking_cancellation_window_hours, 8)

    def test_rbac_permission_update(self):
        role = RoleDefinition.objects.create(
            id='ROLE-TEST',
            name='Test Coach',
            code='test_coach',
            tenant=self.tenant
        )
        perm = PermissionDefinition.objects.create(
            id='PERM-TEST',
            module='CRM',
            action='view',
            label='View Test Leads'
        )
        payload = {
            'permissions': [
                {'permission_id': perm.id, 'granted': True}
            ]
        }
        res = self.client.post(f'/api/v1/users/roles/{role.id}/update-permissions/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rp = RolePermission.objects.filter(role=role, permission=perm).first()
        self.assertIsNotNone(rp)
        self.assertTrue(rp.granted)
