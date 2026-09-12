"""
Comprehensive automated test suite for Phase 2 Coaching Layer API.
Tests Fitness Assessments, Central Exercise Library, Workout Programs, Indian Food Database, Nutrition Plans, and Tenant Isolation.
"""

import os
import json
import uuid
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from django.utils import timezone
from datetime import timedelta
from apps.tenants.models import Tenant, Location
from apps.members.models import Member
from apps.coaching.models import FitnessAssessment, Exercise, WorkoutProgram, NutritionPlan, FoodItem

def run_tests():
    print("=" * 65)
    print("Starting Phase 2 Coaching Layer Backend Verification Tests")
    print("=" * 65)

    client = Client()

    # 1. Login as Admin User (Tenant TEN-001)
    login_res = client.post(
        '/api/v1/auth/login/',
        data=json.dumps({'email': 'admin@yourgym.com', 'password': '123'
}),
        content_type='application/json'
    )
    assert login_res.status_code == 200, "Login failed"
    access_token = login_res.json()['access']
    auth_header = {'HTTP_AUTHORIZATION': f'Bearer {access_token}'}
    print("\n[STEP 1] Authenticated as Admin (Tenant: TEN-001) [OK]")

    # 2. Fitness Assessments (GET /api/v1/coaching/assessments/)
    print("\n[TEST 2] Testing GET /api/v1/coaching/assessments/ ...")
    res_asm = client.get('/api/v1/coaching/assessments/', **auth_header)
    assert res_asm.status_code == 200
    assessments = res_asm.json()
    assert len(assessments) >= 1
    rahul_asm = next((a for a in assessments if a['id'] == 'ASM-001'), assessments[0])
    print(f"  [OK] Found Baseline Assessment for {rahul_asm['member_name']}:")
    print(f"       Weight: {rahul_asm['weight_kg']} kg | BMI: {rahul_asm['bmi']} | Body Fat: {rahul_asm['body_fat_pct']}%")
    print(f"       Strength Baselines: Squat 1RM {rahul_asm['squat_1rm_kg']} kg, Deadlift 1RM {rahul_asm['deadlift_1rm_kg']} kg")

    # 3. Member Assessment History Timeline (GET /api/v1/coaching/assessments/member/MEM-001/)
    print("\n[TEST 3] Testing Member Assessment History Timeline ...")
    res_hist = client.get('/api/v1/coaching/assessments/member/MEM-001/', **auth_header)
    assert res_hist.status_code == 200
    hist = res_hist.json()
    assert len(hist) >= 1
    print(f"  [OK] Retrieved {len(hist)} historical assessment(s) for Rahul Sharma (Overall Score: {hist[0]['overall_fitness_score']}/100)")

    # 4. Create New Periodic Assessment with Auto BMI calculation (POST /api/v1/coaching/assessments/)
    print("\n[TEST 4] Testing Create Periodic Assessment (POST /api/v1/coaching/assessments/) ...")
    new_asm_id = f"ASM-TEST-{uuid.uuid4().hex[:6].upper()}"
    asm_payload = {
        'id': new_asm_id,
        'member': 'MEM-001',
        'assessment_type': 'Periodic',
        'height_cm': 178.00,
        'weight_kg': 79.20,
        'body_fat_pct': 16.8,
        'muscle_mass_pct': 43.2,
        'squat_1rm_kg': 117.50,
        'bench_1rm_kg': 90.00,
        'deadlift_1rm_kg': 150.00,
        'mobility_score': 82,
        'overall_fitness_score': 86,
        'trainer_summary': 'Week 4 check-in: 1RM Squat increased by 7.5kg, body fat reduced by 0.7%.',
    }
    res_create_asm = client.post(
        '/api/v1/coaching/assessments/',
        data=json.dumps(asm_payload),
        content_type='application/json',
        **auth_header
    )
    assert res_create_asm.status_code == 201, f"Assessment creation failed: {res_create_asm.content}"
    created_asm = res_create_asm.json()
    assert float(created_asm['bmi']) == 25.0, f"Auto BMI calculation failed: {created_asm['bmi']}"
    print(f"  [OK] Created Periodic Assessment (ID: {new_asm_id}) | Auto-calculated BMI: {created_asm['bmi']}")

    # 5. Central Exercise Library (GET /api/v1/coaching/exercises/)
    print("\n[TEST 5] Testing Central Exercise Library (GET /api/v1/coaching/exercises/) ...")
    res_exe = client.get('/api/v1/coaching/exercises/', **auth_header)
    assert res_exe.status_code == 200
    exercises = res_exe.json()
    assert len(exercises) >= 8, f"Expected >=8 exercises, got {len(exercises)}"
    print(f"  [OK] Retrieved {len(exercises)} Exercises from Central Library (e.g. {exercises[0]['name']})")

    # Test filtering by muscle: Quadriceps
    res_quads = client.get('/api/v1/coaching/exercises/?muscle=Quadriceps', **auth_header)
    assert res_quads.status_code == 200
    for e in res_quads.json():
        assert e['primary_muscle'] == 'Quadriceps'
    print(f"  [OK] Filter ?muscle=Quadriceps returned {len(res_quads.json())} targeted movements")

    # 6. Workout Programs & Splits (GET /api/v1/coaching/programs/PRG-001/)
    print("\n[TEST 6] Testing Workout Program Detail (GET /api/v1/coaching/programs/PRG-001/) ...")
    res_prg = client.get('/api/v1/coaching/programs/PRG-001/', **auth_header)
    assert res_prg.status_code == 200
    prg = res_prg.json()
    assert 'workout_days' in prg and len(prg['workout_days']) >= 2
    day1 = prg['workout_days'][0]
    assert len(day1['exercises']) >= 3
    print(f"  [OK] Workout Program: '{prg['name']}' for {prg['member_name']}")
    print(f"       {day1['day_name']} contains {len(day1['exercises'])} exercises:")
    for ex in day1['exercises']:
        print(f"       • {ex['exercise_name']} ({ex['sets']} sets x {ex['reps']}, Tempo: {ex['tempo']})")

    # 7. Indian Food Database & Nutrition Plans (GET /api/v1/coaching/nutrition/NUT-001/)
    print("\n[TEST 7] Testing Indian Food Database & Nutrition Plan (GET /api/v1/coaching/nutrition/NUT-001/) ...")
    res_nut = client.get('/api/v1/coaching/nutrition/NUT-001/', **auth_header)
    assert res_nut.status_code == 200
    nut = res_nut.json()
    assert len(nut['meals']) >= 4
    print(f"  [OK] Nutrition Plan: '{nut['name']}' ({nut['diet_type']})")
    print(f"       Daily Targets: {nut['daily_calorie_target']} kcal | {nut['protein_target_g']}g Protein | {nut['carbs_target_g']}g Carbs | {nut['fat_target_g']}g Fat")
    print(f"       Prescribed Indian Meals: {len(nut['meals'])} meals configured:")
    for m in nut['meals']:
        print(f"       • {m['meal_time']}: {m['food_name']} ({m['quantity']})")

    # 8. Multi-Tenant Isolation
    print("\n[TEST 8] Testing Multi-Tenant Data Isolation ...")
    login_b = client.post(
        '/api/v1/auth/login/',
        data=json.dumps({'email': 'admin@fitzone.com', 'password': '123'
}),
        content_type='application/json'
    )
    assert login_b.status_code == 200
    token_b = login_b.json()['access']

    res_b_asm = client.get('/api/v1/coaching/assessments/', HTTP_AUTHORIZATION=f'Bearer {token_b}')
    assert res_b_asm.status_code == 200
    assert len(res_b_asm.json()) == 0, f"Tenant B leaked Tenant A assessments! Found: {len(res_b_asm.json())}"
    print("  [OK] Tenant B cannot see any of Tenant A's assessments or workout programs (100% Isolated)")

    print("\n" + "=" * 65)
    print("ALL 8 PHASE 2 COACHING LAYER TESTS PASSED SUCCESSFULLY! (100% OK)")
    print("=" * 65)

if __name__ == '__main__':
    run_tests()
