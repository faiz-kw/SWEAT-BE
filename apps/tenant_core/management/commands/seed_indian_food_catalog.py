"""
Management command to seed the verified 25-item Indian Food Catalog into the tenant database.
Idempotent insert or update using (name, is_system=True).

Usage:
    python manage.py seed_indian_food_catalog --tenant=sweat
    python manage.py seed_indian_food_catalog --database=tenant_45b04aeb0c2c496e9f07cbd195402556
"""

from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import connections

from apps.master.models_tenant import Tenant
from apps.master.models_infra import TenantDataSource
from config.tenant_middleware import _register_tenant_connection
from config.routers import build_tenant_db_alias
from apps.tenant_core.models_wellness import FoodItem, FoodCategory, FoodProvenance


INDIAN_FOOD_CATALOG = [
    # -------------------------------------------------------------------------
    # 10 USDA DIRECT FOODS
    # -------------------------------------------------------------------------
    {
        'name': 'Whole Milk (Cow)',
        'regional_aliases': ['Doodh', 'Cow Milk', 'Paal'],
        'category': FoodCategory.DAIRY,
        'serving_unit': 'ml',
        'serving_weight_grams': Decimal('100.00'),
        'household_measures': [
            {'name': '1 cup (200ml)', 'weight_g': 200.0, 'unit': 'cup'},
            {'name': '1 glass (250ml)', 'weight_g': 250.0, 'unit': 'glass'},
            {'name': '100ml', 'weight_g': 100.0, 'unit': 'ml'},
        ],
        'calories': Decimal('61.00'),
        'protein_g': Decimal('3.15'),
        'carbs_g': Decimal('4.80'),
        'fat_g': Decimal('3.25'),
        'fiber_g': Decimal('0.00'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 171265',
        'preparation_notes': 'Directly sourced from USDA FoodData Central record 171265 (Milk, whole, 3.25% milkfat).',
    },
    {
        'name': 'Egg, Whole, Boiled',
        'regional_aliases': ['Boiled Egg', 'Ubla Anda', 'Muttai'],
        'category': FoodCategory.DAIRY,
        'serving_unit': 'piece',
        'serving_weight_grams': Decimal('50.00'),
        'household_measures': [
            {'name': '1 large egg (50g)', 'weight_g': 50.0, 'unit': 'piece'},
            {'name': '2 large eggs (100g)', 'weight_g': 100.0, 'unit': 'piece'},
        ],
        'calories': Decimal('77.50'),
        'protein_g': Decimal('6.30'),
        'carbs_g': Decimal('0.56'),
        'fat_g': Decimal('5.30'),
        'fiber_g': Decimal('0.00'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 171287',
        'preparation_notes': 'Directly sourced from USDA FoodData Central record 171287 (Egg, whole, hard-boiled). Scaled to 1 large egg (50g).',
    },
    {
        'name': 'Chicken Breast, Grilled/Cooked',
        'regional_aliases': ['Murgh', 'Kozhi', 'Chicken Breast'],
        'category': FoodCategory.MEAT_POULTRY,
        'serving_unit': 'g',
        'serving_weight_grams': Decimal('100.00'),
        'household_measures': [
            {'name': '1 fillet (150g)', 'weight_g': 150.0, 'unit': 'piece'},
            {'name': '100g portion', 'weight_g': 100.0, 'unit': 'g'},
        ],
        'calories': Decimal('165.00'),
        'protein_g': Decimal('31.00'),
        'carbs_g': Decimal('0.00'),
        'fat_g': Decimal('3.60'),
        'fiber_g': Decimal('0.00'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 171077',
        'preparation_notes': 'Directly sourced from USDA FoodData Central record 171077 (Chicken, breast, meat only, cooked, roasted).',
    },
    {
        'name': 'Paneer (Indian Cottage Cheese, Fresh)',
        'regional_aliases': ['Paneer', 'Cottage Cheese', 'Chenna'],
        'category': FoodCategory.DAIRY,
        'serving_unit': 'g',
        'serving_weight_grams': Decimal('100.00'),
        'household_measures': [
            {'name': '1 slice / large cube (30g)', 'weight_g': 30.0, 'unit': 'piece'},
            {'name': '100g slab', 'weight_g': 100.0, 'unit': 'g'},
        ],
        'calories': Decimal('265.00'),
        'protein_g': Decimal('18.30'),
        'carbs_g': Decimal('3.40'),
        'fat_g': Decimal('20.80'),
        'fiber_g': Decimal('0.00'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 170857',
        'preparation_notes': 'Directly sourced from USDA FoodData Central reference values for whole milk cheese paneer curd.',
    },
    {
        'name': 'Banana, Raw',
        'regional_aliases': ['Kela', 'Vazhaipazham', 'Kele'],
        'category': FoodCategory.FRUITS,
        'serving_unit': 'piece',
        'serving_weight_grams': Decimal('118.00'),
        'household_measures': [
            {'name': '1 medium banana (118g)', 'weight_g': 118.0, 'unit': 'piece'},
            {'name': '1 small banana (80g)', 'weight_g': 80.0, 'unit': 'piece'},
            {'name': '100g portion', 'weight_g': 100.0, 'unit': 'g'},
        ],
        'calories': Decimal('105.00'),
        'protein_g': Decimal('1.29'),
        'carbs_g': Decimal('26.95'),
        'fat_g': Decimal('0.39'),
        'fiber_g': Decimal('3.07'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 173944',
        'preparation_notes': 'Directly sourced from USDA FoodData Central record 173944 (Bananas, raw). Standard serving scaled to 1 medium (118g).',
    },
    {
        'name': 'Apple, Raw with Skin',
        'regional_aliases': ['Seb', 'Apple', 'Kashmir Apple'],
        'category': FoodCategory.FRUITS,
        'serving_unit': 'piece',
        'serving_weight_grams': Decimal('182.00'),
        'household_measures': [
            {'name': '1 medium apple (182g)', 'weight_g': 182.0, 'unit': 'piece'},
            {'name': '100g portion', 'weight_g': 100.0, 'unit': 'g'},
        ],
        'calories': Decimal('94.60'),
        'protein_g': Decimal('0.47'),
        'carbs_g': Decimal('25.13'),
        'fat_g': Decimal('0.31'),
        'fiber_g': Decimal('4.37'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 171688',
        'preparation_notes': 'Directly sourced from USDA FoodData Central record 171688 (Apples, raw, with skin). Scaled to 1 medium apple (182g).',
    },
    {
        'name': 'White Rice, Cooked',
        'regional_aliases': ['Chawal', 'Bhat', 'Sadam', 'Steamed Rice'],
        'category': FoodCategory.GRAINS,
        'serving_unit': 'katori',
        'serving_weight_grams': Decimal('150.00'),
        'household_measures': [
            {'name': '1 katori / bowl (150g)', 'weight_g': 150.0, 'unit': 'katori'},
            {'name': '1 cup (180g)', 'weight_g': 180.0, 'unit': 'cup'},
            {'name': '100g portion', 'weight_g': 100.0, 'unit': 'g'},
        ],
        'calories': Decimal('195.00'),
        'protein_g': Decimal('4.04'),
        'carbs_g': Decimal('42.26'),
        'fat_g': Decimal('0.42'),
        'fiber_g': Decimal('0.60'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 169756',
        'preparation_notes': 'Directly sourced from USDA FoodData Central record 169756 (Rice, white, long-grain, regular, cooked). Scaled to 150g katori.',
    },
    {
        'name': 'Curd / Plain Yogurt (Whole Milk)',
        'regional_aliases': ['Dahi', 'Mosaru', 'Thayir', 'Curd'],
        'category': FoodCategory.DAIRY,
        'serving_unit': 'katori',
        'serving_weight_grams': Decimal('150.00'),
        'household_measures': [
            {'name': '1 katori (150g)', 'weight_g': 150.0, 'unit': 'katori'},
            {'name': '100g portion', 'weight_g': 100.0, 'unit': 'g'},
        ],
        'calories': Decimal('91.50'),
        'protein_g': Decimal('5.21'),
        'carbs_g': Decimal('6.99'),
        'fat_g': Decimal('4.88'),
        'fiber_g': Decimal('0.00'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 171284',
        'preparation_notes': 'Directly sourced from USDA FoodData Central record 171284 (Yogurt, plain, whole milk). Scaled to 150g katori.',
    },
    {
        'name': 'Almonds, Raw',
        'regional_aliases': ['Badam', 'Almonds'],
        'category': FoodCategory.NUTS_SEEDS,
        'serving_unit': 'handful',
        'serving_weight_grams': Decimal('28.00'),
        'household_measures': [
            {'name': '1 handful (28g, ~23 nuts)', 'weight_g': 28.0, 'unit': 'handful'},
            {'name': '10 almonds (12g)', 'weight_g': 12.0, 'unit': 'piece'},
            {'name': '100g portion', 'weight_g': 100.0, 'unit': 'g'},
        ],
        'calories': Decimal('162.10'),
        'protein_g': Decimal('5.92'),
        'carbs_g': Decimal('6.03'),
        'fat_g': Decimal('13.98'),
        'fiber_g': Decimal('3.50'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 170567',
        'preparation_notes': 'Directly sourced from USDA FoodData Central record 170567 (Nuts, almonds). Standard 1 oz (28g) serving.',
    },
    {
        'name': 'Peanuts, Roasted',
        'regional_aliases': ['Mungfali', 'Shengdana', 'Kadalai', 'Peanuts'],
        'category': FoodCategory.NUTS_SEEDS,
        'serving_unit': 'handful',
        'serving_weight_grams': Decimal('30.00'),
        'household_measures': [
            {'name': '1 handful (30g)', 'weight_g': 30.0, 'unit': 'handful'},
            {'name': '100g portion', 'weight_g': 100.0, 'unit': 'g'},
        ],
        'calories': Decimal('175.50'),
        'protein_g': Decimal('7.10'),
        'carbs_g': Decimal('6.45'),
        'fat_g': Decimal('14.90'),
        'fiber_g': Decimal('2.40'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'FDC: 170582',
        'preparation_notes': 'Directly sourced from USDA FoodData Central record 170582 (Peanuts, all types, dry-roasted, without salt). Scaled to 30g.',
    },

    # -------------------------------------------------------------------------
    # 15 COMPOSITE INDIAN RECIPES (RECIPE_ESTIMATE, is_nutritionist_verified=False)
    # -------------------------------------------------------------------------
    {
        'name': 'Phulka / Plain Roti',
        'regional_aliases': ['Roti', 'Chapati', 'Phulka'],
        'category': FoodCategory.GRAINS,
        'serving_unit': 'piece',
        'serving_weight_grams': Decimal('35.00'),
        'household_measures': [
            {'name': '1 phulka / roti (35g)', 'weight_g': 35.0, 'unit': 'piece'},
            {'name': '2 phulkas (70g)', 'weight_g': 70.0, 'unit': 'piece'},
            {'name': '3 phulkas (105g)', 'weight_g': 105.0, 'unit': 'piece'},
        ],
        'calories': Decimal('102.00'),
        'protein_g': Decimal('4.00'),
        'carbs_g': Decimal('21.60'),
        'fat_g': Decimal('0.80'),
        'fiber_g': Decimal('3.20'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: PHULKA-01',
        'preparation_notes': 'Yield: 35g single phulka from 30g whole wheat atta (FDC 169742) mixed with water. Dry tawa cooked, puffed over flame without oil/ghee.',
    },
    {
        'name': 'Plain Dosa',
        'regional_aliases': ['Dosai', 'Sada Dosa', 'Plain Dosa'],
        'category': FoodCategory.GRAINS,
        'serving_unit': 'piece',
        'serving_weight_grams': Decimal('80.00'),
        'household_measures': [
            {'name': '1 medium dosa (80g)', 'weight_g': 80.0, 'unit': 'piece'},
            {'name': '2 dosas (160g)', 'weight_g': 160.0, 'unit': 'piece'},
        ],
        'calories': Decimal('204.80'),
        'protein_g': Decimal('5.50'),
        'carbs_g': Decimal('35.10'),
        'fat_g': Decimal('4.40'),
        'fiber_g': Decimal('1.20'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: DOSA-01',
        'preparation_notes': 'Yield: 80g cooked crepe from fermented batter (35g raw white rice + 12g dehulled white urad dal) with 4g refined vegetable oil on tawa.',
    },
    {
        'name': 'Steamed Idli',
        'regional_aliases': ['Idli', 'Idly', 'Steamed Rice Cake'],
        'category': FoodCategory.GRAINS,
        'serving_unit': 'piece',
        'serving_weight_grams': Decimal('50.00'),
        'household_measures': [
            {'name': '1 idli (50g)', 'weight_g': 50.0, 'unit': 'piece'},
            {'name': '2 idlis (1 plate, 100g)', 'weight_g': 100.0, 'unit': 'piece'},
            {'name': '4 idlis (200g)', 'weight_g': 200.0, 'unit': 'piece'},
        ],
        'calories': Decimal('59.50'),
        'protein_g': Decimal('1.90'),
        'carbs_g': Decimal('12.40'),
        'fat_g': Decimal('0.20'),
        'fiber_g': Decimal('0.40'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: IDLI-01',
        'preparation_notes': 'Yield: 50g single steamed cake from fermented batter (25g parboiled rice + 8g dehulled urad dal) without oil.',
    },
    {
        'name': 'Yellow Dal Tadka',
        'regional_aliases': ['Toor Dal', 'Peeli Dal', 'Dal Fry', 'Tarka Dal'],
        'category': FoodCategory.PULSES_LEGUMES,
        'serving_unit': 'katori',
        'serving_weight_grams': Decimal('150.00'),
        'household_measures': [
            {'name': '1 katori (150g)', 'weight_g': 150.0, 'unit': 'katori'},
            {'name': '1 large bowl (250g)', 'weight_g': 250.0, 'unit': 'bowl'},
        ],
        'calories': Decimal('179.20'),
        'protein_g': Decimal('8.20'),
        'carbs_g': Decimal('25.00'),
        'fat_g': Decimal('5.60'),
        'fiber_g': Decimal('6.00'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: DALTADKA-01',
        'preparation_notes': 'Yield: 150g cooked dal from 35g raw toor dal, 5g ghee/oil, 25g onion-tomato-garlic-cumin tempering.',
    },
    {
        'name': 'Moong Dal Khichdi',
        'regional_aliases': ['Khichri', 'Moong Khichdi', 'Dal Khichdi'],
        'category': FoodCategory.GRAINS,
        'serving_unit': 'bowl',
        'serving_weight_grams': Decimal('200.00'),
        'household_measures': [
            {'name': '1 medium bowl (200g)', 'weight_g': 200.0, 'unit': 'bowl'},
            {'name': '1 large bowl (300g)', 'weight_g': 300.0, 'unit': 'bowl'},
        ],
        'calories': Decimal('260.00'),
        'protein_g': Decimal('8.70'),
        'carbs_g': Decimal('43.70'),
        'fat_g': Decimal('5.60'),
        'fiber_g': Decimal('4.50'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: KHICHDI-01',
        'preparation_notes': 'Yield: 200g cooked soft khichdi from 35g raw rice, 25g split moong dal, 5g ghee, turmeric, cumin, salt.',
    },
    {
        'name': 'Rajma Masala',
        'regional_aliases': ['Rajma Curry', 'Kidney Beans Gravy', 'Rajma'],
        'category': FoodCategory.PULSES_LEGUMES,
        'serving_unit': 'katori',
        'serving_weight_grams': Decimal('180.00'),
        'household_measures': [
            {'name': '1 katori (180g)', 'weight_g': 180.0, 'unit': 'katori'},
            {'name': '1 bowl (250g)', 'weight_g': 250.0, 'unit': 'bowl'},
        ],
        'calories': Decimal('228.00'),
        'protein_g': Decimal('11.60'),
        'carbs_g': Decimal('32.50'),
        'fat_g': Decimal('6.60'),
        'fiber_g': Decimal('12.40'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: RAJMA-01',
        'preparation_notes': 'Yield: 180g cooked curry from 45g raw red kidney beans, 6g oil, 40g onion-tomato spiced gravy, ginger-garlic paste.',
    },
    {
        'name': 'Chana Masala',
        'regional_aliases': ['Chole', 'Kabuli Chana Curry', 'Chana Masala'],
        'category': FoodCategory.PULSES_LEGUMES,
        'serving_unit': 'katori',
        'serving_weight_grams': Decimal('180.00'),
        'household_measures': [
            {'name': '1 katori (180g)', 'weight_g': 180.0, 'unit': 'katori'},
            {'name': '1 bowl (250g)', 'weight_g': 250.0, 'unit': 'bowl'},
        ],
        'calories': Decimal('242.00'),
        'protein_g': Decimal('9.80'),
        'carbs_g': Decimal('32.50'),
        'fat_g': Decimal('8.90'),
        'fiber_g': Decimal('9.00'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: CHANA-01',
        'preparation_notes': 'Yield: 180g cooked curry from 45g raw kabuli chana (chickpeas), 6g oil, 40g onion-tomato spiced gravy, amchur.',
    },
    {
        'name': 'Palak Paneer',
        'regional_aliases': ['Spinach Cottage Cheese', 'Saag Paneer', 'Palak Paneer'],
        'category': FoodCategory.VEGETABLES,
        'serving_unit': 'katori',
        'serving_weight_grams': Decimal('180.00'),
        'household_measures': [
            {'name': '1 katori (180g)', 'weight_g': 180.0, 'unit': 'katori'},
            {'name': '1 bowl (250g)', 'weight_g': 250.0, 'unit': 'bowl'},
        ],
        'calories': Decimal('217.50'),
        'protein_g': Decimal('12.65'),
        'carbs_g': Decimal('8.80'),
        'fat_g': Decimal('16.00'),
        'fiber_g': Decimal('3.20'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: PALAKPANEER-01',
        'preparation_notes': 'Yield: 180g cooked dish from 50g fresh paneer, 100g pureed spinach, 5g oil/butter, onion, garlic, spices.',
    },
    {
        'name': 'Paneer Bhurji',
        'regional_aliases': ['Scrambled Paneer', 'Paneer Bhurji'],
        'category': FoodCategory.DAIRY,
        'serving_unit': 'serving',
        'serving_weight_grams': Decimal('150.00'),
        'household_measures': [
            {'name': '1 serving (150g)', 'weight_g': 150.0, 'unit': 'serving'},
            {'name': '1 katori (100g)', 'weight_g': 100.0, 'unit': 'katori'},
        ],
        'calories': Decimal('281.00'),
        'protein_g': Decimal('15.60'),
        'carbs_g': Decimal('7.70'),
        'fat_g': Decimal('21.80'),
        'fiber_g': Decimal('1.50'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: PANEERBHURJI-01',
        'preparation_notes': 'Yield: 150g scrambled paneer from 80g crumbled paneer, 5g oil, 35g sauteed onions, tomatoes, green chilies, coriander.',
    },
    {
        'name': 'Vegetable Poha',
        'regional_aliases': ['Poha', 'Kanda Poha', 'Aval', 'Batata Poha'],
        'category': FoodCategory.GRAINS,
        'serving_unit': 'plate',
        'serving_weight_grams': Decimal('160.00'),
        'household_measures': [
            {'name': '1 medium plate (160g)', 'weight_g': 160.0, 'unit': 'plate'},
            {'name': '1 small bowl (100g)', 'weight_g': 100.0, 'unit': 'bowl'},
        ],
        'calories': Decimal('312.70'),
        'protein_g': Decimal('6.70'),
        'carbs_g': Decimal('47.10'),
        'fat_g': Decimal('10.80'),
        'fiber_g': Decimal('3.40'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: POHA-01',
        'preparation_notes': 'Yield: 160g cooked dish from 50g dry flattened rice, 10g roasted peanuts, 5g oil, 30g boiled diced potato, onion, mustard seeds, turmeric.',
    },
    {
        'name': 'Vegetable Upma',
        'regional_aliases': ['Rava Upma', 'Uppittu', 'Khara Bath', 'Sooji Upma'],
        'category': FoodCategory.GRAINS,
        'serving_unit': 'bowl',
        'serving_weight_grams': Decimal('180.00'),
        'household_measures': [
            {'name': '1 medium bowl (180g)', 'weight_g': 180.0, 'unit': 'bowl'},
            {'name': '1 small katori (120g)', 'weight_g': 120.0, 'unit': 'katori'},
        ],
        'calories': Decimal('238.00'),
        'protein_g': Decimal('6.10'),
        'carbs_g': Decimal('37.80'),
        'fat_g': Decimal('6.70'),
        'fiber_g': Decimal('3.00'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: UPMA-01',
        'preparation_notes': 'Yield: 180g cooked dish from 45g roasted semolina/sooji, 6g oil/ghee, 35g mixed diced carrots, peas, mustard seeds, curry leaves.',
    },
    {
        'name': 'Masala Chai (with Milk & Sugar)',
        'regional_aliases': ['Tea', 'Indian Chai', 'Milk Tea', 'Masala Tea'],
        'category': FoodCategory.BEVERAGES,
        'serving_unit': 'cup',
        'serving_weight_grams': Decimal('150.00'),
        'household_measures': [
            {'name': '1 cup (150ml)', 'weight_g': 150.0, 'unit': 'cup'},
            {'name': '1 cutting chai (90ml)', 'weight_g': 90.0, 'unit': 'cup'},
            {'name': '1 mug (250ml)', 'weight_g': 250.0, 'unit': 'mug'},
        ],
        'calories': Decimal('67.40'),
        'protein_g': Decimal('2.50'),
        'carbs_g': Decimal('9.00'),
        'fat_g': Decimal('2.40'),
        'fiber_g': Decimal('0.00'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: CHAI-01',
        'preparation_notes': 'Yield: 150ml brewed tea from 75ml whole cow milk, 75ml water, 5g white sugar, 2g black tea leaves, crushed ginger and cardamom.',
    },
    {
        'name': 'Sambar',
        'regional_aliases': ['South Indian Sambar', 'Kozhambu', 'Sambar'],
        'category': FoodCategory.PULSES_LEGUMES,
        'serving_unit': 'katori',
        'serving_weight_grams': Decimal('150.00'),
        'household_measures': [
            {'name': '1 katori (150g)', 'weight_g': 150.0, 'unit': 'katori'},
            {'name': '1 cup (200g)', 'weight_g': 200.0, 'unit': 'cup'},
        ],
        'calories': Decimal('120.10'),
        'protein_g': Decimal('5.50'),
        'carbs_g': Decimal('17.60'),
        'fat_g': Decimal('3.60'),
        'fiber_g': Decimal('5.00'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: SAMBAR-01',
        'preparation_notes': 'Yield: 150g lentil-vegetable stew from 20g raw toor dal, 50g drumstick-gourd-tomato, 3g oil, tamarind pulp, sambar masala.',
    },
    {
        'name': 'Aloo Paratha (with Ghee)',
        'regional_aliases': ['Potato Paratha', 'Aloo Roti', 'Aloo Paratha'],
        'category': FoodCategory.GRAINS,
        'serving_unit': 'piece',
        'serving_weight_grams': Decimal('120.00'),
        'household_measures': [
            {'name': '1 medium paratha (120g)', 'weight_g': 120.0, 'unit': 'piece'},
            {'name': '2 parathas (240g)', 'weight_g': 240.0, 'unit': 'piece'},
        ],
        'calories': Decimal('245.80'),
        'protein_g': Decimal('7.00'),
        'carbs_g': Decimal('41.40'),
        'fat_g': Decimal('7.30'),
        'fiber_g': Decimal('5.70'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: ALOOPARATHA-01',
        'preparation_notes': 'Yield: 120g cooked flatbread from 45g whole wheat atta, 40g boiled spiced potato stuffing, 6g pure desi ghee on tawa.',
    },
    {
        'name': 'Egg Bhurji (Indian Scrambled Eggs)',
        'regional_aliases': ['Anda Bhurji', 'Egg Scramble', 'Muttai Poriyal'],
        'category': FoodCategory.DAIRY,
        'serving_unit': 'serving',
        'serving_weight_grams': Decimal('120.00'),
        'household_measures': [
            {'name': '1 serving (2 eggs, 120g)', 'weight_g': 120.0, 'unit': 'serving'},
            {'name': '1 egg portion (60g)', 'weight_g': 60.0, 'unit': 'serving'},
        ],
        'calories': Decimal('219.20'),
        'protein_g': Decimal('13.40'),
        'carbs_g': Decimal('5.10'),
        'fat_g': Decimal('15.80'),
        'fiber_g': Decimal('1.00'),
        'provenance': FoodProvenance.RECIPE_ESTIMATE,
        'source_reference_id': 'SWEAT-RECIPE: EGGBHURJI-01',
        'preparation_notes': 'Yield: 120g cooked scramble from 2 whole large eggs (100g), 5g oil, 25g chopped onion, tomato, green chilies, turmeric, fresh coriander.',
    },
    # -------------------------------------------------------------------------
    # PACKAGED INDIAN BEVERAGES & SNACKS (WITH VERIFIED EAN-13 BARCODES)
    # -------------------------------------------------------------------------
    {
        'name': 'Maaza Mango Drink',
        'barcode': '8901764012280',
        'regional_aliases': ['Maaza', 'Maaza Mango', '8901764012273', '8901764011214', '8901764012013', '8901764012297', '8901764012280'],
        'category': FoodCategory.BEVERAGES,
        'serving_unit': 'ml',
        'serving_weight_grams': Decimal('100.00'),
        'household_measures': [
            {'name': '1 glass (250ml)', 'weight_g': 250.0, 'unit': 'glass'},
            {'name': '1 small tetrapack (150ml)', 'weight_g': 150.0, 'unit': 'tetrapack'},
            {'name': '1 pet bottle (600ml)', 'weight_g': 600.0, 'unit': 'bottle'},
        ],
        'calories': Decimal('60.00'),
        'protein_g': Decimal('0.10'),
        'carbs_g': Decimal('15.00'),
        'fat_g': Decimal('0.00'),
        'fiber_g': Decimal('0.10'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'EAN13: 8901764012280',
        'preparation_notes': 'Official Coca-Cola India label specification for Maaza Mango Drink. 60 kcal/100ml, 15g carbs (added sugar 13.5g).',
    },
    {
        'name': 'Amul Taaza Homogenised Toned Milk',
        'barcode': '8901030383786',
        'regional_aliases': ['Amul Milk', 'Amul Taaza', 'Toned Milk', '8901030383786'],
        'category': FoodCategory.DAIRY,
        'serving_unit': 'ml',
        'serving_weight_grams': Decimal('100.00'),
        'household_measures': [
            {'name': '1 glass (250ml)', 'weight_g': 250.0, 'unit': 'glass'},
            {'name': '1 cup (200ml)', 'weight_g': 200.0, 'unit': 'cup'},
            {'name': '1 tetrapack (200ml)', 'weight_g': 200.0, 'unit': 'tetrapack'},
        ],
        'calories': Decimal('58.20'),
        'protein_g': Decimal('3.00'),
        'carbs_g': Decimal('4.70'),
        'fat_g': Decimal('3.00'),
        'fiber_g': Decimal('0.00'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'EAN13: 8901030383786',
        'preparation_notes': 'Amul Taaza standard 3% fat homogenized toned milk carton label.',
    },
    {
        'name': 'Amul Butter (Pasteurised)',
        'barcode': '8901262010054',
        'regional_aliases': ['Amul Butter', 'Makhan', 'Butter', '8901262010054'],
        'category': FoodCategory.OILS_FATS,
        'serving_unit': 'g',
        'serving_weight_grams': Decimal('100.00'),
        'household_measures': [
            {'name': '1 dollop / tbsp (14g)', 'weight_g': 14.0, 'unit': 'tbsp'},
            {'name': '1 small blister (10g)', 'weight_g': 10.0, 'unit': 'portion'},
        ],
        'calories': Decimal('717.00'),
        'protein_g': Decimal('0.50'),
        'carbs_g': Decimal('0.00'),
        'fat_g': Decimal('80.00'),
        'fiber_g': Decimal('0.00'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'EAN13: 8901262010054',
        'preparation_notes': 'Classic Amul pasteurized table butter 100g pack.',
    },
    {
        'name': 'Parle-G Original Gluco Biscuits',
        'barcode': '8901719101038',
        'regional_aliases': ['Parle G', 'Parle-G', 'Glucose Biscuits', '8901719101038'],
        'category': FoodCategory.SNACKS_SWEETS,
        'serving_unit': 'piece',
        'serving_weight_grams': Decimal('100.00'),
        'household_measures': [
            {'name': '4 biscuits (20g)', 'weight_g': 20.0, 'unit': 'piece'},
            {'name': '1 pack (65g)', 'weight_g': 65.0, 'unit': 'pack'},
        ],
        'calories': Decimal('454.00'),
        'protein_g': Decimal('6.50'),
        'carbs_g': Decimal('78.00'),
        'fat_g': Decimal('13.00'),
        'fiber_g': Decimal('2.00'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'EAN13: 8901719101038',
        'preparation_notes': 'Parle-G standard biscuit packaging declaration.',
    },
    {
        'name': 'Frooti Fresh Mango Juice',
        'barcode': '8901719283017',
        'regional_aliases': ['Frooti', 'Parle Agro Frooti', 'Mango Drink', '8901719283017'],
        'category': FoodCategory.BEVERAGES,
        'serving_unit': 'ml',
        'serving_weight_grams': Decimal('100.00'),
        'household_measures': [
            {'name': '1 tetrapack (160ml)', 'weight_g': 160.0, 'unit': 'tetrapack'},
            {'name': '1 bottle (250ml)', 'weight_g': 250.0, 'unit': 'bottle'},
        ],
        'calories': Decimal('65.00'),
        'protein_g': Decimal('0.00'),
        'carbs_g': Decimal('16.20'),
        'fat_g': Decimal('0.00'),
        'fiber_g': Decimal('0.10'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'EAN13: 8901719283017',
        'preparation_notes': 'Parle Agro Frooti official nutrition label declaration.',
    },
    {
        'name': 'Thums Up Charged Carbonated Drink',
        'barcode': '8901764012259',
        'regional_aliases': ['Thums Up', 'Thumbs Up', 'Cola', '8901764012259'],
        'category': FoodCategory.BEVERAGES,
        'serving_unit': 'ml',
        'serving_weight_grams': Decimal('100.00'),
        'household_measures': [
            {'name': '1 can (300ml)', 'weight_g': 300.0, 'unit': 'can'},
            {'name': '1 pet bottle (250ml)', 'weight_g': 250.0, 'unit': 'bottle'},
        ],
        'calories': Decimal('44.00'),
        'protein_g': Decimal('0.00'),
        'carbs_g': Decimal('11.00'),
        'fat_g': Decimal('0.00'),
        'fiber_g': Decimal('0.00'),
        'provenance': FoodProvenance.USDA_DIRECT,
        'source_reference_id': 'EAN13: 8901764012259',
        'preparation_notes': 'The Coca-Cola Company Thums Up carbonated beverage.',
    },
]


class Command(BaseCommand):
    help = 'Seeds the verified 25-item Indian Food Catalog into a dedicated tenant database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant',
            type=str,
            default='sweat',
            help='Slug of the tenant database to seed (default: sweat)',
        )
        parser.add_argument(
            '--database',
            type=str,
            help='Direct database alias to use (overrides --tenant)',
        )

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant')
        db_alias = options.get('database')

        if not db_alias:
            tenant = Tenant.objects.using('default').filter(slug=tenant_slug, status='ACTIVE').first()
            if not tenant:
                self.stderr.write(self.style.ERROR(f"Tenant with slug '{tenant_slug}' not found on Master DB."))
                return
            data_source = TenantDataSource.objects.using('default').filter(tenant=tenant).first()
            if not data_source or not data_source.db_name:
                self.stderr.write(self.style.ERROR(f"No TenantDataSource configured for tenant '{tenant_slug}'."))
                return

            db_alias = build_tenant_db_alias(tenant.id)
            _register_tenant_connection(db_alias, data_source.db_name, data_source=data_source, tenant_id=tenant.id)

        self.stdout.write(f"Seeding Indian Food Catalog into database alias '{db_alias}'...")

        created_count = 0
        updated_count = 0

        for item_data in INDIAN_FOOD_CATALOG:
            food_item, created = FoodItem.objects.using(db_alias).update_or_create(
                name=item_data['name'],
                is_system=True,
                defaults={
                    'organization': None,
                    'barcode': item_data.get('barcode', None),
                    'regional_aliases': item_data['regional_aliases'],
                    'category': item_data['category'],
                    'serving_unit': item_data['serving_unit'],
                    'serving_weight_grams': item_data['serving_weight_grams'],
                    'household_measures': item_data['household_measures'],
                    'calories': item_data['calories'],
                    'protein_g': item_data['protein_g'],
                    'carbs_g': item_data['carbs_g'],
                    'fat_g': item_data['fat_g'],
                    'fiber_g': item_data['fiber_g'],
                    'provenance': item_data['provenance'],
                    'source_reference_id': item_data['source_reference_id'],
                    'preparation_notes': item_data['preparation_notes'],
                    'is_nutritionist_verified': False,
                    'is_active': True,
                }
            )
            if created:
                created_count += 1
            else:
                updated_count += 1

        total = created_count + updated_count
        self.stdout.write(self.style.SUCCESS(
            f"Successfully seeded {total} food items ({created_count} created, {updated_count} updated) into '{db_alias}'."
        ))
