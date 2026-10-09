# -*- coding: utf-8 -*-

"""

tests/test_parq_purchase_flow.py  Connected PAR-Q submission and purchase consent flow tests.

"""

from decimal import Decimal

import uuid

from unittest.mock import patch

from django.utils import timezone

from rest_framework.test import APITestCase

from django.core.exceptions import ValidationError



from apps.master.models import Tenant, TenantDataSource, ProductModule, TenantModule, SaasPlan, TenantSubscription

from apps.tenant_core.context import set_tenant_db_alias

from apps.tenant_core.models_org import Organization, Location, Branch

from apps.tenant_core.models_crm import Lead, IntakeForm, IntakeQuestion, IntakeSubmission, IntakeAnswer

from apps.tenant_core.models_catalog import ProgramCategory, Program, Package, PackageVersion, PackagePrice

from apps.tenant_core.models_workforce import UserProfile

from apps.tenant_core.models_users import TenantUser

from apps.tenant_core.services_crm import CRMLeadService, LeadConversionService

from apps.tenant_core.views_members import build_member_360_aggregate





class ParqPurchaseFlowTestCase(APITestCase):

    databases = {'default', 'tenant_test'}



    def setUp(self):

        self.alias = 'tenant_test'

        set_tenant_db_alias(self.alias)



        # 1. Master Tenant Setup

        self.tenant, _ = Tenant.objects.using('default').get_or_create(

            name="SWEAT Fitness Org",

            slug="sweat-fitness-org",

            status="ACTIVE",

            defaults={"code": "SWEAT-ORG"}

        )

        self.ds, _ = TenantDataSource.objects.using('default').get_or_create(

            tenant=self.tenant,

            defaults={"db_name": "test_fitness_tenant", "database_name": "test_fitness_tenant", "status": "ACTIVE"}

        )

        self.plan, _ = SaasPlan.objects.using('default').get_or_create(

            code='ENTERPRISE-SWEAT',

            defaults={'name': 'Enterprise Plan', 'tier': 'ENTERPRISE', 'status': 'ACTIVE'}

        )

        self.sub, _ = TenantSubscription.objects.using('default').get_or_create(

            tenant=self.tenant,

            defaults={'plan': self.plan, 'status': 'ACTIVE'}

        )



        # 2. Tenant Org & Branch

        self.org, _ = Organization.objects.using(self.alias).get_or_create(

            name="SWEAT Fitness Organization",

            defaults={"code": "SWEAT_ORG", "status": "ACTIVE"}

        )

        self.loc, _ = Location.objects.using(self.alias).get_or_create(

            organization=self.org,

            name="SWEAT City Center",

            defaults={"code": "CITY_CTR"}

        )

        self.branch, _ = Branch.objects.using(self.alias).get_or_create(

            organization=self.org,

            location=self.loc,

            name="SWEAT Flagship",

            defaults={"code": "FLG", "status": "ACTIVE"}

        )



        # 3. Users

        self.admin_user, _ = TenantUser.objects.using(self.alias).get_or_create(

            email="admin@sweat.test",

            defaults={"username": "sweat_admin", "status": "ACTIVE", "organization": self.org}

        )



        # 4. Catalog: Program, Package, Version, Price

        self.cat, _ = ProgramCategory.objects.using(self.alias).get_or_create(

            organization=self.org,

            name="Athletic Training",

            defaults={"code": "ATHLETIC"}

        )

        self.program = Program.objects.using(self.alias).create(

            organization=self.org,

            category=self.cat,

            name="SWEAT Strength & Conditioning",

            code=f"PRG-{uuid.uuid4().hex[:4].upper()}",

            status="ACTIVE"

        )

        self.package = Package.objects.using(self.alias).create(

            organization=self.org,

            program=self.program,

            name="Quarterly Athlete",

            code=f"PKG-{uuid.uuid4().hex[:4].upper()}",

            status="ACTIVE"

        )

        self.pv = PackageVersion.objects.using(self.alias).create(

            package=self.package,

            version_number=1,

            status='ACTIVE',

            duration_value=3,

            duration_unit='MONTH',

            effective_from=timezone.now(),

            created_by_user=self.admin_user

        )

        self.price = PackagePrice.objects.using(self.alias).create(

            package_version=self.pv,

            branch=self.branch,

            currency='INR',

            base_price=Decimal('15000.00'),

            tax_percent=Decimal('18.00'),

            prices_include_tax=False,

            effective_from=timezone.now(),

            created_by_user=self.admin_user

        )



        # 5. Lead

        self.lead = Lead.objects.using(self.alias).create(

            organization=self.org,

            branch=self.branch,

            first_name="Rohan",

            last_name="Sharma",

            email_normalized=f"rohan.{uuid.uuid4().hex[:6]}@example.com",

            phone_normalized=f"+9198765{uuid.uuid4().hex[:5]}",

            interested_program=self.program,

            current_status='NEW_LEAD'

        )



        # 6. Active PAR-Q Form

        self.form = IntakeForm.objects.using(self.alias).create(

            organization=self.org,

            name="PAR-Q  UAT Demo",

            form_type="PAR_Q",

            version_number=1,

            status="ACTIVE",

            agreement_title="Physical Activity Readiness & Assumption of Risk Agreement",

            agreement_text="I confirm that all statements provided are true and I voluntarily assume all risks of exercise.",

            is_required_for_purchase=True,

            requires_explicit_consent=True,

            reassessment_days=365,

            is_default_for_all_programs=True

        )



        self.q1 = IntakeQuestion.objects.using(self.alias).create(

            intake_form=self.form,

            question_text="Do you have any heart condition?",

            question_type="BOOLEAN",

            category="MEDICAL",

            is_required=True,

            is_sensitive=True,

            display_order=1,

            status="ACTIVE"

        )



        self.q2 = IntakeQuestion.objects.using(self.alias).create(

            intake_form=self.form,

            question_text="Are you taking any prescription medication?",

            question_type="BOOLEAN",

            category="MEDICAL",

            is_required=True,

            is_sensitive=True,

            display_order=2,

            status="ACTIVE"

        )



        self.q3 = IntakeQuestion.objects.using(self.alias).create(

            intake_form=self.form,

            question_text="Additional health notes",

            question_type="TEXT",

            category="GENERAL",

            is_required=False,

            is_sensitive=False,

            display_order=3,

            status="ACTIVE"

        )



    def test_01_resolve_applicable_parq(self):

        """Applicable PAR-Q resolves correctly for program and organization."""

        res = CRMLeadService.resolve_applicable_parq(

            organization=self.org,

            program_id=str(self.program.id),

            lead_id=str(self.lead.id),

            db_alias=self.alias

        )

        self.assertTrue(res['configured'])

        self.assertTrue(res['requires_completion'])

        self.assertFalse(res['is_reusable'])

        self.assertEqual(res['form']['name'], "PAR-Q  UAT Demo")

        self.assertEqual(len(res['form']['questions']), 3)

        self.assertEqual(res['agreement_title'], "Physical Activity Readiness & Assumption of Risk Agreement")



    def test_02_required_question_validation(self):

        """Unanswered required questions block submission."""

        with self.assertRaises(ValidationError) as ctx:

            CRMLeadService.submit_purchase_parq(

                intake_form=self.form,

                answers=[{'question_id': str(self.q1.id), 'boolean_value': True}],  # Missing q2

                lead=self.lead,

                agreement_accepted=True,

                db_alias=self.alias

            )

        self.assertIn("required", str(ctx.exception).lower())



    def test_03_explicit_consent_validation(self):

        """Missing agreement acceptance blocks submission when configured."""

        with self.assertRaises(ValidationError) as ctx:

            CRMLeadService.submit_purchase_parq(

                intake_form=self.form,

                answers=[

                    {'question_id': str(self.q1.id), 'boolean_value': False},

                    {'question_id': str(self.q2.id), 'boolean_value': False},

                ],

                lead=self.lead,

                agreement_accepted=False,  # NOT ACCEPTED

                db_alias=self.alias

            )

        self.assertIn("explicit agreement acceptance is required", str(ctx.exception).lower())



    def test_04_successful_submission_and_immutability(self):

        """PAR-Q submission stores immutable snapshot and preserves pre-payment state."""

        idem_key = f"idem-{uuid.uuid4().hex}"

        sub = CRMLeadService.submit_purchase_parq(

            intake_form=self.form,

            answers=[

                {'question_id': str(self.q1.id), 'boolean_value': False},

                {'question_id': str(self.q2.id), 'boolean_value': True},

                {'question_id': str(self.q3.id), 'text_value': "Occasional knee stiffness"},

            ],

            lead=self.lead,

            agreement_accepted=True,

            agreement_text_snapshot=self.form.agreement_text,

            accepted_by_name="Rohan Sharma",

            signer_type="MEMBER_DIRECT",

            channel="STAFF_ASSISTED",

            idempotency_key=idem_key,

            db_alias=self.alias

        )



        self.assertIsNotNone(sub.id)

        self.assertEqual(sub.status, 'COMPLETED')

        self.assertTrue(sub.agreement_accepted)

        self.assertIsNotNone(sub.agreement_accepted_at)

        self.assertIsNone(sub.membership)  # Membership is NOT created upon form completion

        self.assertEqual(sub.answers.using(self.alias).count(), 3)

        self.assertEqual(sub.form_snapshot['version_number'], 1)

        self.assertEqual(len(sub.form_snapshot['questions']), 3)



        # Idempotency replay check

        sub_replay = CRMLeadService.submit_purchase_parq(

            intake_form=self.form,

            answers=[],

            lead=self.lead,

            idempotency_key=idem_key,

            db_alias=self.alias

        )

        self.assertEqual(sub.id, sub_replay.id)



    def test_05_member_360_projection_and_rbac(self):

        """Member 360 projects PAR-Q submission and enforces cs.member-health.view permission."""

        sub = CRMLeadService.submit_purchase_parq(

            intake_form=self.form,

            answers=[

                {'question_id': str(self.q1.id), 'boolean_value': False},

                {'question_id': str(self.q2.id), 'boolean_value': True},

            ],

            lead=self.lead,

            agreement_accepted=True,

            db_alias=self.alias

        )



        user_profile, _ = LeadConversionService.resolve_or_create_member_identity(

            lead=self.lead,

            branch=self.branch,

            db_alias=self.alias

        )

        sub.user_profile = user_profile

        sub.save(using=self.alias, update_fields=['user_profile'])



        # 1. Authorized health viewer (cs.member-health.view = True)

        user_health = TenantUser.objects.using(self.alias).create(

            username=f"doc_{uuid.uuid4().hex[:6]}",
            email=f"doc_{uuid.uuid4().hex[:6]}@example.com",

            display_name="Dr. Jane",

            status="ACTIVE",

            organization=self.org

        )

        with patch('apps.tenant_core.rbac_engine.RBACAuthorizationEngine.evaluate', return_value=(True, 'Allowed', None)):

            m360_auth = build_member_360_aggregate(user_profile, self.alias, requesting_user=user_health)

            self.assertIn('par_q_form', m360_auth)

            self.assertIn('health_and_forms', m360_auth)

            self.assertEqual(len(m360_auth['par_q_form']['submissions']), 1)

            sub_item = m360_auth['par_q_form']['submissions'][0]

            self.assertEqual(sub_item['form_title'], "PAR-Q  UAT Demo")

            self.assertFalse(sub_item['sensitive_data_restricted'])

            self.assertEqual(len(sub_item['answers']), 2)



        # 2. Unauthorized staff without cs.member-health.view

        user_sales = TenantUser.objects.using(self.alias).create(

            username=f"sales_{uuid.uuid4().hex[:6]}",
            email=f"sales_{uuid.uuid4().hex[:6]}@example.com",

            display_name="Bob Sales",

            status="ACTIVE",

            organization=self.org

        )

        with patch('apps.tenant_core.rbac_engine.RBACAuthorizationEngine.evaluate', return_value=(False, 'Denied', None)):

            m360_anon = build_member_360_aggregate(user_profile, self.alias, requesting_user=user_sales)

            sub_anon = m360_anon['par_q_form']['submissions'][0]

            self.assertTrue(sub_anon['sensitive_data_restricted'])

            self.assertEqual(len(sub_anon['answers']), 0)  # Sensitive answers redacted!

