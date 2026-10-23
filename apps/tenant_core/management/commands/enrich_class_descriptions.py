"""
Management command to enrich ClassTemplate descriptions with detailed workout overviews.
"""

from django.core.management.base import BaseCommand
from apps.master.models_tenant import Tenant
from apps.authentication.views import _register_and_resolve_tenant
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_classes import ClassTemplate


CLASS_DESCRIPTIONS = {
    'SWEAT_TOTAL_PILATES': (
        "A comprehensive full-body reformer Pilates session designed to sculpt, tone, and strengthen. "
        "Targets deep core stability, postural alignment, and muscular endurance through controlled resistance "
        "and flowing transitions suitable for all fitness levels."
    ),
    'SWEAT_STRETCH': (
        "A restorative low-impact session dedicated to joint mobility, active flexibility, and spine decompression. "
        "Designed to relieve muscle tightness, improve range of motion in the hips and hamstrings, and accelerate physical recovery."
    ),
    'SWEAT_ATHLETIC': (
        "A high-energy, athletic-conditioning reformer workout combining dynamic compound sequences with progressive resistance. "
        "Emphasizes power output, agility, core stability, and cardiovascular conditioning."
    ),
    'BOOTCAMP': (
        "An intense functional training and metabolic conditioning session combining strength stations, bodyweight intervals, "
        "and high-energy circuits to maximize cardiovascular burn and athletic resilience."
    ),
    'BOOTCAMP_TOTAL': (
        "A total-body conditioning workout integrating foundational strength lifts with high-intensity interval finishers "
        "for balanced functional strength and endurance."
    ),
    'SWEAT_TOTAL_BOOTCAMP': (
        "A high-tempo full-body bootcamp workout focusing on explosive movements, upper and lower body compound supersets, "
        "and functional stamina."
    ),
    'SWEAT_MOBILITY': (
        "A targeted movement session focusing on functional joint mobility, hip openers, thoracic spine extension, "
        "and active flexibility to optimize movement quality and prevent training injuries."
    ),
    'SWEAT_LIFT': (
        "A strength-focused progressive overload class utilizing dumbbells, kettlebells, and functional equipment. "
        "Designed to build lean muscle mass, reinforce lifting technique, and enhance total-body power."
    ),
    'BODYPROCOACH_TOTAL': (
        "Signature BodyProCoach total-body strength and conditioning routine integrating movement mechanics, "
        "functional power, and core endurance."
    ),
}

# Generic fallbacks by keyword
KEYWORD_DESCRIPTIONS = {
    'stretch': "Dedicated flexibility and mobility flow designed to release joint tension, improve flexibility, and restore muscles.",
    'athletic': "Dynamic high-intensity session combining athletic strength work with endurance intervals.",
    'mobility': "Focused functional mobility and active recovery session targeting joint range of motion and injury prevention.",
    'lift': "Structured resistance training workout prioritizing form, progressive overload, and power development.",
    'bootcamp': "High-energy functional circuit combining cardio drills, functional strength, and core conditioning.",
    'total': "Comprehensive full-body workout targeting major muscle groups, functional stamina, and core stability.",
    'pilates': "Classic and contemporary reformer Pilates targeting core engagement, posture, balance, and lean muscle tone.",
}


class Command(BaseCommand):
    help = 'Enriches ClassTemplate descriptions with comprehensive session explanations'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', type=str, default='sweat-demo', help='Tenant slug')

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant') or 'sweat-demo'
        tenant = Tenant.objects.get(slug=tenant_slug)
        alias = _register_and_resolve_tenant(tenant)
        set_tenant_db_alias(alias)

        classes = ClassTemplate.objects.using(alias).all()
        updated_count = 0

        for c in classes:
            code_upper = (c.code or '').upper().strip()
            name_lower = (c.name or '').lower()

            desc = None
            if code_upper in CLASS_DESCRIPTIONS:
                desc = CLASS_DESCRIPTIONS[code_upper]
            else:
                for kw, kw_desc in KEYWORD_DESCRIPTIONS.items():
                    if kw in name_lower:
                        desc = f"{c.name}: {kw_desc}"
                        break

            if not desc:
                desc = f"{c.name}: High-performance coached fitness session targeting strength, mobility, and cardiovascular health."

            c.description = desc
            c.save(using=alias, update_fields=['description'])
            updated_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Successfully enriched descriptions for {updated_count} ClassTemplates in {alias}."
        ))
