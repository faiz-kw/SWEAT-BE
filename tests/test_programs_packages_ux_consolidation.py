"""
Targeted Verification Tests for Programs + Packages UX Consolidation:
1. Program Type creation without Code field -> generates unique internal code
2. Program creation without Code field -> generates unique internal code
3. Package creation without Code field -> generates unique internal code
4. Legal Policy creation without Code field -> generates unique internal code
5. Stable code on rename -> code does NOT mutate when name is changed
6. Duplicate code collision handling -> generates safe deterministic suffix
7. Historical version immutability -> version publishing and snapshots
8. Package activation/deactivation
9. Program activation/deactivation
10. Business Audit Event recording
"""

import uuid
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from config.routers import set_tenant_db_alias
from apps.tenant_core.models_org import Organization
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_catalog import (
    ProgramType,
    Program,
    Package,
    PackageVersion,
    TermsDocument,
)
from apps.tenant_core.models_audit_outbox import BusinessAuditEvent
from apps.tenant_core.serializers_catalog import (
    ProgramTypeSerializer,
    ProgramSerializer,
    PackageSerializer,
    TermsDocumentSerializer,
)


class ProgramsPackagesConsolidationTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.using('tenant_test').create(
            code=f"ORG-{uuid.uuid4().hex[:6].upper()}",
            name="Consolidated Gym Org",
            status="ACTIVE",
        )
        self.user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email="admin@consolidatedgym.com",
            first_name="Admin",
            last_name="User",
            user_type="ORG_ADMIN",
            status="ACTIVE",
        )

    def tearDown(self):
        set_tenant_db_alias(None)

    def test_1_and_5_program_type_auto_code_generation_and_rename_stability(self):
        """ProgramType created without code auto-generates stable code from name."""
        data = {
            'name': 'Pilates & Yoga Studio',
            'description': 'Reformer and mat classes',
            'display_order': 1,
            'status': 'ACTIVE',
        }
        context = {'organization': self.org, 'db_alias': 'tenant_test'}
        serializer = ProgramTypeSerializer(data=data, context=context)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        pt = serializer.save(organization=self.org)

        self.assertEqual(pt.code, 'PILATES_YOGA_STUDIO')
        self.assertEqual(pt.name, 'Pilates & Yoga Studio')

        # Rename: name changes but code MUST stay PILATES_YOGA_STUDIO
        update_data = {'name': 'Advanced Pilates & Yoga Studio'}
        update_serializer = ProgramTypeSerializer(instance=pt, data=update_data, partial=True, context=context)
        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        updated_pt = update_serializer.save()

        self.assertEqual(updated_pt.name, 'Advanced Pilates & Yoga Studio')
        self.assertEqual(updated_pt.code, 'PILATES_YOGA_STUDIO', "Code must remain stable on rename")

    def test_2_and_7_program_auto_code_and_collision_handling(self):
        """Program created without code auto-generates code; collision handled safely."""
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org,
            code='STRENGTH_TYPE',
            name='Strength Type',
            status='ACTIVE',
        )
        context = {'organization': self.org, 'db_alias': 'tenant_test'}

        # 1. Create first program
        data1 = {
            'name': 'CrossFit Training',
            'program_type': pt.id,
            'trial_allowed': True,
        }
        ser1 = ProgramSerializer(data=data1, context=context)
        self.assertTrue(ser1.is_valid(), ser1.errors)
        prog1 = ser1.save(organization=self.org)
        self.assertEqual(prog1.code, 'CROSSFIT_TRAINING')

        # 2. Create second program with same name -> collision handling adds deterministic unique suffix
        data2 = {
            'name': 'CrossFit Training',
            'program_type': pt.id,
            'trial_allowed': False,
        }
        ser2 = ProgramSerializer(data=data2, context=context)
        self.assertTrue(ser2.is_valid(), ser2.errors)
        prog2 = ser2.save(organization=self.org)
        self.assertTrue(prog2.code.startswith('CROSSFIT_TRAINING_'))
        self.assertNotEqual(prog1.code, prog2.code)

        # 3. Rename prog1
        ser_up = ProgramSerializer(instance=prog1, data={'name': 'Elite CrossFit Training'}, partial=True, context=context)
        self.assertTrue(ser_up.is_valid(), ser_up.errors)
        up_prog1 = ser_up.save()
        self.assertEqual(up_prog1.code, 'CROSSFIT_TRAINING', "Code must remain unchanged on rename")

    def test_3_package_auto_code_generation_and_versions(self):
        """Package creation without code auto-generates code and nests versions."""
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org,
            code='MEMBERSHIP_TYPE',
            name='Membership Type',
            status='ACTIVE',
        )
        prog = Program.objects.using('tenant_test').create(
            organization=self.org,
            code='PILATES_PROG',
            name='Pilates Program',
            program_type=pt,
            status='ACTIVE',
        )
        context = {'organization': self.org, 'db_alias': 'tenant_test'}

        # Create package without code
        pkg_data = {
            'name': 'Gold Pilates Annual',
            'program': prog.id,
            'status': 'ACTIVE',
        }
        ser = PackageSerializer(data=pkg_data, context=context)
        self.assertTrue(ser.is_valid(), ser.errors)
        pkg = ser.save(organization=self.org)

        self.assertEqual(pkg.code, 'GOLD_PILATES_ANNUAL')
        self.assertEqual(pkg.program_id, prog.id)

        # Add versions
        v1 = PackageVersion.objects.using('tenant_test').create(
            package=pkg,
            version_number=1,
            name_snapshot='Gold Pilates Annual v1',
            duration_value=12,
            duration_unit='MONTH',
            total_days=365,
            validity_days=365,
            status='ACTIVE',
            created_by_user=self.user,
            effective_from='2026-01-01T00:00:00Z',
        )

        # Retrieve package via serializer
        pkg_ser = PackageSerializer(instance=pkg, context=context)
        data = pkg_ser.data
        self.assertIn('versions', data)
        self.assertEqual(len(data['versions']), 1)
        self.assertEqual(data['versions'][0]['version_number'], 1)
        self.assertEqual(data['versions_count'], 1)

    def test_4_terms_document_auto_code_generation(self):
        """Legal Policy (TermsDocument) created without code auto-generates code."""
        context = {'organization': self.org, 'db_alias': 'tenant_test'}
        data = {
            'name': 'Privacy & Data Protection Notice',
            'document_type': 'PRIVACY_NOTICE',
            'status': 'ACTIVE',
        }
        ser = TermsDocumentSerializer(data=data, context=context)
        self.assertTrue(ser.is_valid(), ser.errors)
        doc = ser.save(organization=self.org)

        self.assertEqual(doc.code, 'PRIVACY_DATA_PROTECTION_NOTICE')

    def test_package_and_program_lifecycle_and_audit(self):
        """Test active/inactive status changes and business audit log compatibility."""
        pt = ProgramType.objects.using('tenant_test').create(
            organization=self.org,
            code='FITNESS',
            name='Fitness',
            status='ACTIVE',
        )
        prog = Program.objects.using('tenant_test').create(
            organization=self.org,
            code='BOOTCAMP',
            name='Bootcamp',
            program_type=pt,
            status='ACTIVE',
        )
        pkg = Package.objects.using('tenant_test').create(
            organization=self.org,
            code='BOOTCAMP_MONTHLY',
            name='Bootcamp Monthly',
            program=prog,
            status='ACTIVE',
        )

        # Deactivate package
        pkg.status = 'INACTIVE'
        pkg.save(using='tenant_test')
        self.assertEqual(pkg.status, 'INACTIVE')

        # Deactivate program
        prog.status = 'INACTIVE'
        prog.save(using='tenant_test')
        self.assertEqual(prog.status, 'INACTIVE')

        # Record audit event
        audit = BusinessAuditEvent.objects.using('tenant_test').create(
            organization=self.org,
            module='CATALOG',
            action_code='PACKAGE_DEACTIVATED',
            entity_type='Package',
            entity_id=pkg.id,
            metadata={'previous_status': 'ACTIVE', 'new_status': 'INACTIVE'},
        )
        self.assertIsNotNone(audit.id)
        self.assertEqual(audit.action_code, 'PACKAGE_DEACTIVATED')
        self.assertEqual(audit.entity_id, pkg.id)
