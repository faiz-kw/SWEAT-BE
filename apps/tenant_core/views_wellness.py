"""
apps/tenant_core/views_wellness.py — Dedicated Mobile Wellness & Nutrition API Views for SWEAT

Endpoints:
1. GET  /api/v1/mobile/wellness/foods/ — Search local Indian food catalog & custom tenant foods.
2. GET  /api/v1/mobile/wellness/targets/ — Get member daily nutrition & wellness goals.
3. PUT  /api/v1/mobile/wellness/targets/ — Update member daily goals.
4. GET  /api/v1/mobile/wellness/meal-logs/ — List meals logged on a given date.
5. POST /api/v1/mobile/wellness/meal-logs/ — Create meal log with portion calculation & snapshot.
6. GET  /api/v1/mobile/wellness/meal-logs/<id>/ — Detail of a meal log.
7. PATCH /api/v1/mobile/wellness/meal-logs/<id>/ — Update notes or meal type.
8. DELETE /api/v1/mobile/wellness/meal-logs/<id>/ — Delete meal log.
9. GET  /api/v1/mobile/wellness/water-logs/ — List water logs for a date.
10. POST /api/v1/mobile/wellness/water-logs/ — Log water intake in ml.
11. DELETE /api/v1/mobile/wellness/water-logs/<id>/ — Remove water log entry.
12. GET  /api/v1/mobile/wellness/sleep-logs/ — List sleep logs for a date.
13. POST /api/v1/mobile/wellness/sleep-logs/ — Log manual sleep session.
14. DELETE /api/v1/mobile/wellness/sleep-logs/<id>/ — Remove sleep log entry.
15. GET  /api/v1/mobile/wellness/daily-summary/ — Consolidated daily nutrition, water & sleep rollup.
"""

import os
import base64
import json
import logging
from decimal import Decimal
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any

import requests
from django.conf import settings
from django.db import transaction, models, IntegrityError
from django.utils import timezone
from rest_framework import status, serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .views_mobile import _resolve_mobile_tenant_and_db, _get_or_create_user_profile
from .models_org import Organization
from .models_wellness import (
    FoodItem,
    FoodCategory,
    FoodProvenance,
    NutritionTarget,
    MealLog,
    MealLogItem,
    WaterLog,
    SleepLog,
    MealType,
)
from .serializers_wellness import (
    FoodItemSerializer,
    MealLogSerializer,
    CreateMealLogSerializer,
    WaterLogSerializer,
    CreateWaterLogSerializer,
    SleepLogSerializer,
    CreateSleepLogSerializer,
    UpdateSleepLogSerializer,
    NutritionTargetSerializer,
)
from .services_reliability import execute_idempotent_operation, IdempotencyConflictError

logger = logging.getLogger(__name__)


def _extract_idempotency_key(request) -> Optional[str]:
    """
    Extract Idempotency-Key from headers (Idempotency-Key, X-Idempotency-Key, HTTP_IDEMPOTENCY_KEY)
    or body payload. Strips whitespace.
    """
    key = None
    if hasattr(request, 'headers'):
        key = request.headers.get('Idempotency-Key') or request.headers.get('X-Idempotency-Key')
    if not key and hasattr(request, 'META'):
        key = request.META.get('HTTP_IDEMPOTENCY_KEY') or request.META.get('HTTP_X_IDEMPOTENCY_KEY')
    if not key and isinstance(getattr(request, 'data', None), dict):
        key = request.data.get('idempotency_key')
    if key:
        key = str(key).strip()
    return key or None


def _get_active_organization(request, alias):
    """Fetch the active Organization for the authenticated user or tenant DB."""
    user = getattr(request, 'user', None)
    if user and getattr(user, 'organization', None):
        return user.organization
    org = Organization.objects.using(alias).filter(status='ACTIVE').first()
    if not org:
        org = Organization.objects.using(alias).first()
    return org



class FoodSearchMobileView(APIView):
    """
    GET /api/v1/mobile/wellness/foods/
    Search and filter verified Indian food catalog and tenant-created custom foods.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        query = (request.query_params.get('q') or '').strip()
        category = (request.query_params.get('category') or '').strip().upper()

        # Scope: System foods OR current tenant's custom foods
        scope_q = models.Q(is_system=True)
        if org:
            scope_q |= models.Q(organization=org)

        qs = FoodItem.objects.using(alias).filter(is_active=True).filter(scope_q)

        if category:
            qs = qs.filter(category=category)

        if query:
            qs = qs.filter(
                models.Q(name__icontains=query) |
                models.Q(regional_aliases__icontains=query)
            )

        foods = qs.order_by('name')[:100]
        serializer = FoodItemSerializer(foods, many=True)
        return Response({
            'count': len(serializer.data),
            'results': serializer.data,
        })


class FoodBarcodeMobileView(APIView):
    """
    GET /api/v1/mobile/wellness/foods/barcode/<str:barcode>/
    Lookup a food item by its UPC/EAN barcode.
    Flow:
    1. Check local tenant/system FoodItem catalog by barcode.
    2. If not found locally, query OpenFoodFacts API (free, open crowdsourced database).
    3. If found on OpenFoodFacts, normalize nutritional fields (calories, protein, carbs, fat, fiber)
       per 100g and household serving sizes, persist into the tenant database, and return.
    4. If not found on OpenFoodFacts, return clean 404 prompting member to create custom food.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, barcode: str):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        clean_barcode = (barcode or '').strip()
        if not clean_barcode or len(clean_barcode) < 3 or len(clean_barcode) > 64:
            return Response({'error': 'Invalid barcode format.'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)

        # 1. Search local tenant / system catalog
        scope_q = models.Q(is_system=True)
        if org:
            scope_q |= models.Q(organization=org)

        local_item = FoodItem.objects.using(alias).filter(
            barcode=clean_barcode,
            is_active=True
        ).filter(scope_q).first()

        if local_item:
            return Response({
                'found': True,
                'source': 'local_catalog',
                'food_item': FoodItemSerializer(local_item).data,
            }, status=status.HTTP_200_OK)

        # 2. Query OpenFoodFacts API
        off_url = f"https://world.openfoodfacts.org/api/v0/product/{clean_barcode}.json"
        headers = {
            'User-Agent': 'SWEAT-FitnessApp/1.0 (dev@sweat.com)',
            'Accept': 'application/json',
        }

        try:
            resp = requests.get(off_url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 1 and 'product' in data:
                    product = data['product']
                    product_name = (
                        product.get('product_name') or
                        product.get('product_name_en') or
                        product.get('generic_name') or
                        f"Scanned Item ({clean_barcode})"
                    ).strip()

                    nutriments = product.get('nutriments', {})

                    # Extract kcal (OpenFoodFacts provides energy-kcal_100g, or energy_100g in kJ)
                    kcal = nutriments.get('energy-kcal_100g')
                    if kcal is None:
                        kcal = nutriments.get('energy-kcal')
                    if kcal is None and nutriments.get('energy-kj_100g'):
                        try:
                            kcal = float(nutriments['energy-kj_100g']) / 4.184
                        except (ValueError, TypeError):
                            kcal = 0.0

                    protein = nutriments.get('proteins_100g') or nutriments.get('proteins', 0.0)
                    carbs = nutriments.get('carbohydrates_100g') or nutriments.get('carbohydrates', 0.0)
                    fat = nutriments.get('fat_100g') or nutriments.get('fat', 0.0)
                    fiber = nutriments.get('fiber_100g') or nutriments.get('fiber', 0.0)

                    # Safe Decimal conversion
                    def _to_dec(val, dec_pl=2, default='0.00'):
                        try:
                            d = Decimal(str(val if val is not None else 0))
                            if d < 0:
                                d = Decimal('0.00')
                            return round(d, dec_pl)
                        except Exception:
                            return Decimal(default)

                    dec_calories = _to_dec(kcal, default='0.00')
                    dec_protein = _to_dec(protein, default='0.00')
                    dec_carbs = _to_dec(carbs, default='0.00')
                    dec_fat = _to_dec(fat, default='0.00')
                    dec_fiber = _to_dec(fiber, default='0.00')

                    # Household measures / serving size
                    serving_size_str = (product.get('serving_size') or '').strip()
                    serving_qty = product.get('serving_quantity')
                    household_measures = []
                    if serving_size_str:
                        serving_weight = 100.0
                        if serving_qty:
                            try:
                                serving_weight = float(serving_qty)
                            except (ValueError, TypeError):
                                pass
                        household_measures.append({
                            'name': f"1 serving ({serving_size_str})",
                            'weight_g': serving_weight,
                            'unit': 'serving',
                        })

                    # Persist newly discovered product in tenant database cache
                    new_item = FoodItem.objects.using(alias).create(
                        organization=org,
                        is_system=False,
                        name=product_name[:200],
                        barcode=clean_barcode,
                        category=FoodCategory.OTHER,
                        serving_unit='g',
                        serving_weight_grams=Decimal('100.00'),
                        household_measures=household_measures,
                        calories=dec_calories,
                        protein_g=dec_protein,
                        carbs_g=dec_carbs,
                        fat_g=dec_fat,
                        fiber_g=dec_fiber,
                        provenance=FoodProvenance.OPEN_FOOD_FACTS,
                        source_reference_id=f"OFF:{clean_barcode}",
                        preparation_notes=f"Auto-imported from OpenFoodFacts (brand: {product.get('brands', 'Unknown')})",
                        is_nutritionist_verified=False,
                        is_active=True,
                    )

                    return Response({
                        'found': True,
                        'source': 'open_food_facts',
                        'food_item': FoodItemSerializer(new_item).data,
                    }, status=status.HTTP_200_OK)

        except requests.RequestException as e:
            logger.warning(f"OpenFoodFacts API error for barcode {clean_barcode}: {e}")
            return Response({
                'found': False,
                'barcode': clean_barcode,
                'detail': 'External barcode lookup service temporarily unreachable. You can add this item manually.',
                'error': str(e),
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({
            'found': False,
            'barcode': clean_barcode,
            'detail': 'Product not found for this barcode. You can log it as a custom food item.',
        }, status=status.HTTP_404_NOT_FOUND)


class FoodImageAnalysisMobileView(APIView):
    """
    POST /api/v1/mobile/wellness/foods/analyze-image/
    AI-powered food photo recognition using Google Gemini 1.5 Flash Vision.

    Accepts:
    - Multipart file upload: `image`
    - OR JSON payload with base64 string: `image_base64` or `image`
    - Optional `mock=true` query param or JSON flag for offline/mock development.

    Returns:
    - Detected food items with estimated portions (grams) and nutrient breakdowns.
    - Automatic fuzzy cross-reference matching against the gym's verified FoodItem catalog.
    - Meal rollup totals (calories, protein, carbs, fat, fiber).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)

        # Check mock parameter (useful for testing or frontend UI development)
        is_mock = (
            request.query_params.get('mock', '').lower() in ('true', '1') or
            (isinstance(request.data, dict) and request.data.get('mock') in (True, 'true', '1'))
        )

        image_data = None
        mime_type = 'image/jpeg'

        # 1. Check for file upload
        if 'image' in request.FILES:
            uploaded_file = request.FILES['image']
            if uploaded_file.size > 10 * 1024 * 1024:
                return Response({'error': 'Image file exceeds maximum 10MB limit.'}, status=status.HTTP_400_BAD_REQUEST)
            content_type = uploaded_file.content_type or 'image/jpeg'
            if content_type in ('image/jpeg', 'image/png', 'image/webp', 'image/heic'):
                mime_type = content_type
            image_bytes = uploaded_file.read()
            image_data = base64.b64encode(image_bytes).decode('utf-8')

        # 2. Check for base64 in data
        elif isinstance(request.data, dict) and ('image_base64' in request.data or 'image' in request.data):
            raw_b64 = request.data.get('image_base64') or request.data.get('image')
            if isinstance(raw_b64, str):
                # Strip data URI prefix if present (e.g. data:image/jpeg;base64,...)
                if ',' in raw_b64:
                    header, raw_b64 = raw_b64.split(',', 1)
                    if 'image/png' in header:
                        mime_type = 'image/png'
                    elif 'image/webp' in header:
                        mime_type = 'image/webp'
                image_data = raw_b64.strip()

        if not image_data and not is_mock:
            return Response({
                'error': 'No image provided. Upload an image file under key "image" or send base64 string under "image_base64".'
            }, status=status.HTTP_400_BAD_REQUEST)

        gemini_api_key = getattr(settings, 'GEMINI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '').strip()

        detected_items = []

        # If mock requested or (no API key in development/test mode)
        if is_mock or (not gemini_api_key and (settings.DEBUG or 'test' in str(alias))):
            # Simulated Indian thali analysis for instant frontend/QA testing
            detected_items = [
                {
                    "food_name": "Roti",
                    "category": "GRAINS",
                    "estimated_grams": 70.0,
                    "calories": 208.0,
                    "protein_g": 6.2,
                    "carbs_g": 36.4,
                    "fat_g": 7.4,
                    "fiber_g": 4.6,
                    "confidence": 0.94,
                    "notes": "2 medium whole wheat chapatis with light ghee",
                },
                {
                    "food_name": "Dal Tadka",
                    "category": "PULSES_LEGUMES",
                    "estimated_grams": 150.0,
                    "calories": 180.0,
                    "protein_g": 9.0,
                    "carbs_g": 24.0,
                    "fat_g": 5.5,
                    "fiber_g": 4.0,
                    "confidence": 0.89,
                    "notes": "1 standard katori yellow lentil curry",
                },
                {
                    "food_name": "Cucumber Salad",
                    "category": "VEGETABLES",
                    "estimated_grams": 80.0,
                    "calories": 20.0,
                    "protein_g": 0.8,
                    "carbs_g": 3.2,
                    "fat_g": 0.2,
                    "fiber_g": 1.1,
                    "confidence": 0.91,
                    "notes": "Sliced cucumber and carrot slices",
                }
            ]
        elif not gemini_api_key:
            return Response({
                'error': 'AI vision service is not configured. Please set GEMINI_API_KEY in environment or contact support.',
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        else:
            # Call Google Gemini 1.5 Flash Vision API
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_api_key}"
            prompt_text = (
                "You are an expert nutrition and fitness diet assistant. Analyze this food photo and identify the distinct food items visible on the plate or meal.\n"
                "For each identified item, provide a realistic portion estimate in grams and estimated macronutrients.\n"
                "Return strictly valid JSON array of objects with keys:\n"
                "- food_name: string (name of dish or food item, e.g. 'Roti', 'Paneer Bhurji', 'Grilled Chicken Breast')\n"
                "- category: string (one of: GRAINS, PULSES_LEGUMES, DAIRY, MEAT_POULTRY, VEGETABLES, FRUITS, OILS_FATS, NUTS_SEEDS, SNACKS_SWEETS, BEVERAGES, OTHER)\n"
                "- estimated_grams: number (weight in grams)\n"
                "- calories: number (energy in kcal)\n"
                "- protein_g: number (grams)\n"
                "- carbs_g: number (grams)\n"
                "- fat_g: number (grams)\n"
                "- fiber_g: number (grams)\n"
                "- confidence: number (0.0 to 1.0 estimate)\n"
                "- notes: string (short observation like 'approx. 2 pieces' or '1 medium bowl')\n"
                "If no recognizable food is detected, return an empty array []."
            )

            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt_text},
                            {
                                "inline_data": {
                                    "mime_type": mime_type,
                                    "data": image_data
                                }
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "response_mime_type": "application/json",
                    "temperature": 0.2,
                }
            }

            try:
                resp = requests.post(gemini_url, json=payload, timeout=12)
                if resp.status_code == 200:
                    resp_json = resp.json()
                    candidates = resp_json.get('candidates', [])
                    if candidates:
                        content_parts = candidates[0].get('content', {}).get('parts', [])
                        if content_parts:
                            text_out = content_parts[0].get('text', '[]')
                            raw_parsed = json.loads(text_out)
                            if isinstance(raw_parsed, list):
                                detected_items = raw_parsed
                            elif isinstance(raw_parsed, dict) and 'items' in raw_parsed:
                                detected_items = raw_parsed['items']
                else:
                    logger.error(f"Gemini Vision API error {resp.status_code}: {resp.text}")
                    return Response({
                        'error': f"AI vision provider error (HTTP {resp.status_code}). Please try again."
                    }, status=status.HTTP_502_BAD_GATEWAY)
            except requests.Timeout:
                return Response({'error': 'AI vision service timed out. Please try again with a smaller image.'}, status=status.HTTP_504_GATEWAY_TIMEOUT)
            except Exception as e:
                logger.exception(f"Unexpected error in Gemini vision call: {e}")
                return Response({'error': f"Failed to analyze food image: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # 3. Post-process & Cross-reference against local FoodItem catalog
        scope_q = models.Q(is_system=True)
        if org:
            scope_q |= models.Q(organization=org)

        enhanced_items = []
        total_cal = Decimal('0.00')
        total_p = Decimal('0.00')
        total_c = Decimal('0.00')
        total_f = Decimal('0.00')
        total_fib = Decimal('0.00')

        for item in detected_items:
            food_name = item.get('food_name', 'Unknown')
            # Look up catalog match with prioritized matching:
            # 1. Direct name match (e.g. "Roti" matches "Roti / Chapati")
            # 2. Regional aliases match
            # 3. First word match
            catalog_match = None
            first_word = food_name.split()[0] if food_name else ''
            matched_food = FoodItem.objects.using(alias).filter(is_active=True).filter(scope_q).filter(
                name__icontains=food_name
            ).first()

            if not matched_food:
                matched_food = FoodItem.objects.using(alias).filter(is_active=True).filter(scope_q).filter(
                    regional_aliases__icontains=food_name
                ).first()

            if not matched_food and first_word:
                matched_food = FoodItem.objects.using(alias).filter(is_active=True).filter(scope_q).filter(
                    name__icontains=first_word
                ).first()

            if matched_food:
                catalog_match = FoodItemSerializer(matched_food).data

            def _to_float(v):
                try:
                    return round(float(v), 2)
                except (ValueError, TypeError):
                    return 0.0

            cal = _to_float(item.get('calories', 0))
            p = _to_float(item.get('protein_g', 0))
            c = _to_float(item.get('carbs_g', 0))
            f = _to_float(item.get('fat_g', 0))
            fib = _to_float(item.get('fiber_g', 0))

            total_cal += Decimal(str(cal))
            total_p += Decimal(str(p))
            total_c += Decimal(str(c))
            total_f += Decimal(str(f))
            total_fib += Decimal(str(fib))

            enhanced_items.append({
                'food_name': food_name,
                'category': item.get('category', 'OTHER'),
                'estimated_grams': _to_float(item.get('estimated_grams', 100)),
                'calories': cal,
                'protein_g': p,
                'carbs_g': c,
                'fat_g': f,
                'fiber_g': fib,
                'confidence': _to_float(item.get('confidence', 0.8)),
                'notes': item.get('notes', ''),
                'matched_catalog_item': catalog_match,
            })

        return Response({
            'status': 'success',
            'model': 'gemini-1.5-flash',
            'is_mock': is_mock or (not gemini_api_key),
            'detected_count': len(enhanced_items),
            'detected_items': enhanced_items,
            'summary': {
                'total_calories': float(total_cal),
                'total_protein_g': float(total_p),
                'total_carbs_g': float(total_c),
                'total_fat_g': float(total_f),
                'total_fiber_g': float(total_fib),
            }
        }, status=status.HTTP_200_OK)


class NutritionTargetMobileView(APIView):
    """
    GET /api/v1/mobile/wellness/targets/
    PUT /api/v1/mobile/wellness/targets/
    Read or update member daily goals (calories, protein, carbs, fat, water, sleep).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        profile = _get_or_create_user_profile(request.user, alias)

        target = NutritionTarget.objects.using(alias).filter(
            user=request.user,
            profile=profile,
            is_active=True,
        ).first()

        if not target:
            target = NutritionTarget(
                organization=org,
                user=request.user,
                profile=profile,
                daily_calories=Decimal('2000.00'),
                protein_grams=Decimal('120.00'),
                carbs_grams=Decimal('220.00'),
                fat_grams=Decimal('65.00'),
                water_ml=2500,
                sleep_target_minutes=480,
                is_active=True,
            )
            target.save(using=alias)

        serializer = NutritionTargetSerializer(target)
        return Response(serializer.data)

    def put(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        profile = _get_or_create_user_profile(request.user, alias)

        target, _ = NutritionTarget.objects.using(alias).get_or_create(
            user=request.user,
            profile=profile,
            defaults={
                'organization': org,
                'daily_calories': Decimal('2000.00'),
                'protein_grams': Decimal('120.00'),
                'carbs_grams': Decimal('220.00'),
                'fat_grams': Decimal('65.00'),
                'water_ml': 2500,
                'sleep_target_minutes': 480,
                'is_active': True,
            }
        )

        data = request.data
        if 'daily_calories' in data:
            target.daily_calories = Decimal(str(data['daily_calories']))
        if 'protein_grams' in data:
            target.protein_grams = Decimal(str(data['protein_grams']))
        if 'carbs_grams' in data:
            target.carbs_grams = Decimal(str(data['carbs_grams']))
        if 'fat_grams' in data:
            target.fat_grams = Decimal(str(data['fat_grams']))
        if 'water_ml' in data:
            target.water_ml = int(data['water_ml'])
        if 'sleep_target_minutes' in data:
            target.sleep_target_minutes = int(data['sleep_target_minutes'])

        target.save(using=alias)
        serializer = NutritionTargetSerializer(target)
        return Response(serializer.data)


class MealLogListCreateMobileView(APIView):
    """
    GET  /api/v1/mobile/wellness/meal-logs/?date=YYYY-MM-DD
    POST /api/v1/mobile/wellness/meal-logs/
    List meals logged on a given date or create a multi-item meal log with nutrient snapshots.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        profile = _get_or_create_user_profile(request.user, alias)
        date_str = request.query_params.get('date')
        if date_str:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            target_date = timezone.now().date()

        meals = MealLog.objects.using(alias).filter(
            user=request.user,
            profile=profile,
            logged_date=target_date,
        ).prefetch_related('items').order_by('logged_time')

        serializer = MealLogSerializer(meals, many=True)
        return Response({
            'date': str(target_date),
            'count': len(serializer.data),
            'results': serializer.data,
        })

    def post(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        if not org:
            return Response({'error': 'Active organization not found'}, status=status.HTTP_400_BAD_REQUEST)
        profile = _get_or_create_user_profile(request.user, alias)

        input_serializer = CreateMealLogSerializer(data=request.data)
        if not input_serializer.is_valid():
            return Response(input_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        valid_data = input_serializer.validated_data

        def _do_create_meal():
            logged_date = valid_data['logged_date']
            logged_time = valid_data.get('logged_time') or timezone.now().time()
            meal_type = valid_data['meal_type']
            notes = valid_data.get('notes', '')
            items_data = valid_data['items']

            with transaction.atomic(using=alias):
                meal_log = MealLog(
                    organization=org,
                    user=request.user,
                    profile=profile,
                    logged_date=logged_date,
                    logged_time=logged_time,
                    meal_type=meal_type,
                    notes=notes,
                )
                meal_log.save(using=alias)

                for item_in in items_data:
                    food_id = item_in['food_item_id']
                    quantity = Decimal(str(item_in.get('quantity') or '1.00'))
                    unit_name = (item_in.get('unit_name') or 'g').strip()
                    consumed_grams_raw = item_in.get('consumed_grams')

                    food = FoodItem.objects.using(alias).filter(id=food_id, is_active=True).first()
                    if not food:
                        raise serializers.ValidationError({'food_item_id': f'Food item {food_id} does not exist.'})

                    # Portion & gram resolution
                    if consumed_grams_raw is not None and Decimal(str(consumed_grams_raw)) > 0:
                        consumed_grams = Decimal(str(consumed_grams_raw))
                    else:
                        matching_measure = next(
                            (
                                m for m in (food.household_measures or [])
                                if str(m.get('unit', '')).lower() == unit_name.lower()
                                or str(m.get('name', '')).lower() == unit_name.lower()
                            ),
                            None
                        )
                        if matching_measure and matching_measure.get('weight_g'):
                            consumed_grams = quantity * Decimal(str(matching_measure['weight_g']))
                        elif unit_name.lower() in ('g', 'gram', 'grams', 'ml'):
                            consumed_grams = quantity
                        else:
                            consumed_grams = quantity * food.serving_weight_grams

                    # Proportion of base serving
                    ratio = consumed_grams / food.serving_weight_grams

                    # Nutrient snapshots
                    snap_calories = round(food.calories * ratio, 2)
                    snap_protein = round(food.protein_g * ratio, 2)
                    snap_carbs = round(food.carbs_g * ratio, 2)
                    snap_fat = round(food.fat_g * ratio, 2)
                    snap_fiber = round(food.fiber_g * ratio, 2)

                    log_item = MealLogItem(
                        meal_log=meal_log,
                        food_item=food,
                        food_name_snapshot=food.name,
                        quantity=quantity,
                        unit_name=unit_name,
                        consumed_grams=consumed_grams,
                        snapshot_calories=snap_calories,
                        snapshot_protein_g=snap_protein,
                        snapshot_carbs_g=snap_carbs,
                        snapshot_fat_g=snap_fat,
                        snapshot_fiber_g=snap_fiber,
                    )
                    log_item.save(using=alias)

            out_serializer = MealLogSerializer(meal_log)
            return out_serializer.data

        idempotency_key = _extract_idempotency_key(request)
        if idempotency_key:
            scoped_key = f"u:{request.user.id}:{idempotency_key}"
            try:
                result = execute_idempotent_operation(
                    organization=org,
                    operation_type='WELLNESS_MEAL_LOG',
                    idempotency_key=scoped_key,
                    operation_func=_do_create_meal,
                    request_data=request.data,
                    actor_user=request.user,
                    ttl_seconds=86400,
                    db_alias=alias,
                )
                return Response(result, status=status.HTTP_201_CREATED)
            except IdempotencyConflictError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        else:
            result = _do_create_meal()
            return Response(result, status=status.HTTP_201_CREATED)



class MealLogDetailMobileView(APIView):
    """
    GET    /api/v1/mobile/wellness/meal-logs/<id>/
    PATCH  /api/v1/mobile/wellness/meal-logs/<id>/
    DELETE /api/v1/mobile/wellness/meal-logs/<id>/
    Member-scoped operations on an existing meal log.
    """
    permission_classes = [IsAuthenticated]

    def _get_object(self, request, alias, pk):
        profile = _get_or_create_user_profile(request.user, alias)
        return MealLog.objects.using(alias).filter(
            id=pk,
            user=request.user,
            profile=profile,
        ).prefetch_related('items').first()

    def get(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        meal = self._get_object(request, alias, pk)
        if not meal:
            return Response({'error': 'Meal log not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = MealLogSerializer(meal)
        return Response(serializer.data)

    def patch(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        meal = self._get_object(request, alias, pk)
        if not meal:
            return Response({'error': 'Meal log not found'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        if 'notes' in data:
            meal.notes = data['notes']
        if 'meal_type' in data and data['meal_type'] in MealType.values:
            meal.meal_type = data['meal_type']
        if 'logged_time' in data:
            meal.logged_time = data['logged_time']

        meal.save(using=alias)
        serializer = MealLogSerializer(meal)
        return Response(serializer.data)

    def delete(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        meal = self._get_object(request, alias, pk)
        if not meal:
            return Response({'error': 'Meal log not found'}, status=status.HTTP_404_NOT_FOUND)

        meal.delete(using=alias)
        return Response({'success': True, 'message': 'Meal log deleted successfully.'}, status=status.HTTP_200_OK)


class WaterLogListCreateMobileView(APIView):
    """
    GET  /api/v1/mobile/wellness/water-logs/?date=YYYY-MM-DD
    POST /api/v1/mobile/wellness/water-logs/
    Log or list daily water intake.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        profile = _get_or_create_user_profile(request.user, alias)
        date_str = request.query_params.get('date')
        if date_str:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            target_date = timezone.now().date()

        logs = WaterLog.objects.using(alias).filter(
            user=request.user,
            profile=profile,
            logged_date=target_date,
        ).order_by('logged_at')

        total_ml = sum(log.amount_ml for log in logs)
        serializer = WaterLogSerializer(logs, many=True)
        return Response({
            'date': str(target_date),
            'total_ml': total_ml,
            'count': len(serializer.data),
            'results': serializer.data,
        })

    def post(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        if not org:
            return Response({'error': 'Active organization not found'}, status=status.HTTP_400_BAD_REQUEST)
        profile = _get_or_create_user_profile(request.user, alias)

        serializer = CreateWaterLogSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        def _do_create_water():
            logged_date = data['logged_date']
            amount_ml = data['amount_ml']
            logged_at = data.get('logged_at') or timezone.now()

            log = WaterLog(
                organization=org,
                user=request.user,
                profile=profile,
                logged_date=logged_date,
                amount_ml=amount_ml,
                logged_at=logged_at,
            )
            log.save(using=alias)

            out_serializer = WaterLogSerializer(log)
            return out_serializer.data

        idempotency_key = _extract_idempotency_key(request)
        if idempotency_key:
            scoped_key = f"u:{request.user.id}:{idempotency_key}"
            try:
                result = execute_idempotent_operation(
                    organization=org,
                    operation_type='WELLNESS_WATER_LOG',
                    idempotency_key=scoped_key,
                    operation_func=_do_create_water,
                    request_data=request.data,
                    actor_user=request.user,
                    ttl_seconds=86400,
                    db_alias=alias,
                )
                return Response(result, status=status.HTTP_201_CREATED)
            except IdempotencyConflictError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        else:
            result = _do_create_water()
            return Response(result, status=status.HTTP_201_CREATED)


class WaterLogDetailMobileView(APIView):
    """
    DELETE /api/v1/mobile/wellness/water-logs/<id>/
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        profile = _get_or_create_user_profile(request.user, alias)
        log = WaterLog.objects.using(alias).filter(id=pk, user=request.user, profile=profile).first()
        if not log:
            return Response({'error': 'Water log not found'}, status=status.HTTP_404_NOT_FOUND)

        log.delete(using=alias)
        return Response({'success': True, 'message': 'Water entry deleted.'}, status=status.HTTP_200_OK)


class SleepLogListCreateMobileView(APIView):
    """
    GET  /api/v1/mobile/wellness/sleep-logs/?date=YYYY-MM-DD
    POST /api/v1/mobile/wellness/sleep-logs/
    Log or retrieve sleep entries.
    Supports idempotent submission and atomic single-daily-record update strategy.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        profile = _get_or_create_user_profile(request.user, alias)
        date_str = request.query_params.get('date')
        if date_str:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            target_date = timezone.now().date()

        logs = SleepLog.objects.using(alias).filter(
            user=request.user,
            profile=profile,
            logged_date=target_date,
        ).order_by('-start_time')

        serializer = SleepLogSerializer(logs, many=True)
        return Response({
            'date': str(target_date),
            'count': len(serializer.data),
            'results': serializer.data,
        })

    def post(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        if not org:
            return Response({'error': 'Active organization not found'}, status=status.HTTP_400_BAD_REQUEST)
        profile = _get_or_create_user_profile(request.user, alias)

        serializer = CreateSleepLogSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        def _do_save_sleep():
            logged_date = data['logged_date']
            start_time = data['start_time']
            end_time = data['end_time']
            duration_minutes = data['duration_minutes']
            quality_rating = data.get('quality_rating', 3)
            notes = data.get('notes', '')

            sleep_log = None
            created = False

            # Concurrency race-resilient upsert:
            # When two distinct concurrent requests arrive without an Idempotency-Key
            # before any sleep record exists for (user, logged_date), one thread wins
            # the INSERT while the second hits uq_sleep_log_user_date.
            # We catch IntegrityError specifically for uq_sleep_log_user_date, roll back
            # the inner savepoint cleanly, and acquire the row lock on the winner to update it.
            for attempt in range(3):
                try:
                    with transaction.atomic(using=alias):
                        existing = SleepLog.objects.using(alias).select_for_update().filter(
                            user=request.user,
                            logged_date=logged_date,
                        ).first()

                        if existing:
                            existing.start_time = start_time
                            existing.end_time = end_time
                            existing.duration_minutes = duration_minutes
                            existing.quality_rating = quality_rating
                            if 'notes' in data:
                                existing.notes = notes
                            existing.save(using=alias)
                            sleep_log = existing
                            created = False
                        else:
                            try:
                                with transaction.atomic(using=alias):
                                    sleep_log = SleepLog.objects.using(alias).create(
                                        organization=org,
                                        user=request.user,
                                        profile=profile,
                                        logged_date=logged_date,
                                        start_time=start_time,
                                        end_time=end_time,
                                        duration_minutes=duration_minutes,
                                        quality_rating=quality_rating,
                                        notes=notes,
                                    )
                                    created = True
                            except IntegrityError as create_err:
                                err_text = str(create_err).lower()
                                if 'uq_sleep_log_user_date' in err_text or 'unique constraint' in err_text:
                                    logger.info(
                                        "Concurrent first-insert race on sleep log for user %s date %s; retrying via update.",
                                        request.user.id, logged_date
                                    )
                                    continue
                                else:
                                    # Do not swallow unrelated integrity errors (e.g. FK, NOT NULL)
                                    raise

                    if sleep_log:
                        break
                except IntegrityError as outer_err:
                    err_text = str(outer_err).lower()
                    if ('uq_sleep_log_user_date' in err_text or 'unique constraint' in err_text) and attempt < 2:
                        continue
                    raise

            if not sleep_log:
                sleep_log = SleepLog.objects.using(alias).filter(
                    user=request.user,
                    logged_date=logged_date,
                ).first()
                if not sleep_log:
                    raise RuntimeError("Failed to resolve sleep log after concurrency retries.")
                created = False

            out_serializer = SleepLogSerializer(sleep_log)
            res = dict(out_serializer.data)
            res['_is_created'] = created
            return res

        idempotency_key = _extract_idempotency_key(request)
        if idempotency_key:
            scoped_key = f"u:{request.user.id}:{idempotency_key}"
            try:
                result = execute_idempotent_operation(
                    organization=org,
                    operation_type='WELLNESS_SLEEP_LOG',
                    idempotency_key=scoped_key,
                    operation_func=_do_save_sleep,
                    request_data=request.data,
                    actor_user=request.user,
                    ttl_seconds=86400,
                    db_alias=alias,
                )
                is_created = result.pop('_is_created', True)
                return Response(result, status=status.HTTP_201_CREATED if is_created else status.HTTP_200_OK)
            except IdempotencyConflictError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        else:
            result = _do_save_sleep()
            is_created = result.pop('_is_created', True)
            return Response(result, status=status.HTTP_201_CREATED if is_created else status.HTTP_200_OK)


class SleepLogDetailMobileView(APIView):
    """
    GET    /api/v1/mobile/wellness/sleep-logs/<id>/
    PATCH  /api/v1/mobile/wellness/sleep-logs/<id>/
    PUT    /api/v1/mobile/wellness/sleep-logs/<id>/
    DELETE /api/v1/mobile/wellness/sleep-logs/<id>/
    Member-scoped operations on an existing sleep log.
    """
    permission_classes = [IsAuthenticated]

    def _get_object(self, request, alias, pk):
        profile = _get_or_create_user_profile(request.user, alias)
        return SleepLog.objects.using(alias).filter(
            id=pk,
            user=request.user,
            profile=profile,
        ).first()

    def get(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        sleep = self._get_object(request, alias, pk)
        if not sleep:
            return Response({'error': 'Sleep log not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = SleepLogSerializer(sleep)
        return Response(serializer.data)

    def patch(self, request, pk):
        return self._update(request, pk, partial=True)

    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    def _update(self, request, pk, partial=True):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        sleep = self._get_object(request, alias, pk)
        if not sleep:
            return Response({'error': 'Sleep log not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = UpdateSleepLogSerializer(data=request.data, partial=partial)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        if 'start_time' in data:
            sleep.start_time = data['start_time']
        if 'end_time' in data:
            sleep.end_time = data['end_time']
        if 'duration_minutes' in data:
            sleep.duration_minutes = data['duration_minutes']
        elif 'start_time' in data or 'end_time' in data:
            diff = (sleep.end_time - sleep.start_time).total_seconds() / 60.0
            sleep.duration_minutes = max(1, int(round(diff)))
        if 'quality_rating' in data:
            sleep.quality_rating = data['quality_rating']
        if 'notes' in data:
            sleep.notes = data['notes']

        # Boundary checks
        if sleep.end_time <= sleep.start_time:
            return Response({'error': 'end_time must be after start_time.'}, status=status.HTTP_400_BAD_REQUEST)
        if (sleep.end_time - sleep.start_time).total_seconds() > 86400:
            return Response({'error': 'Sleep duration cannot exceed 24 hours.'}, status=status.HTTP_400_BAD_REQUEST)

        sleep.save(using=alias)
        out_serializer = SleepLogSerializer(sleep)
        return Response(out_serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, pk):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        profile = _get_or_create_user_profile(request.user, alias)
        log = SleepLog.objects.using(alias).filter(id=pk, user=request.user, profile=profile).first()
        if not log:
            return Response({'error': 'Sleep log not found'}, status=status.HTTP_404_NOT_FOUND)

        log.delete(using=alias)
        return Response({'success': True, 'message': 'Sleep log deleted.'}, status=status.HTTP_200_OK)



class DailySummaryMobileView(APIView):
    """
    GET /api/v1/mobile/wellness/daily-summary/?date=YYYY-MM-DD
    Returns combined daily nutrition, water, and sleep rollup against member targets.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'error': 'Tenant database not found'}, status=status.HTTP_400_BAD_REQUEST)

        org = _get_active_organization(request, alias)
        profile = _get_or_create_user_profile(request.user, alias)

        date_str = request.query_params.get('date')
        if date_str:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            target_date = timezone.now().date()

        # 1. Member Targets
        target = NutritionTarget.objects.using(alias).filter(
            user=request.user,
            profile=profile,
            is_active=True,
        ).first()

        target_cals = target.daily_calories if target else Decimal('2000.00')
        target_protein = target.protein_grams if target else Decimal('120.00')
        target_carbs = target.carbs_grams if target else Decimal('220.00')
        target_fat = target.fat_grams if target else Decimal('65.00')
        target_water = target.water_ml if target else 2500
        target_sleep = target.sleep_target_minutes if target else 480

        # 2. Nutrition Consumption
        meals = MealLog.objects.using(alias).filter(
            user=request.user,
            profile=profile,
            logged_date=target_date,
        ).prefetch_related('items').order_by('logged_time')

        total_calories = Decimal('0.00')
        total_protein = Decimal('0.00')
        total_carbs = Decimal('0.00')
        total_fat = Decimal('0.00')
        total_fiber = Decimal('0.00')

        meals_breakdown = []
        for meal in meals:
            m_cals = sum((item.snapshot_calories for item in meal.items.all()), Decimal('0.00'))
            m_prot = sum((item.snapshot_protein_g for item in meal.items.all()), Decimal('0.00'))
            m_carbs = sum((item.snapshot_carbs_g for item in meal.items.all()), Decimal('0.00'))
            m_fat = sum((item.snapshot_fat_g for item in meal.items.all()), Decimal('0.00'))
            m_fib = sum((item.snapshot_fiber_g for item in meal.items.all()), Decimal('0.00'))

            total_calories += m_cals
            total_protein += m_prot
            total_carbs += m_carbs
            total_fat += m_fat
            total_fiber += m_fib

            meals_breakdown.append({
                'id': str(meal.id),
                'meal_type': meal.meal_type,
                'logged_time': str(meal.logged_time),
                'calories': float(m_cals),
                'protein_g': float(m_prot),
                'carbs_g': float(m_carbs),
                'fat_g': float(m_fat),
                'fiber_g': float(m_fib),
                'items_count': meal.items.count(),
            })

        # Remaining
        rem_cals = max(Decimal('0.00'), target_cals - total_calories)
        rem_prot = max(Decimal('0.00'), target_protein - total_protein)
        rem_carbs = max(Decimal('0.00'), target_carbs - total_carbs)
        rem_fat = max(Decimal('0.00'), target_fat - total_fat)

        # 3. Water Consumption
        water_logs = WaterLog.objects.using(alias).filter(
            user=request.user,
            profile=profile,
            logged_date=target_date,
        )
        total_water_ml = sum(log.amount_ml for log in water_logs)
        water_pct = round((total_water_ml / target_water * 100), 1) if target_water else 0.0

        # 4. Sleep Entry
        sleep_log = SleepLog.objects.using(alias).filter(
            user=request.user,
            profile=profile,
            logged_date=target_date,
        ).order_by('-start_time').first()

        sleep_duration = sleep_log.duration_minutes if sleep_log else 0
        sleep_rating = sleep_log.quality_rating if sleep_log else None

        return Response({
            'date': str(target_date),
            'targets': {
                'calories': float(target_cals),
                'protein_g': float(target_protein),
                'carbs_g': float(target_carbs),
                'fat_g': float(target_fat),
                'water_ml': target_water,
                'sleep_target_minutes': target_sleep,
            },
            'consumed': {
                'calories': float(total_calories),
                'protein_g': float(total_protein),
                'carbs_g': float(total_carbs),
                'fat_g': float(total_fat),
                'fiber_g': float(total_fiber),
            },
            'remaining': {
                'calories': float(rem_cals),
                'protein_g': float(rem_prot),
                'carbs_g': float(rem_carbs),
                'fat_g': float(rem_fat),
            },
            'water': {
                'consumed_ml': total_water_ml,
                'target_ml': target_water,
                'percentage': water_pct,
            },
            'sleep': {
                'duration_minutes': sleep_duration,
                'target_minutes': target_sleep,
                'quality_rating': sleep_rating,
            },
            'meals': meals_breakdown,
        })
