"""
backend/tests/test_wellness_core.py — Automated Tests for SWEAT Nutrition MVP (Milestone 1)

Covers:
1. Portion calculation and serving math.
2. Historical nutrient snapshot immutability upon catalog mutation.
3. Server-side UserProfile scoping and member privacy isolation.
4. Multi-tenant boundary isolation (custom foods).
5. Idempotent catalog seeding without duplicates.
6. Water logging API & intraday aggregation.
7. Manual sleep tracking API & duration computation.
8. Daily summary contract & rollup against targets.
"""

import uuid
from decimal import Decimal
from datetime import date, datetime, time, timedelta
from unittest.mock import patch, MagicMock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from django.core.management import call_command
from rest_framework.test import APIClient
from rest_framework import status

from config.routers import set_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from apps.master.models_saas import SaasPlan, TenantSubscription
from apps.authentication.views import _build_tenant_token
from apps.tenant_core.models_org import Organization, Location, Branch
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_workforce import UserProfile
from apps.tenant_core.models_wellness import (
    FoodItem,
    FoodCategory,
    FoodProvenance,
    MealType,
    MealLog,
    MealLogItem,
    WaterLog,
    SleepLog,
    NutritionTarget,
)
from apps.tenant_core.models_audit_outbox import IdempotencyRecord



class WellnessCoreTests(TestCase):
    databases = '__all__'

    def setUp(self):
        super().setUp()
        self.alias = 'tenant_test'
        set_tenant_db_alias(self.alias)

        # 1. Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            code='SWEAT-WELLNESS-TEST',
            name='SWEAT Elite UAT',
            slug='sweat',
            status='ACTIVE',
        )
        self.ds = TenantDataSource.objects.using('default').create(
            tenant=self.tenant,
            db_name='test_fitness_tenant',
            database_name='test_fitness_tenant',
            status='ACTIVE',
            database_engine='POSTGRESQL',
        )
        self.plan = SaasPlan.objects.using('default').create(
            name='Wellness Test Plan',
            code='WELLNESS-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )

        # 2. Second Tenant for Tenant Isolation Verification
        self.tenant_b = Tenant.objects.using('default').create(
            code='OTHER-GYM',
            name='Other Gym',
            slug='other-gym',
            status='ACTIVE',
        )

        # 3. Tenant DB Org Setup
        self.org = Organization.objects.using(self.alias).create(
            code='SWEAT-ORG',
            name='SWEAT Studio',
            status='ACTIVE',
        )
        self.org_b = Organization.objects.using(self.alias).create(
            code='OTHER-ORG',
            name='Other Studio',
            status='ACTIVE',
        )
        self.loc = Location.objects.using(self.alias).create(
            organization=self.org,
            code='IND-BLR',
            name='Bangalore',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using(self.alias).create(
            organization=self.org,
            location=self.loc,
            code='BLR-KOR',
            name='Koramangala',
            status='ACTIVE',
        )

        # 4. Member A (Alice)
        self.user_alice = TenantUser.objects.using(self.alias).create(
            organization=self.org,
            email='alice@sweat.test',
            first_name='Alice',
            last_name='Member',
            home_branch=self.branch,
            status='ACTIVE',
        )
        self.user_alice.set_password('Pass123!')
        self.user_alice.save(using=self.alias)
        self.profile_alice = UserProfile.objects.using(self.alias).create(
            user=self.user_alice,
            member_number='SW-001',
            first_name_snapshot='Alice',
            last_name_snapshot='Member',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )

        # 5. Member B (Bob)
        self.user_bob = TenantUser.objects.using(self.alias).create(
            organization=self.org,
            email='bob@sweat.test',
            first_name='Bob',
            last_name='Member',
            home_branch=self.branch,
            status='ACTIVE',
        )
        self.user_bob.set_password('Pass123!')
        self.user_bob.save(using=self.alias)
        self.profile_bob = UserProfile.objects.using(self.alias).create(
            user=self.user_bob,
            member_number='SW-002',
            first_name_snapshot='Bob',
            last_name_snapshot='Member',
            preferred_branch=self.branch,
            member_status='ACTIVE',
        )

        # 6. Seed Foods for Alice and Bob
        call_command('seed_indian_food_catalog', database=self.alias)

        # API Clients with Tokens
        self.client_alice = APIClient()
        token_alice = str(_build_tenant_token(self.user_alice, self.tenant, self.alias).access_token)
        self.client_alice.credentials(HTTP_AUTHORIZATION=f'Bearer {token_alice}')

        self.client_bob = APIClient()
        token_bob = str(_build_tenant_token(self.user_bob, self.tenant, self.alias).access_token)
        self.client_bob.credentials(HTTP_AUTHORIZATION=f'Bearer {token_bob}')

    # -------------------------------------------------------------------------
    # TEST 1: Catalog Seeding & Mathematical Reconciliations
    # -------------------------------------------------------------------------
    def test_catalog_seeded_values_and_idempotency(self):
        """Verify the 25 items are seeded with exact mathematical values and idempotent re-runs."""
        foods = FoodItem.objects.using(self.alias).filter(is_system=True)
        self.assertEqual(foods.count(), 25)

        # Plain Dosa check
        dosa = foods.get(name='Plain Dosa')
        self.assertEqual(dosa.calories, Decimal('204.80'))
        self.assertEqual(dosa.protein_g, Decimal('5.50'))
        self.assertEqual(dosa.carbs_g, Decimal('35.10'))
        self.assertEqual(dosa.fat_g, Decimal('4.40'))
        self.assertEqual(dosa.fiber_g, Decimal('1.20'))
        self.assertEqual(dosa.provenance, FoodProvenance.RECIPE_ESTIMATE)
        self.assertFalse(dosa.is_nutritionist_verified)

        # Steamed Idli check
        idli = foods.get(name='Steamed Idli')
        self.assertEqual(idli.calories, Decimal('59.50'))
        self.assertEqual(idli.protein_g, Decimal('1.90'))
        self.assertEqual(idli.carbs_g, Decimal('12.40'))
        self.assertEqual(idli.fat_g, Decimal('0.20'))
        self.assertEqual(idli.fiber_g, Decimal('0.40'))
        self.assertEqual(idli.provenance, FoodProvenance.RECIPE_ESTIMATE)
        self.assertFalse(idli.is_nutritionist_verified)

        # Phulka check
        phulka = foods.get(name='Phulka / Plain Roti')
        self.assertEqual(phulka.calories, Decimal('102.00'))
        self.assertEqual(phulka.protein_g, Decimal('4.00'))
        self.assertEqual(phulka.carbs_g, Decimal('21.60'))
        self.assertEqual(phulka.fat_g, Decimal('0.80'))
        self.assertEqual(phulka.fiber_g, Decimal('3.20'))
        self.assertEqual(phulka.provenance, FoodProvenance.RECIPE_ESTIMATE)

        # USDA Direct item check (Egg, Whole, Boiled)
        egg = foods.get(name='Egg, Whole, Boiled')
        self.assertEqual(egg.provenance, FoodProvenance.USDA_DIRECT)
        self.assertEqual(egg.source_reference_id, 'FDC: 171287')

        # Idempotency: Re-run seed command
        call_command('seed_indian_food_catalog', database=self.alias)
        self.assertEqual(FoodItem.objects.using(self.alias).filter(is_system=True).count(), 25)

    # -------------------------------------------------------------------------
    # TEST 2: Portion Calculation & Meal Logging API
    # -------------------------------------------------------------------------
    def test_meal_logging_portion_math(self):
        """Verify portion scaling math when logging multiple items."""
        phulka = FoodItem.objects.using(self.alias).get(name='Phulka / Plain Roti')
        dal = FoodItem.objects.using(self.alias).get(name='Yellow Dal Tadka')

        payload = {
            'logged_date': '2026-10-20',
            'logged_time': '13:00:00',
            'meal_type': 'LUNCH',
            'notes': '2 phulkas and 1 katori dal',
            'items': [
                {
                    'food_item_id': str(phulka.id),
                    'quantity': 2.0,
                    'unit_name': 'piece',
                    'consumed_grams': 70.0,
                },
                {
                    'food_item_id': str(dal.id),
                    'quantity': 1.0,
                    'unit_name': 'katori',
                    'consumed_grams': 150.0,
                },
            ],
        }

        res = self.client_alice.post('/api/v1/mobile/wellness/meal-logs/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.data

        # 2 Phulkas: 102 * 2 = 204.0 kcal, 4.0 * 2 = 8.0g P
        # 1 Dal (150g): 179.2 kcal, 8.2g P
        # Total cals: 204.0 + 179.2 = 383.2 kcal
        self.assertAlmostEqual(float(data['total_calories']), 383.2, places=1)
        self.assertAlmostEqual(float(data['total_protein_g']), 16.2, places=1)
        self.assertEqual(len(data['items']), 2)

    # -------------------------------------------------------------------------
    # TEST 3: Nutrient Snapshot Immutability
    # -------------------------------------------------------------------------
    def test_nutrient_snapshot_immutability(self):
        """Logged MealLogItem snapshot values must never change even if FoodItem catalog values mutate."""
        phulka = FoodItem.objects.using(self.alias).get(name='Phulka / Plain Roti')

        payload = {
            'logged_date': '2026-10-20',
            'meal_type': 'DINNER',
            'items': [
                {
                    'food_item_id': str(phulka.id),
                    'quantity': 1.0,
                    'unit_name': 'piece',
                    'consumed_grams': 35.0,
                }
            ],
        }
        res = self.client_alice.post('/api/v1/mobile/wellness/meal-logs/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        meal_id = res.data['id']
        logged_item_id = res.data['items'][0]['id']

        # Mutate the catalog item in database
        phulka.calories = Decimal('999.00')
        phulka.protein_g = Decimal('88.00')
        phulka.save(using=self.alias)

        # Retrieve the meal log again
        res_get = self.client_alice.get(f'/api/v1/mobile/wellness/meal-logs/{meal_id}/')
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        item_data = res_get.data['items'][0]

        # Snapshot MUST remain 102 kcal and 4.0g P
        self.assertEqual(float(item_data['snapshot_calories']), 102.0)
        self.assertEqual(float(item_data['snapshot_protein_g']), 4.0)

    # -------------------------------------------------------------------------
    # TEST 4: Server-Side UserProfile Scoping & Privacy Enforcement
    # -------------------------------------------------------------------------
    def test_member_privacy_and_user_profile_scoping(self):
        """A member cannot access or mutate another member's wellness logs."""
        phulka = FoodItem.objects.using(self.alias).get(name='Phulka / Plain Roti')

        # Alice creates a meal log
        payload = {
            'logged_date': '2026-10-20',
            'meal_type': 'BREAKFAST',
            'items': [
                {
                    'food_item_id': str(phulka.id),
                    'quantity': 1.0,
                    'unit_name': 'piece',
                    'consumed_grams': 35.0,
                }
            ],
        }
        res_alice = self.client_alice.post('/api/v1/mobile/wellness/meal-logs/', payload, format='json')
        self.assertEqual(res_alice.status_code, status.HTTP_201_CREATED)
        alice_meal_id = res_alice.data['id']

        # Bob attempts to GET Alice's meal log -> 404
        res_bob_get = self.client_bob.get(f'/api/v1/mobile/wellness/meal-logs/{alice_meal_id}/')
        self.assertEqual(res_bob_get.status_code, status.HTTP_404_NOT_FOUND)

        # Bob attempts to PATCH Alice's meal log -> 404
        res_bob_patch = self.client_bob.patch(
            f'/api/v1/mobile/wellness/meal-logs/{alice_meal_id}/',
            {'notes': 'Hacked notes'},
            format='json'
        )
        self.assertEqual(res_bob_patch.status_code, status.HTTP_404_NOT_FOUND)

        # Bob attempts to DELETE Alice's meal log -> 404
        res_bob_del = self.client_bob.delete(f'/api/v1/mobile/wellness/meal-logs/{alice_meal_id}/')
        self.assertEqual(res_bob_del.status_code, status.HTTP_404_NOT_FOUND)

        # Alice's meal log still exists untouched
        self.assertTrue(MealLog.objects.using(self.alias).filter(id=alice_meal_id).exists())

    # -------------------------------------------------------------------------
    # TEST 5: Multi-Tenant Boundary Isolation (Custom Foods)
    # -------------------------------------------------------------------------
    def test_tenant_custom_food_isolation(self):
        """Custom food items created by Org B must never be visible to Org A."""
        # Create Org B custom food
        custom_food_b = FoodItem.objects.using(self.alias).create(
            organization=self.org_b,
            is_system=False,
            name='Secret Protein Shake',
            calories=Decimal('350.00'),
            protein_g=Decimal('40.00'),
            carbs_g=Decimal('10.00'),
            fat_g=Decimal('5.00'),
            fiber_g=Decimal('2.00'),
            provenance=FoodProvenance.CUSTOM_TENANT,
        )

        # Alice (Org A) searches for 'Secret Protein Shake'
        res = self.client_alice.get('/api/v1/mobile/wellness/foods/?q=Secret')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        # Must return 0 results
        self.assertEqual(res.data['count'], 0)

        # But system foods are visible
        res_sys = self.client_alice.get('/api/v1/mobile/wellness/foods/?q=Dosa')
        self.assertEqual(res_sys.status_code, status.HTTP_200_OK)
        self.assertGreater(res_sys.data['count'], 0)

    # -------------------------------------------------------------------------
    # TEST 6: Water Tracking API
    # -------------------------------------------------------------------------
    def test_water_logging_and_intraday_total(self):
        """Log water intake and verify daily total and deletion."""
        # 1. Log 500 ml
        res1 = self.client_alice.post('/api/v1/mobile/wellness/water-logs/', {
            'logged_date': '2026-10-20',
            'amount_ml': 500,
        }, format='json')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        water_id = res1.data['id']

        # 2. Log 250 ml
        res2 = self.client_alice.post('/api/v1/mobile/wellness/water-logs/', {
            'logged_date': '2026-10-20',
            'amount_ml': 250,
        }, format='json')
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)

        # 3. GET list for today
        res_list = self.client_alice.get('/api/v1/mobile/wellness/water-logs/?date=2026-10-20')
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        self.assertEqual(res_list.data['total_ml'], 750)
        self.assertEqual(res_list.data['count'], 2)

        # 4. Bob cannot delete Alice's water log
        res_bob_del = self.client_bob.delete(f'/api/v1/mobile/wellness/water-logs/{water_id}/')
        self.assertEqual(res_bob_del.status_code, status.HTTP_404_NOT_FOUND)

        # 5. Alice deletes 500 ml entry
        res_del = self.client_alice.delete(f'/api/v1/mobile/wellness/water-logs/{water_id}/')
        self.assertEqual(res_del.status_code, status.HTTP_200_OK)

        # Check total is now 250
        res_check = self.client_alice.get('/api/v1/mobile/wellness/water-logs/?date=2026-10-20')
        self.assertEqual(res_check.data['total_ml'], 250)

    # -------------------------------------------------------------------------
    # TEST 7: Sleep Tracking API & Duration Computation
    # -------------------------------------------------------------------------
    def test_manual_sleep_logging(self):
        """Log sleep entry, verify automated duration calculation and validation."""
        start = timezone.now() - timedelta(hours=8)
        end = timezone.now()

        payload = {
            'logged_date': '2026-10-20',
            'start_time': start.isoformat(),
            'end_time': end.isoformat(),
            'quality_rating': 4,
            'notes': 'Deep restorative sleep',
        }

        res = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['duration_minutes'], 480)
        self.assertEqual(res.data['quality_rating'], 4)

        # Invalid end before start
        bad_payload = {
            'logged_date': '2026-10-20',
            'start_time': end.isoformat(),
            'end_time': start.isoformat(),
        }
        res_bad = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', bad_payload, format='json')
        self.assertEqual(res_bad.status_code, status.HTTP_400_BAD_REQUEST)

    # -------------------------------------------------------------------------
    # TEST 8: Daily Summary API Contract
    # -------------------------------------------------------------------------
    def test_daily_summary_aggregation(self):
        """Verify daily summary rolls up targets, consumed nutrients, water and sleep."""
        # Custom target for Alice: 1800 kcal, 100g P
        self.client_alice.put('/api/v1/mobile/wellness/targets/', {
            'daily_calories': 1800.0,
            'protein_grams': 100.0,
            'water_ml': 2000,
            'sleep_target_minutes': 480,
        }, format='json')

        # Log Meal: 1 Dosa (204.8 kcal, 5.5g P)
        dosa = FoodItem.objects.using(self.alias).get(name='Plain Dosa')
        self.client_alice.post('/api/v1/mobile/wellness/meal-logs/', {
            'logged_date': '2026-10-20',
            'meal_type': 'BREAKFAST',
            'items': [{
                'food_item_id': str(dosa.id),
                'quantity': 1.0,
                'unit_name': 'piece',
                'consumed_grams': 80.0,
            }],
        }, format='json')

        # Log Water: 1000 ml
        self.client_alice.post('/api/v1/mobile/wellness/water-logs/', {
            'logged_date': '2026-10-20',
            'amount_ml': 1000,
        }, format='json')

        # Log Sleep: 420 mins (7h)
        start = timezone.now() - timedelta(hours=7)
        end = timezone.now()
        self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', {
            'logged_date': '2026-10-20',
            'start_time': start.isoformat(),
            'end_time': end.isoformat(),
            'duration_minutes': 420,
            'quality_rating': 5,
        }, format='json')

        # Request Daily Summary
        res_summary = self.client_alice.get('/api/v1/mobile/wellness/daily-summary/?date=2026-10-20')
        self.assertEqual(res_summary.status_code, status.HTTP_200_OK)
        data = res_summary.data

        # Check Targets
        self.assertEqual(data['targets']['calories'], 1800.0)
        self.assertEqual(data['targets']['protein_g'], 100.0)
        self.assertEqual(data['targets']['water_ml'], 2000)

        # Check Consumed
        self.assertAlmostEqual(data['consumed']['calories'], 204.8, places=1)
        self.assertAlmostEqual(data['consumed']['protein_g'], 5.5, places=1)

        # Check Remaining
        self.assertAlmostEqual(data['remaining']['calories'], 1800.0 - 204.8, places=1)
        self.assertAlmostEqual(data['remaining']['protein_g'], 100.0 - 5.5, places=1)

        # Check Water
        self.assertEqual(data['water']['consumed_ml'], 1000)
        self.assertEqual(data['water']['percentage'], 50.0)

        # Check Sleep
        self.assertEqual(data['sleep']['duration_minutes'], 420)
        self.assertEqual(data['sleep']['quality_rating'], 5)

        # Check Meals breakdown
        self.assertEqual(len(data['meals']), 1)
        self.assertEqual(data['meals'][0]['meal_type'], 'BREAKFAST')

    # =========================================================================
    # Milestone 1.1 Tests: Idempotency & Sleep Hardening
    # =========================================================================

    def test_meal_log_idempotency_replay_and_conflict(self):
        """
        Verify that:
        1. Initial POST with Idempotency-Key returns 201 Created.
        2. Retried POST with identical key & payload returns replayed 201 without creating duplicate rows.
        3. Reused key with different payload returns 409 Conflict.
        """
        food = FoodItem.objects.using(self.alias).filter(name__icontains='Khichdi').first()
        payload = {
            'logged_date': '2026-10-20',
            'meal_type': 'LUNCH',
            'notes': 'Post-workout khichdi',
            'items': [{
                'food_item_id': str(food.id),
                'quantity': 1.0,
                'unit_name': 'katori',
                'consumed_grams': 200.0,
            }],
        }

        # 1. Initial Request with Idempotency-Key
        headers = {'HTTP_IDEMPOTENCY_KEY': 'meal-key-001'}
        res1 = self.client_alice.post('/api/v1/mobile/wellness/meal-logs/', payload, format='json', **headers)
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        meal_id = res1.data['id']
        self.assertEqual(MealLog.objects.using(self.alias).filter(user=self.user_alice).count(), 1)
        self.assertEqual(MealLogItem.objects.using(self.alias).filter(meal_log_id=meal_id).count(), 1)

        # 2. Retried Request with SAME Key and IDENTICAL Payload
        res2 = self.client_alice.post('/api/v1/mobile/wellness/meal-logs/', payload, format='json', **headers)
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res2.data['id'], meal_id)
        # Verify no duplicate created in DB
        self.assertEqual(MealLog.objects.using(self.alias).filter(user=self.user_alice).count(), 1)
        self.assertEqual(MealLogItem.objects.using(self.alias).filter(meal_log_id=meal_id).count(), 1)

        # 3. Request with SAME Key but DIFFERENT Payload (e.g. DINNER instead of LUNCH)
        conflict_payload = dict(payload)
        conflict_payload['meal_type'] = 'DINNER'
        res3 = self.client_alice.post('/api/v1/mobile/wellness/meal-logs/', conflict_payload, format='json', **headers)
        self.assertEqual(res3.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('different payload', res3.data.get('error', '').lower())
        # Still exactly 1 meal in DB
        self.assertEqual(MealLog.objects.using(self.alias).filter(user=self.user_alice).count(), 1)

    def test_water_log_idempotency_replay_and_conflict(self):
        """
        Verify water logging idempotency prevents double-counting on network retry,
        and returns 409 Conflict when key is reused with different amount.
        """
        payload = {
            'logged_date': '2026-10-20',
            'amount_ml': 250,
        }
        headers = {'HTTP_IDEMPOTENCY_KEY': 'water-key-001'}

        # 1. Initial Request
        res1 = self.client_alice.post('/api/v1/mobile/wellness/water-logs/', payload, format='json', **headers)
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        water_id = res1.data['id']
        self.assertEqual(res1.data['amount_ml'], 250)
        self.assertEqual(WaterLog.objects.using(self.alias).filter(user=self.user_alice).count(), 1)

        # 2. Network Retry with SAME Key and IDENTICAL Payload
        res2 = self.client_alice.post('/api/v1/mobile/wellness/water-logs/', payload, format='json', **headers)
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res2.data['id'], water_id)
        # Total water in DB must still be 250 ml (NOT 500 ml)
        logs = WaterLog.objects.using(self.alias).filter(user=self.user_alice)
        self.assertEqual(logs.count(), 1)
        self.assertEqual(sum(l.amount_ml for l in logs), 250)

        # 3. Conflict: Same Key with DIFFERENT Payload (500 ml)
        conflict_payload = {'logged_date': '2026-10-20', 'amount_ml': 500}
        res3 = self.client_alice.post('/api/v1/mobile/wellness/water-logs/', conflict_payload, format='json', **headers)
        self.assertEqual(res3.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(WaterLog.objects.using(self.alias).filter(user=self.user_alice).count(), 1)

    def test_idempotency_user_and_endpoint_scoping(self):
        """
        Verify that:
        1. Two distinct members (Alice and Bob) can use the same key without conflict.
        2. The same key can be used across different endpoints (MealLog vs WaterLog) without conflict.
        """
        shared_key = 'idem-cross-scope-key'

        # Alice logs 300 ml water
        res_alice_water = self.client_alice.post(
            '/api/v1/mobile/wellness/water-logs/',
            {'logged_date': '2026-10-20', 'amount_ml': 300},
            format='json',
            HTTP_IDEMPOTENCY_KEY=shared_key
        )
        self.assertEqual(res_alice_water.status_code, status.HTTP_201_CREATED)

        # Bob logs 500 ml water using the EXACT SAME key -> must succeed (user scoped)
        res_bob_water = self.client_bob.post(
            '/api/v1/mobile/wellness/water-logs/',
            {'logged_date': '2026-10-20', 'amount_ml': 500},
            format='json',
            HTTP_IDEMPOTENCY_KEY=shared_key
        )
        self.assertEqual(res_bob_water.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(res_alice_water.data['id'], res_bob_water.data['id'])

        # Alice logs a meal using the EXACT SAME key -> must succeed (endpoint scoped)
        food = FoodItem.objects.using(self.alias).filter(name__icontains='Plain Dosa').first()
        res_alice_meal = self.client_alice.post(
            '/api/v1/mobile/wellness/meal-logs/',
            {
                'logged_date': '2026-10-20',
                'meal_type': 'BREAKFAST',
                'items': [{'food_item_id': str(food.id), 'quantity': 1.0, 'unit_name': 'piece', 'consumed_grams': 80.0}],
            },
            format='json',
            HTTP_IDEMPOTENCY_KEY=shared_key
        )
        self.assertEqual(res_alice_meal.status_code, status.HTTP_201_CREATED)

    def test_idempotency_concurrent_in_progress_conflict(self):
        """
        Verify that when a request is currently PROCESSING under a given key,
        a concurrent request with the same key returns 409 Conflict.
        """
        from apps.tenant_core.services_reliability import compute_request_hash

        key = 'concurrent-processing-key'
        scoped_key = f"u:{self.user_alice.id}:{key}"
        payload = {'logged_date': '2026-10-20', 'amount_ml': 200}
        req_hash = compute_request_hash(payload)

        # Simulate an in-flight request by manually creating a PROCESSING record with matching payload hash
        IdempotencyRecord.objects.using(self.alias).create(
            organization=self.org,
            operation_type='WELLNESS_WATER_LOG',
            idempotency_key=scoped_key,
            actor_user=self.user_alice,
            request_hash=req_hash,
            status='PROCESSING',
            expires_at=timezone.now() + timedelta(minutes=5),
        )

        res = self.client_alice.post(
            '/api/v1/mobile/wellness/water-logs/',
            payload,
            format='json',
            HTTP_IDEMPOTENCY_KEY=key
        )
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('currently processing', res.data.get('error', '').lower())


    def test_sleep_log_repeated_submission_and_upsert(self):
        """
        Verify that repeated submissions for the same member on the same date:
        1. Gracefully updates the existing daily sleep record instead of creating duplicates.
        2. Updates duration_minutes, start_time, end_time, quality_rating, and notes.
        3. Returns HTTP 200 on update (HTTP 201 on initial create).
        4. Exactly 1 SleepLog record exists for that user and date.
        """
        now = timezone.now()
        start1 = now - timedelta(hours=8)
        end1 = now

        # 1. Initial Submission for 2026-10-20
        res1 = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', {
            'logged_date': '2026-10-20',
            'start_time': start1.isoformat(),
            'end_time': end1.isoformat(),
            'duration_minutes': 480,
            'quality_rating': 3,
            'notes': 'Initial log',
        }, format='json')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        sleep_id = res1.data['id']
        self.assertEqual(res1.data['duration_minutes'], 480)
        self.assertEqual(res1.data['quality_rating'], 3)
        self.assertEqual(SleepLog.objects.using(self.alias).filter(user=self.user_alice, logged_date='2026-10-20').count(), 1)

        # 2. Repeated Submission for SAME date: corrected times and rating
        start2 = now - timedelta(hours=9)
        end2 = now
        res2 = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', {
            'logged_date': '2026-10-20',
            'start_time': start2.isoformat(),
            'end_time': end2.isoformat(),
            'duration_minutes': 540,
            'quality_rating': 5,
            'notes': 'Updated after reviewing smart band',
        }, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data['id'], sleep_id)
        self.assertEqual(res2.data['duration_minutes'], 540)
        self.assertEqual(res2.data['quality_rating'], 5)
        self.assertEqual(res2.data['notes'], 'Updated after reviewing smart band')

        # Verify only 1 record in DB
        self.assertEqual(SleepLog.objects.using(self.alias).filter(user=self.user_alice, logged_date='2026-10-20').count(), 1)

    def test_sleep_log_idempotency_retry_and_conflict(self):
        """
        Verify sleep logging idempotency with Idempotency-Key:
        1. Initial POST returns 201 Created.
        2. Exact retry returns 201 Created with cached response.
        3. Different payload with same key returns 409 Conflict.
        """
        now = timezone.now()
        start = now - timedelta(hours=8)
        end = now
        payload = {
            'logged_date': '2026-10-19',
            'start_time': start.isoformat(),
            'end_time': end.isoformat(),
            'duration_minutes': 480,
            'quality_rating': 4,
            'notes': 'Solid sleep',
        }
        headers = {'HTTP_IDEMPOTENCY_KEY': 'sleep-idem-key-01'}

        # Initial
        res1 = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', payload, format='json', **headers)
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        sleep_id = res1.data['id']

        # Retry with identical payload
        res2 = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', payload, format='json', **headers)
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res2.data['id'], sleep_id)
        self.assertEqual(SleepLog.objects.using(self.alias).filter(user=self.user_alice, logged_date='2026-10-19').count(), 1)

        # Conflict with altered payload
        conflict_payload = dict(payload)
        conflict_payload['quality_rating'] = 2
        res3 = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', conflict_payload, format='json', **headers)
        self.assertEqual(res3.status_code, status.HTTP_409_CONFLICT)

    def test_sleep_log_detail_get_patch_put_and_boundaries(self):
        """
        Verify SleepLogDetailMobileView:
        1. GET returns member's sleep log.
        2. PATCH updates specific fields (rating, notes).
        3. PUT updates full timing and recomputes duration.
        4. Cross-member access (Bob trying to view/edit Alice's record) returns 404.
        """
        start = timezone.now() - timedelta(hours=8)
        end = timezone.now()
        res_create = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', {
            'logged_date': '2026-10-18',
            'start_time': start.isoformat(),
            'end_time': end.isoformat(),
            'duration_minutes': 480,
            'quality_rating': 3,
            'notes': 'Normal night',
        }, format='json')
        self.assertEqual(res_create.status_code, status.HTTP_201_CREATED)
        sleep_id = res_create.data['id']

        # 1. GET Detail
        res_get = self.client_alice.get(f'/api/v1/mobile/wellness/sleep-logs/{sleep_id}/')
        self.assertEqual(res_get.status_code, status.HTTP_200_OK)
        self.assertEqual(res_get.data['id'], sleep_id)

        # 2. PATCH Update
        res_patch = self.client_alice.patch(f'/api/v1/mobile/wellness/sleep-logs/{sleep_id}/', {
            'quality_rating': 5,
            'notes': 'Felt amazing in the morning',
        }, format='json')
        self.assertEqual(res_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(res_patch.data['quality_rating'], 5)
        self.assertEqual(res_patch.data['notes'], 'Felt amazing in the morning')

        # 3. PUT Update
        new_start = timezone.now() - timedelta(hours=6)
        new_end = timezone.now()
        res_put = self.client_alice.put(f'/api/v1/mobile/wellness/sleep-logs/{sleep_id}/', {
            'start_time': new_start.isoformat(),
            'end_time': new_end.isoformat(),
            'quality_rating': 4,
        }, format='json')
        self.assertEqual(res_put.status_code, status.HTTP_200_OK)
        self.assertEqual(res_put.data['duration_minutes'], 360)
        self.assertEqual(res_put.data['quality_rating'], 4)

        # 4. Privacy: Bob attempts to read or edit Alice's sleep log
        res_bob_get = self.client_bob.get(f'/api/v1/mobile/wellness/sleep-logs/{sleep_id}/')
        self.assertEqual(res_bob_get.status_code, status.HTTP_404_NOT_FOUND)

        res_bob_patch = self.client_bob.patch(f'/api/v1/mobile/wellness/sleep-logs/{sleep_id}/', {'quality_rating': 1}, format='json')
        self.assertEqual(res_bob_patch.status_code, status.HTTP_404_NOT_FOUND)

    def test_sleep_and_logging_boundary_validations(self):
        """
        Verify validation rules and boundaries:
        1. end_time <= start_time -> 400 Bad Request.
        2. duration > 24 hours -> 400 Bad Request.
        3. Future dates beyond tomorrow -> 400 Bad Request.
        4. Quality rating outside 1..5 -> 400 Bad Request.
        """
        now = timezone.now()

        # 1. Inverted times (end_time before start_time)
        res_inverted = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', {
            'logged_date': '2026-10-20',
            'start_time': now.isoformat(),
            'end_time': (now - timedelta(hours=2)).isoformat(),
        }, format='json')
        self.assertEqual(res_inverted.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('end_time', res_inverted.data)

        # 2. Exceeding 24 hours (e.g. 26 hours)
        res_too_long = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', {
            'logged_date': '2026-10-20',
            'start_time': (now - timedelta(hours=26)).isoformat(),
            'end_time': now.isoformat(),
        }, format='json')
        self.assertEqual(res_too_long.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. Far future date (e.g. 10 days in future)
        future_date = (now.date() + timedelta(days=10)).isoformat()
        res_future_sleep = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', {
            'logged_date': future_date,
            'start_time': (now - timedelta(hours=7)).isoformat(),
            'end_time': now.isoformat(),
        }, format='json')
        self.assertEqual(res_future_sleep.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('logged_date', res_future_sleep.data)

        res_future_water = self.client_alice.post('/api/v1/mobile/wellness/water-logs/', {
            'logged_date': future_date,
            'amount_ml': 250,
        }, format='json')
        self.assertEqual(res_future_water.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('logged_date', res_future_water.data)

        # 4. Invalid quality rating (0 or 6)
        res_invalid_rating = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', {
            'logged_date': '2026-10-20',
            'start_time': (now - timedelta(hours=7)).isoformat(),
            'end_time': now.isoformat(),
            'quality_rating': 6,
        }, format='json')
        self.assertEqual(res_invalid_rating.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('quality_rating', res_invalid_rating.data)

    def test_concurrent_first_insert_sleep_race_recovery(self):
        """
        Regression test: When two distinct concurrent requests without an Idempotency-Key
        attempt to create a sleep log on the same date for the same member simultaneously,
        an IntegrityError on uq_sleep_log_user_date is caught, the inner savepoint is rolled back,
        and the winning record is locked and updated cleanly without a 500 error.
        """
        now = timezone.now()
        target_date = '2026-10-15'
        start = now - timedelta(hours=8)
        end = now

        from unittest.mock import patch
        from django.db import IntegrityError

        # 1. Winning record already committed in database by concurrent Thread A
        winning_record = SleepLog.objects.using(self.alias).create(
            organization=self.org,
            user=self.user_alice,
            profile=self.profile_alice,
            logged_date=target_date,
            start_time=start,
            end_time=end,
            duration_minutes=400,
            quality_rating=2,
            notes='From concurrent winner',
        )

        # 2. Simulate Thread B race condition:
        # First lookup returns None (simulating Thread B checking right before Thread A commits).
        # Thread B then proceeds to create(), which hits the real PostgreSQL UNIQUE constraint
        # 'uq_sleep_log_user_date'.
        # On attempt 2, the retry finds the real winning_record, locks it, and updates it cleanly.
        orig_using = SleepLog.objects.using
        attempt_tracker = {'count': 0}

        def mock_using_race(alias_arg):
            qs = orig_using(alias_arg)
            orig_first = qs.first
            def race_first(*args, **kwargs):
                attempt_tracker['count'] += 1
                if attempt_tracker['count'] == 1:
                    # Simulate first check misses the record
                    return None
                return orig_first(*args, **kwargs)
            qs.first = race_first
            return qs

        payload = {
            'logged_date': target_date,
            'start_time': start.isoformat(),
            'end_time': end.isoformat(),
            'duration_minutes': 480,
            'quality_rating': 4,
            'notes': 'Concurrent race update notes',
        }

        with patch.object(SleepLog.objects, 'using', side_effect=mock_using_race):
            # Send request without Idempotency-Key
            res = self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', payload, format='json')

            # Must recover cleanly without a 500 error, returning 200 OK
            self.assertEqual(res.status_code, status.HTTP_200_OK)
            self.assertEqual(res.data['notes'], 'Concurrent race update notes')
            self.assertEqual(res.data['quality_rating'], 4)
            self.assertEqual(res.data['duration_minutes'], 480)

        # Confirm exactly 1 record in database, updated with Thread B's payload
        qs = SleepLog.objects.using(self.alias).filter(user=self.user_alice, logged_date=target_date)
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().notes, 'Concurrent race update notes')

    def test_unrelated_integrity_error_not_suppressed(self):
        """
        Verify that unrelated database integrity errors (e.g. foreign key or check constraint violations)
        are NOT swallowed by the concurrency race handler and return 500 Internal Server Error.
        """
        now = timezone.now()
        target_date = '2026-10-14'
        start = now - timedelta(hours=7)
        end = now

        from unittest.mock import patch
        from django.db import IntegrityError

        orig_using = SleepLog.objects.using
        def mock_using_error(alias_arg):
            qs = orig_using(alias_arg)
            def err_create(*args, **kwargs):
                raise IntegrityError('insert or update on table "sleep_logs" violates foreign key constraint "sleep_logs_user_id_fk"')
            qs.create = err_create
            return qs

        with patch.object(SleepLog.objects, 'using', side_effect=mock_using_error):
            payload = {
                'logged_date': target_date,
                'start_time': start.isoformat(),
                'end_time': end.isoformat(),
                'duration_minutes': 420,
            }
            # Unrelated integrity error must not be suppressed; propagates as IntegrityError
            with self.assertRaises(IntegrityError):
                self.client_alice.post('/api/v1/mobile/wellness/sleep-logs/', payload, format='json')

    def test_idempotency_stuck_processing_lock_override(self):
        """
        Verify that if an IdempotencyRecord is stuck in 'PROCESSING' state due to a crashed worker
        (updated_at > 300 seconds ago), a retry with the same key safely overrides the dead lock
        and executes rather than returning 409 Conflict forever.
        """
        from apps.tenant_core.services_reliability import compute_request_hash
        now = timezone.now()
        stuck_time = now - timedelta(minutes=10)
        idem_key = 'stuck-test-key-999'
        scoped_key = f"u:{self.user_alice.id}:{idem_key}"

        payload = {
            'logged_date': '2026-10-16',
            'amount_ml': 500,
        }
        req_hash = compute_request_hash(payload)

        # Manually seed a stuck PROCESSING record with matching hash
        record = IdempotencyRecord.objects.using(self.alias).create(
            organization=self.org,
            operation_type='WELLNESS_WATER_LOG',
            idempotency_key=scoped_key,
            actor_user=self.user_alice,
            request_hash=req_hash,
            status='PROCESSING',
            expires_at=now + timedelta(hours=24),
        )
        # Update updated_at in the past
        IdempotencyRecord.objects.using(self.alias).filter(id=record.id).update(updated_at=stuck_time)

        # Send request with the same idempotency key and matching payload
        headers = {'HTTP_IDEMPOTENCY_KEY': idem_key}
        res = self.client_alice.post('/api/v1/mobile/wellness/water-logs/', payload, format='json', **headers)

        # Must succeed with 201 Created instead of 409 Conflict
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['amount_ml'], 500)

        # Verify record is now COMPLETED
        record.refresh_from_db(using=self.alias)
        self.assertEqual(record.status, 'COMPLETED')

    def test_barcode_lookup_local_hit(self):
        """FoodBarcodeMobileView: Returns local catalog food if barcode already exists."""
        item = FoodItem.objects.using(self.alias).create(
            organization=self.org,
            name="Amul Butter 100g",
            barcode="8901262010053",
            category=FoodCategory.DAIRY,
            serving_unit="g",
            serving_weight_grams=Decimal("100.00"),
            calories=Decimal("717.00"),
            protein_g=Decimal("0.50"),
            carbs_g=Decimal("0.00"),
            fat_g=Decimal("81.00"),
            provenance=FoodProvenance.CUSTOM_TENANT,
            is_active=True,
        )

        res = self.client_alice.get('/api/v1/mobile/wellness/foods/barcode/8901262010053/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['found'])
        self.assertEqual(res.data['source'], 'local_catalog')
        self.assertEqual(res.data['food_item']['name'], 'Amul Butter 100g')
        self.assertEqual(res.data['food_item']['barcode'], '8901262010053')

    @patch('apps.tenant_core.views_wellness.requests.get')
    def test_barcode_lookup_open_food_facts_success(self, mock_get):
        """FoodBarcodeMobileView: Queries OpenFoodFacts, saves to DB, and returns food item."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'status': 1,
            'product': {
                'product_name': 'Nutella Hazelnut Spread',
                'brands': 'Ferrero',
                'serving_size': '15g',
                'serving_quantity': '15',
                'nutriments': {
                    'energy-kcal_100g': 539,
                    'proteins_100g': 6.3,
                    'carbohydrates_100g': 57.5,
                    'fat_100g': 30.9,
                    'fiber_100g': 3.0,
                }
            }
        }
        mock_get.return_value = mock_response

        res = self.client_alice.get('/api/v1/mobile/wellness/foods/barcode/3017620422003/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['found'])
        self.assertEqual(res.data['source'], 'open_food_facts')
        self.assertEqual(res.data['food_item']['name'], 'Nutella Hazelnut Spread')
        self.assertEqual(Decimal(str(res.data['food_item']['calories'])), Decimal('539.00'))

        # Verify cached in tenant DB
        db_item = FoodItem.objects.using(self.alias).filter(barcode='3017620422003').first()
        self.assertIsNotNone(db_item)
        self.assertEqual(db_item.provenance, FoodProvenance.OPEN_FOOD_FACTS)

    @patch('apps.tenant_core.views_wellness.requests.get')
    def test_barcode_lookup_not_found(self, mock_get):
        """FoodBarcodeMobileView: Returns 404 when product is not in local DB or OpenFoodFacts."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'status': 0,
            'status_verbose': 'product not found'
        }
        mock_get.return_value = mock_response

        res = self.client_alice.get('/api/v1/mobile/wellness/foods/barcode/9999999999999/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(res.data['found'])

    def test_food_image_analysis_missing_image(self):
        """FoodImageAnalysisMobileView: Rejects request with missing image."""
        res = self.client_alice.post('/api/v1/mobile/wellness/foods/analyze-image/', {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('No image provided', res.data['error'])

    def test_food_image_analysis_mock_flow(self):
        """FoodImageAnalysisMobileView: Analyzes meal image, matches catalog items, and calculates totals."""
        payload = {
            'image_base64': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
            'mock': True,
        }
        res = self.client_alice.post('/api/v1/mobile/wellness/foods/analyze-image/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['status'], 'success')
        self.assertTrue(res.data['is_mock'])
        self.assertGreaterEqual(res.data['detected_count'], 1)
        self.assertIn('summary', res.data)
        self.assertGreater(res.data['summary']['total_calories'], 0)

        # Check that Roti matched with catalog item from seed catalog
        roti_match = next((item for item in res.data['detected_items'] if 'Roti' in item['food_name']), None)
        self.assertIsNotNone(roti_match)
        self.assertIsNotNone(roti_match['matched_catalog_item'])
        self.assertIn('Roti', roti_match['matched_catalog_item']['name'])

    @patch('apps.tenant_core.views_wellness._resolve_mobile_tenant_and_db')
    def test_food_image_analysis_production_rejects_mock_without_api_key(self, mock_resolve):
        """FoodImageAnalysisMobileView: In production without API key, mock=true cannot bypass and returns 503."""
        mock_resolve.return_value = ('tenant_prod_live', self.tenant)
        payload = {
            'image_base64': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
            'mock': True,
        }
        with self.settings(DEBUG=False, _IS_TEST=False, DEPLOYMENT_ENVIRONMENT='production', GEMINI_API_KEY=''):
            res = self.client_alice.post('/api/v1/mobile/wellness/foods/analyze-image/', payload, format='json')
            self.assertEqual(res.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
            self.assertIn('AI vision service is not configured', res.data['error'])

    @patch('apps.tenant_core.views_wellness._resolve_mobile_tenant_and_db')
    def test_food_image_analysis_production_rejects_mock_with_api_key(self, mock_resolve):
        """FoodImageAnalysisMobileView: In production with API key, client-requested mock=true is explicitly rejected (400)."""
        mock_resolve.return_value = ('tenant_prod_live', self.tenant)
        payload = {
            'image_base64': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
            'mock': True,
        }
        with self.settings(DEBUG=False, _IS_TEST=False, DEPLOYMENT_ENVIRONMENT='production', GEMINI_API_KEY='live_test_api_key'):
            res = self.client_alice.post('/api/v1/mobile/wellness/foods/analyze-image/', payload, format='json')
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn('Mock mode is disabled in production environments', res.data['error'])

    def test_food_image_analysis_throttle_rate_limiting(self):
        """FoodImageAnalysisMobileView: Enforces rate limiting per authenticated user and returns 429 when exceeded."""
        from apps.tenant_core.views_wellness import FoodImageAnalysisThrottle
        cache.clear()
        payload = {
            'image_base64': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
            'mock': True,
        }
        # Simulate rate limit of 2 requests per minute
        with patch.object(FoodImageAnalysisThrottle, 'get_rate', return_value='2/minute'):
            res1 = self.client_alice.post('/api/v1/mobile/wellness/foods/analyze-image/', payload, format='json')
            self.assertEqual(res1.status_code, status.HTTP_200_OK)

            res2 = self.client_alice.post('/api/v1/mobile/wellness/foods/analyze-image/', payload, format='json')
            self.assertEqual(res2.status_code, status.HTTP_200_OK)

            # 3rd request trips the 2/minute throttle
            res3 = self.client_alice.post('/api/v1/mobile/wellness/foods/analyze-image/', payload, format='json')
            self.assertEqual(res3.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            self.assertIn('throttled', res3.data['detail'].lower())
        cache.clear()



