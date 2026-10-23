"""
Management command to seed the 5-Step Member Onboarding & Goal Assessment Questionnaire.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.master.models_tenant import Tenant
from apps.authentication.views import _register_and_resolve_tenant
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_crm import IntakeForm, IntakeQuestion, IntakeQuestionOption


class Command(BaseCommand):
    help = 'Seeds the 5-step Member Onboarding & Goal Assessment Questionnaire with rich metadata'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', type=str, default='sweat-demo', help='Tenant slug')

    def handle(self, *args, **options):
        tenant_slug = options.get('tenant') or 'sweat-demo'
        tenant = Tenant.objects.get(slug=tenant_slug)
        alias = _register_and_resolve_tenant(tenant)
        set_tenant_db_alias(alias)

        org = Organization.objects.using(alias).first()
        if not org:
            self.stdout.write(self.style.ERROR(f"No Organization found in {alias}"))
            return

        self.stdout.write(f"Seeding Onboarding Form for Org: {org.name} ({alias})")

        # Create or update authoritative ONBOARDING form
        agreement_text = (
            "I confirm that the information I have provided is accurate to the best of my knowledge.\n"
            "I understand that exercise and recovery activities may involve risks, including possible injury.\n"
            "I will follow the instructions provided and stop if I experience pain, dizziness or discomfort.\n"
            "The app provides fitness and wellness guidance, not medical advice.\n"
            "I will consult a healthcare professional if I have any medical condition, injury or concern.\n\n"
            "I HAVE READ, UNDERSTOOD AND AGREE TO THE HEALTH & SAFETY TERMS AND ASSUME RESPONSIBILITY "
            "FOR PARTICIPATING IN SWEAT ACTIVITIES."
        )

        form, created = IntakeForm.objects.using(alias).get_or_create(
            organization=org,
            form_type='ONBOARDING',
            version_number=1,
            defaults={
                'name': 'SWEAT Member Onboarding & Goal Assessment',
                'status': 'ACTIVE',
                'agreement_title': 'A Safer SWEAT Experience',
                'agreement_text': agreement_text,
                'is_required_for_purchase': False,
                'requires_explicit_consent': True,
                'effective_from': timezone.now(),
            }
        )
        if not created:
            form.name = 'SWEAT Member Onboarding & Goal Assessment'
            form.status = 'ACTIVE'
            form.agreement_title = 'A Safer SWEAT Experience'
            form.agreement_text = agreement_text
            form.save(using=alias)

        # Remove existing questions to allow clean idempotency
        IntakeQuestion.objects.using(alias).filter(intake_form=form).delete()

        questions_def = [
            {
                'text': "LET'S MAKE IT PERSONAL: What would you like to work towards?",
                'type': 'SINGLE_SELECT',
                'category': 'FITNESS',
                'display_order': 1,
                'required': True,
                'sensitive': False,
                'options': [
                    ("BUILD STRENGTH /// Get stronger and more powerful /// icon:dumbbell", "BUILD_STRENGTH"),
                    ("TONE & SHAPE /// Lean, sculpted and confident /// icon:tone", "TONE_AND_SHAPE"),
                    ("IMPROVE FLEXIBILITY /// Move better with less stiffness /// icon:flexibility", "IMPROVE_FLEXIBILITY"),
                    ("BOOST FITNESS & STAMINA /// More energy for everyday life /// icon:stamina", "BOOST_FITNESS_STAMINA"),
                    ("RECOVER & RECHARGE /// Reduce stress and feel your best /// icon:recovery", "RECOVER_AND_RECHARGE"),
                    ("BUILD A CONSISTENT ROUTINE /// Stay active and make it a habit /// icon:routine", "CONSISTENT_ROUTINE"),
                ]
            },
            {
                'text': "WHERE ARE YOU ON YOUR JOURNEY? We'll tailor your experience to your level.",
                'type': 'SINGLE_SELECT',
                'category': 'FITNESS',
                'display_order': 2,
                'required': True,
                'sensitive': False,
                'options': [
                    ("JUST GETTING STARTED /// New to fitness or returning after a break /// bars:1", "BEGINNER"),
                    ("FINDING MY RHYTHM /// A few classes and building consistency /// bars:2", "INTERMEDIATE"),
                    ("CONSISTENT AND PROGRESSING /// Regular workouts and seeing results /// bars:3", "ADVANCED"),
                    ("EXPERIENCED AND READY FOR MORE /// Confident with training and looking for advanced challenges /// bars:4", "EXPERT"),
                ]
            },
            {
                'text': "WHAT WOULD YOU LIKE TO FOCUS ON? Choose one or more areas that matter most to you.",
                'type': 'MULTI_SELECT',
                'category': 'FITNESS',
                'display_order': 3,
                'required': True,
                'sensitive': False,
                'options': [
                    ("FULL-BODY FITNESS /// Total body conditioning and vitality /// img:full_body", "FULL_BODY"),
                    ("STRENGTH & CONDITIONING /// Progressive load and muscle endurance /// img:strength", "STRENGTH_CONDITIONING"),
                    ("CORE & STABILITY /// Midsection power, posture and balance /// img:core", "CORE_STABILITY"),
                    ("MOBILITY & FLEXIBILITY /// Joint health and active range of motion /// img:mobility", "MOBILITY_FLEXIBILITY"),
                    ("STRESS RELIEF & RECOVERY /// Nervous system reset and tension release /// img:recovery", "STRESS_RELIEF_RECOVERY"),
                    ("ENERGY & WELLBEING /// Daily vitality and mental clarity /// img:energy", "ENERGY_WELLBEING"),
                ]
            },
            {
                'text': "HOW OFTEN WOULD YOU LIKE TO SWEAT? Choose a rhythm that works for your life.",
                'type': 'SINGLE_SELECT',
                'category': 'LIFESTYLE',
                'display_order': 4,
                'required': True,
                'sensitive': False,
                'options': [
                    ("1 - 2 DAYS / WEEK /// A lighter pace to start or maintain /// bars:1", "1_2_DAYS"),
                    ("3 DAYS / WEEK /// A great balance for visible progress /// RECOMMENDED /// bars:2", "3_DAYS"),
                    ("4 DAYS / WEEK /// Accelerate your results /// bars:3", "4_DAYS"),
                    ("5+ DAYS / WEEK /// For maximum progress and performance /// bars:4", "5_PLUS_DAYS"),
                ]
            },
            {
                'text': "ANYTHING WE SHOULD KEEP IN MIND? Help us create a safer and more personalised experience.",
                'type': 'MULTI_SELECT',
                'category': 'MEDICAL',
                'display_order': 5,
                'required': False,
                'sensitive': True,
                'options': [
                    ("INJURIES OR MOVEMENT LIMITATIONS /// E.g. knee, back, shoulder etc. /// icon:injury", "INJURIES_LIMITATIONS"),
                    ("AREAS THAT NEED EXTRA CARE /// E.g. neck, lower back, hips /// icon:target", "AREAS_EXTRA_CARE"),
                    ("HEALTH CONSIDERATIONS /// E.g. medical conditions, pregnancy /// icon:heart", "HEALTH_CONSIDERATIONS"),
                    ("ANYTHING ELSE YOU'D LIKE YOUR COACH TO KNOW /// E.g. personal goals, preferences /// icon:chat", "COACH_NOTES"),
                    ("NOTHING TO SHARE RIGHT NOW /// I'm ready to get started /// icon:check /// EXCLUSIVE", "NONE"),
                ]
            },
        ]

        for q_data in questions_def:
            q = IntakeQuestion.objects.using(alias).create(
                intake_form=form,
                question_text=q_data['text'],
                question_type=q_data['type'],
                category=q_data['category'],
                display_order=q_data['display_order'],
                is_required=q_data['required'],
                is_sensitive=q_data['sensitive'],
                status='ACTIVE',
            )
            for idx, (lbl, val) in enumerate(q_data['options'], 1):
                IntakeQuestionOption.objects.using(alias).create(
                    question=q,
                    label=lbl,
                    value=val,
                    display_order=idx,
                    status='ACTIVE',
                )

        self.stdout.write(self.style.SUCCESS(
            f"Successfully seeded Onboarding Form '{form.name}' with {len(questions_def)} questions."
        ))
