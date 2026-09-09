from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status

from apps.tenants.models import Tenant, Location, PlatformPlan
from apps.users.models import User, Role


class TenantOnboardingTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.super_user = User.objects.create_superuser(
            id='USR-SUPER',
            email='superadmin@performanceos.com',
            password='Password123!',
            role=Role.SUPER_ADMIN
        )
        self.client.force_authenticate(user=self.super_user)
        self.plan = PlatformPlan.objects.create(
            id='PLAN-GROWTH',
            name='Growth Pro',
            code='growth',
            price_monthly=19999.00
        )

    def test_atomic_tenant_onboarding(self):
        payload = {
            'brand_name': 'IronPeak Fitness',
            'slug': 'ironpeak',
            'tier': 'Growth',
            'plan_id': self.plan.id,
            'currency': 'INR',
            'timezone': 'Asia/Kolkata',
            'location_name': 'Indiranagar Flagship',
            'city': 'Bengaluru',
            'address': '100ft Road',
            'admin_first_name': 'Vikram',
            'admin_last_name': 'Malhotra',
            'admin_email': 'owner@ironpeak.com',
            'admin_phone': '+91 98765 43210',
            'admin_password': 'SecurePassword123!',
            'enabled_modules': ['crm', 'members', 'operations', 'finance']
        }

        response = self.client.post('/api/v1/platform/onboard/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('tenant', response.data)
        
        # Verify DB records
        tenant = Tenant.objects.filter(slug='ironpeak').first()
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.name, 'IronPeak Fitness')
        
        location = Location.objects.filter(tenant=tenant).first()
        self.assertIsNotNone(location)
        self.assertEqual(location.name, 'Indiranagar Flagship')
        
        admin_user = User.objects.filter(email='owner@ironpeak.com').first()
        self.assertIsNotNone(admin_user)
        self.assertEqual(admin_user.tenant, tenant)
        self.assertEqual(admin_user.role, Role.ADMIN)
        self.assertTrue(admin_user.check_password('SecurePassword123!'))
