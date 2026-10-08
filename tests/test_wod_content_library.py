"""
Comprehensive Verification Tests for Workout of the Day (WOD) — Phase 1:
Admin-Only Workout Content Library, Configurable Tag Master, Video Upload & Link Download, and Excel Import.

Covers all 23 required specification test cases + video upload & link auto-download verification:
1. Create tag in PROGRAM group
2. Create tag in SECTION group
3. Prevent duplicate tag code in same group + organization
4. Allow same tag code in different tag group
5. Allow same tag code in different organization
6. Create movement content item with multiple tags
7. Create movement with multiple SECTION tags
8. Create movement with multiple MUSCLE_GROUP / BODY_TARGET tags
9. Create setup content item (content_kind = SETUP, is_wod_eligible = FALSE)
10. Reject marking SETUP item as WOD eligible
11. Reject marking item with missing video_url/video as WOD eligible
12. Reject marking item with missing SECTION tag as WOD eligible
13. Reject marking INACTIVE item as WOD eligible
14. Mark complete movement as WOD eligible
15. Filter movements by PROGRAM tag
16. Filter movements by SECTION tag
17. Filter movements by INTENSITY tag
18. Filter movements by is_wod_eligible
19. Excel import preview classifies movements vs setup items
20. Excel import normalizes multi-value tags and downloads video from link
21. Non-admin role gets 403 on all WOD endpoints
22. Tenant/org admin gets 200/201 on WOD endpoints
23. Cross-organization isolation works
24. Direct video file upload stores video and streams via stream-video endpoint
25. Video link auto-download handles Google Drive / direct video links and flags folder/broken links
"""

import io
import zipfile
from unittest.mock import MagicMock, patch
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from apps.authentication.views import _build_tenant_token
from apps.master.models import (
    ProductModule,
    SaasPlan,
    Tenant,
    TenantDataSource,
    TenantModule,
    TenantSubscription,
)
from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_org import Branch, Location, Organization
from apps.tenant_core.models_rbac import (
    ModuleCatalog,
    Permission,
    Role,
    RoleAssignment,
    RoleModuleAccess,
    RolePermissionSet,
    RolePermissionSetItem,
    RoleSubmoduleAccess,
    SubmoduleCatalog,
)
from apps.tenant_core.models_users import TenantUser
from apps.tenant_core.models_wod import WorkoutContentItem, WorkoutContentTag, WorkoutTag


def build_minimal_xlsx_bytes(headers: list[str], rows: list[list[str]]) -> bytes:
    """Builds a valid in-memory .xlsx zip archive for testing Excel import."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            '[Content_Types].xml',
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '</Types>',
        )
        all_rows = [headers] + rows
        row_xml_parts = []
        for r_idx, r_vals in enumerate(all_rows, start=1):
            cells_xml = []
            for c_idx, val in enumerate(r_vals):
                col_letter = chr(ord('A') + c_idx) if c_idx < 26 else f"A{chr(ord('A') + c_idx - 26)}"
                escaped = (
                    str(val)
                    .replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;')
                )
                cells_xml.append(
                    f'<c r="{col_letter}{r_idx}" t="inlineStr"><is><t>{escaped}</t></is></c>'
                )
            row_xml_parts.append(f'<row r="{r_idx}">{"".join(cells_xml)}</row>')

        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(row_xml_parts)}</sheetData>'
            '</worksheet>'
        )
        zf.writestr('xl/worksheets/sheet1.xml', sheet_xml)
    return buf.getvalue()


class WODContentLibraryTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')

        # 1. Master Tenant Setup
        self.tenant = Tenant.objects.using('default').create(
            code='SWEAT-WOD',
            name='Sweat WOD Studio',
            slug='sweat-wod-studio',
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
            name='WOD Enterprise Plan',
            code='WOD-PLAN',
            tier='ENTERPRISE',
            status='ACTIVE',
        )
        self.sub = TenantSubscription.objects.using('default').create(
            tenant=self.tenant,
            plan=self.plan,
            status='ACTIVE',
        )
        self.prod_mod_ops, _ = ProductModule.objects.using('default').get_or_create(
            code='ops',
            defaults={'name': 'Operations', 'status': 'ACTIVE'},
        )
        TenantModule.objects.using('default').create(
            tenant=self.tenant,
            module=self.prod_mod_ops,
            is_enabled=True,
            availability_mode='ALL_BRANCHES',
        )

        # 2. Organization A & B in Tenant DB
        self.org = Organization.objects.using('tenant_test').create(
            code='SWEAT-ORG-A',
            name='Sweat Organization A',
            status='ACTIVE',
        )
        self.org_b = Organization.objects.using('tenant_test').create(
            code='SWEAT-ORG-B',
            name='Sweat Organization B',
            status='ACTIVE',
        )
        self.loc = Location.objects.using('tenant_test').create(
            organization=self.org,
            code='LOC-MUM-WEST',
            name='Mumbai West',
            city='Mumbai',
            state='Maharashtra',
            status='ACTIVE',
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.loc,
            code='BR-WOD-1',
            name='Bandra Studio',
            status='ACTIVE',
        )

        # 3. Module Catalog in Tenant DB
        self.mod_ops = ModuleCatalog.objects.using('tenant_test').create(
            code='ops',
            module_code='ops',
            name='Operations',
            is_enabled=True,
            status='ACTIVE',
        )
        self.sub_classes = SubmoduleCatalog.objects.using('tenant_test').create(
            module=self.mod_ops,
            code='classes',
            submodule_code='classes',
            name='Classes',
            is_enabled=True,
            status='ACTIVE',
        )

        # 4. Roles & Users: ORG_ADMIN vs TRAINER (Non-Admin) vs Org B Admin
        self.admin_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Organization Admin',
            code='ORG_ADMIN',
            scope='ORG',
            is_system_role=True,
            status='ACTIVE',
        )
        self.trainer_role = Role.objects.using('tenant_test').create(
            organization=self.org,
            name='Trainer',
            code='TRAINER',
            scope='BRANCH',
            status='ACTIVE',
        )
        RoleModuleAccess.objects.using('tenant_test').create(
            role=self.trainer_role,
            module=self.mod_ops,
            can_access=True,
        )
        RoleSubmoduleAccess.objects.using('tenant_test').create(
            role=self.trainer_role,
            submodule=self.sub_classes,
            can_access=True,
        )

        self.admin_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='admin@sweatwod.com',
            username='admin_wod',
            first_name='Org',
            last_name='Admin',
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.admin_user,
            role=self.admin_role,
            scope_type='ORGANIZATION',
            is_active=True,
            status='ACTIVE',
        )

        self.trainer_user = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email='trainer@sweatwod.com',
            username='trainer_wod',
            first_name='Trainer',
            last_name='User',
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.trainer_user,
            role=self.trainer_role,
            branch=self.branch,
            scope_type='BRANCH',
            is_active=True,
            status='ACTIVE',
        )

        # Org B Admin
        self.org_b_admin_role = Role.objects.using('tenant_test').create(
            organization=self.org_b,
            name='Org B Admin',
            code='ORG_ADMIN',
            scope='ORG',
            is_system_role=True,
            status='ACTIVE',
        )
        self.org_b_admin = TenantUser.objects.using('tenant_test').create(
            organization=self.org_b,
            email='admin@orgb.com',
            username='admin_orgb',
            first_name='OrgB',
            last_name='Admin',
            status='ACTIVE',
            is_login_allowed=True,
        )
        RoleAssignment.objects.using('tenant_test').create(
            user=self.org_b_admin,
            role=self.org_b_admin_role,
            scope_type='ORGANIZATION',
            is_active=True,
            status='ACTIVE',
        )

        # Build JWT tokens
        self.admin_token = str(_build_tenant_token(self.admin_user, self.tenant, 'tenant_test').access_token)
        self.trainer_token = str(_build_tenant_token(self.trainer_user, self.tenant, 'tenant_test').access_token)
        self.org_b_token = str(_build_tenant_token(self.org_b_admin, self.tenant, 'tenant_test').access_token)

        self.auth_admin()

    def auth_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.admin_token}')

    def auth_trainer(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.trainer_token}')

    def auth_org_b(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.org_b_token}')

    def _mock_video_download_success(self, mock_session_cls):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {'Content-Type': 'video/mp4'}
        mock_resp.cookies = {}
        mock_resp.iter_content.return_value = [b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom']
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

    def _get_or_create_required_movement_tags(self):
        # Trigger tag seeding via GET /api/v1/tenant/wod/tags/
        self.client.get('/api/v1/tenant/wod/tags/')
        prog = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='PROGRAM', name='Sweat Stretch').first()
        sec1 = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='SECTION', name='Warm Up').first()
        sec2 = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='SECTION', name='Stretch').first()
        inten = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='INTENSITY', name='Moderate').first()
        m1 = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='MUSCLE_GROUP', name='Full Body').first()
        m2 = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='BODY_TARGET', name='Balance').first()
        breath = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='BREATHING', name='Exhale - Natural').first()
        return prog, sec1, sec2, inten, m1, m2, breath

    # -----------------------------------------------------------------------
    # Tests 1 - 5: Tag Master & Uniqueness Constraints
    # -----------------------------------------------------------------------

    def test_01_create_tag_in_program_group(self):
        res = self.client.post(
            '/api/v1/tenant/wod/tags/',
            {'tag_group': 'PROGRAM', 'name': 'Sweat Reformer Pro', 'code': 'SWEAT_REFORMER_PRO'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['tag_group'], 'PROGRAM')
        self.assertEqual(res.data['code'], 'SWEAT_REFORMER_PRO')

    def test_02_create_tag_in_section_group(self):
        res = self.client.post(
            '/api/v1/tenant/wod/tags/',
            {'tag_group': 'SECTION', 'name': 'Finisher Burn'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['tag_group'], 'SECTION')
        self.assertEqual(res.data['code'], 'FINISHER_BURN')

    def test_03_prevent_duplicate_tag_code_in_same_group_and_organization(self):
        res1 = self.client.post(
            '/api/v1/tenant/wod/tags/',
            {'tag_group': 'EQUIPMENT', 'name': 'Kettlebell', 'code': 'KETTLEBELL'},
            format='json',
        )
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        res2 = self.client.post(
            '/api/v1/tenant/wod/tags/',
            {'tag_group': 'EQUIPMENT', 'name': 'Kettlebell Duplicate', 'code': 'KETTLEBELL'},
            format='json',
        )
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_04_allow_same_tag_code_in_different_tag_group(self):
        res1 = self.client.post(
            '/api/v1/tenant/wod/tags/',
            {'tag_group': 'SECTION', 'name': 'Core Special', 'code': 'CORE_SPECIAL'},
            format='json',
        )
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        res2 = self.client.post(
            '/api/v1/tenant/wod/tags/',
            {'tag_group': 'MUSCLE_GROUP', 'name': 'Core Special', 'code': 'CORE_SPECIAL'},
            format='json',
        )
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)

    def test_05_allow_same_tag_code_in_different_organization(self):
        res_a = self.client.post(
            '/api/v1/tenant/wod/tags/',
            {'tag_group': 'PROGRAM', 'name': 'Signature Pilates', 'code': 'SIG_PILATES'},
            format='json',
        )
        self.assertEqual(res_a.status_code, status.HTTP_201_CREATED)

        self.auth_org_b()
        res_b = self.client.post(
            '/api/v1/tenant/wod/tags/',
            {'tag_group': 'PROGRAM', 'name': 'Signature Pilates', 'code': 'SIG_PILATES'},
            format='json',
        )
        self.assertEqual(res_b.status_code, status.HTTP_201_CREATED)

    # -----------------------------------------------------------------------
    # Tests 6 - 14: Content Items, Multi-Tagging, Setup Rules & WOD Eligibility
    # -----------------------------------------------------------------------

    @patch('apps.tenant_core.services_wod.requests.Session')
    def test_06_07_08_create_movement_with_multiple_section_and_muscle_tags(self, mock_session_cls):
        self._mock_video_download_success(mock_session_cls)
        prog, sec1, sec2, inten, m1, m2, breath = self._get_or_create_required_movement_tags()

        res = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {
                'movement_name': 'Star Pose Right',
                'content_kind': 'MOVEMENT',
                'status': 'ACTIVE',
                'video_url': 'https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view',
                'cue_1': 'Engage core and lift ribs',
                'cue_2': 'Extend top arm toward ceiling',
                'tag_ids': [
                    str(prog.id),
                    str(sec1.id),
                    str(sec2.id),
                    str(inten.id),
                    str(m1.id),
                    str(m2.id),
                    str(breath.id),
                ],
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        data = res.data
        self.assertEqual(data['movement_name'], 'Star Pose Right')
        self.assertEqual(data['classification_status'], 'COMPLETE')
        self.assertEqual(data['video_download_status'], 'COMPLETED')
        self.assertTrue(data['has_stored_video'])

        # Verify multiple SECTION tags (Warm Up, Stretch)
        section_names = {t['name'] for t in data['tags_by_group'].get('SECTION', [])}
        self.assertEqual(section_names, {'Warm Up', 'Stretch'})

        # Verify multiple MUSCLE_GROUP / BODY_TARGET tags (Full Body, Balance)
        muscle_names = {t['name'] for t in data['tags_by_group'].get('MUSCLE_GROUP', [])}
        target_names = {t['name'] for t in data['tags_by_group'].get('BODY_TARGET', [])}
        self.assertIn('Full Body', muscle_names)
        self.assertIn('Balance', target_names)

    @patch('apps.tenant_core.services_wod.requests.Session')
    def test_09_10_setup_content_item_and_reject_wod_eligibility(self, mock_session_cls):
        self._mock_video_download_success(mock_session_cls)
        res = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {
                'movement_name': 'How to wear Loop Band',
                'content_kind': 'SETUP',
                'status': 'ACTIVE',
                'video_url': 'https://drive.google.com/file/d/1SetupLoopBandVideoId/view',
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        item_id = res.data['id']
        self.assertEqual(res.data['content_kind'], 'SETUP')
        self.assertFalse(res.data['is_wod_eligible'])

        # Test 10: Reject marking SETUP item as WOD eligible
        elig_res = self.client.post(
            f'/api/v1/tenant/wod/content-items/{item_id}/set-wod-eligibility/',
            {'is_wod_eligible': True},
            format='json',
        )
        self.assertEqual(elig_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Only MOVEMENT items can be marked as WOD eligible', str(elig_res.data))

    def test_11_reject_marking_item_with_missing_video_as_wod_eligible(self):
        prog, sec1, _, inten, m1, _, _ = self._get_or_create_required_movement_tags()
        res = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {
                'movement_name': 'Plank Pike Without Video',
                'content_kind': 'MOVEMENT',
                'status': 'ACTIVE',
                'tag_ids': [str(prog.id), str(sec1.id), str(inten.id), str(m1.id)],
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['classification_status'], 'INCOMPLETE')

        elig_res = self.client.post(
            f"/api/v1/tenant/wod/content-items/{res.data['id']}/set-wod-eligibility/",
            {'is_wod_eligible': True},
            format='json',
        )
        self.assertEqual(elig_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Video', str(elig_res.data))

    @patch('apps.tenant_core.services_wod.requests.Session')
    def test_12_reject_marking_item_with_missing_section_tag_as_wod_eligible(self, mock_session_cls):
        self._mock_video_download_success(mock_session_cls)
        prog, _, _, inten, m1, _, _ = self._get_or_create_required_movement_tags()
        res = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {
                'movement_name': 'Movement Missing Section',
                'content_kind': 'MOVEMENT',
                'status': 'ACTIVE',
                'video_url': 'https://drive.google.com/file/d/1ValidVideoId123/view',
                'tag_ids': [str(prog.id), str(inten.id), str(m1.id)],  # No SECTION tag
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['classification_status'], 'INCOMPLETE')

        elig_res = self.client.post(
            f"/api/v1/tenant/wod/content-items/{res.data['id']}/set-wod-eligibility/",
            {'is_wod_eligible': True},
            format='json',
        )
        self.assertEqual(elig_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('SECTION', str(elig_res.data))

    @patch('apps.tenant_core.services_wod.requests.Session')
    def test_13_14_reject_inactive_and_allow_complete_active_movement_wod_eligible(self, mock_session_cls):
        self._mock_video_download_success(mock_session_cls)
        prog, sec1, _, inten, m1, _, _ = self._get_or_create_required_movement_tags()

        res = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {
                'movement_name': 'Complete Squat Press',
                'content_kind': 'MOVEMENT',
                'status': 'INACTIVE',
                'video_url': 'https://drive.google.com/file/d/1ValidSquatVideo/view',
                'tag_ids': [str(prog.id), str(sec1.id), str(inten.id), str(m1.id)],
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        item_id = res.data['id']

        # Test 13: Reject marking INACTIVE item as WOD eligible
        inelig_res = self.client.post(
            f'/api/v1/tenant/wod/content-items/{item_id}/set-wod-eligibility/',
            {'is_wod_eligible': True},
            format='json',
        )
        self.assertEqual(inelig_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('ACTIVE', str(inelig_res.data))

        # Activate item then mark WOD eligible (Test 14)
        act_res = self.client.post(f'/api/v1/tenant/wod/content-items/{item_id}/activate/')
        self.assertEqual(act_res.status_code, status.HTTP_200_OK)
        self.assertEqual(act_res.data['status'], 'ACTIVE')

        elig_res = self.client.post(
            f'/api/v1/tenant/wod/content-items/{item_id}/set-wod-eligibility/',
            {'is_wod_eligible': True},
            format='json',
        )
        self.assertEqual(elig_res.status_code, status.HTTP_200_OK)
        self.assertTrue(elig_res.data['is_wod_eligible'])

    # -----------------------------------------------------------------------
    # Tests 15 - 18: Filtering by PROGRAM, SECTION, INTENSITY, is_wod_eligible
    # -----------------------------------------------------------------------

    @patch('apps.tenant_core.services_wod.requests.Session')
    def test_15_16_17_18_filtering_movements(self, mock_session_cls):
        self._mock_video_download_success(mock_session_cls)
        prog_stretch, sec_warm, sec_stretch, inten_mod, m_full, _, _ = self._get_or_create_required_movement_tags()
        prog_athletic = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='PROGRAM', name='Sweat Athletic').first()
        sec_main = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='SECTION', name='Main Zone').first()
        inten_high = WorkoutTag.objects.using('tenant_test').filter(organization=self.org, tag_group='INTENSITY', name='High').first()

        # Item 1: Sweat Stretch, Warm Up, Moderate, WOD Eligible = True
        r1 = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {
                'movement_name': 'Cat Cow Stretch',
                'content_kind': 'MOVEMENT',
                'status': 'ACTIVE',
                'video_url': 'https://drive.google.com/file/d/1CatCowVideo/view',
                'tag_ids': [str(prog_stretch.id), str(sec_warm.id), str(inten_mod.id), str(m_full.id)],
                'is_wod_eligible': True,
            },
            format='json',
        )
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)

        # Item 2: Sweat Athletic, Main Zone, High, WOD Eligible = False
        r2 = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {
                'movement_name': 'Explosive Box Jump',
                'content_kind': 'MOVEMENT',
                'status': 'ACTIVE',
                'video_url': 'https://drive.google.com/file/d/1BoxJumpVideo/view',
                'tag_ids': [str(prog_athletic.id), str(sec_main.id), str(inten_high.id), str(m_full.id)],
                'is_wod_eligible': False,
            },
            format='json',
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)

        # Test 15: Filter by PROGRAM tag
        f_prog = self.client.get('/api/v1/tenant/wod/content-items/', {'program': 'Sweat Stretch'})
        self.assertEqual(f_prog.status_code, status.HTTP_200_OK)
        names_prog = [i['movement_name'] for i in f_prog.data['results']]
        self.assertEqual(names_prog, ['Cat Cow Stretch'])

        # Test 16: Filter by SECTION tag
        f_sec = self.client.get('/api/v1/tenant/wod/content-items/', {'section': 'Main Zone'})
        names_sec = [i['movement_name'] for i in f_sec.data['results']]
        self.assertEqual(names_sec, ['Explosive Box Jump'])

        # Test 17: Filter by INTENSITY tag
        f_int = self.client.get('/api/v1/tenant/wod/content-items/', {'intensity': 'High'})
        names_int = [i['movement_name'] for i in f_int.data['results']]
        self.assertEqual(names_int, ['Explosive Box Jump'])

        # Test 18: Filter by is_wod_eligible
        f_elig = self.client.get('/api/v1/tenant/wod/content-items/', {'is_wod_eligible': 'true'})
        names_elig = [i['movement_name'] for i in f_elig.data['results']]
        self.assertEqual(names_elig, ['Cat Cow Stretch'])

    # -----------------------------------------------------------------------
    # Tests 19 - 20: Excel Import Preview & Confirm + Video Auto-Download
    # -----------------------------------------------------------------------

    @patch('apps.tenant_core.services_wod.requests.Session')
    def test_19_20_excel_import_preview_and_confirm_normalizes_tags_and_downloads_videos(self, mock_session_cls):
        self._mock_video_download_success(mock_session_cls)
        headers = [
            'Name of the movement',
            'Ideal for | Program',
            'Movement ideal for | Section',
            'Intensity level',
            'Muscle working',
            'Breathing',
            'Resistance | Springs',
            'Video Link (Edited)',
            'Cues 1',
            'Edit checked',
        ]
        rows = [
            [
                'Star Pose Right',
                'Sweat Stretch, Sweat Total',
                'Warm Up, Stretch',
                'Moderate',
                'Full Body, Balance',
                'Exhale - Natural',
                '1 Red 1 Blue',
                'https://drive.google.com/file/d/1StarPoseRightVideo/view',
                'Reach tall through crown',
                'Yes',
            ],
            [
                'How to set up Footbar',
                'Sweat Stretch',
                '',
                '',
                '',
                '',
                '',
                'https://drive.google.com/file/d/1SetupFootbarVideo/view',
                'Lock footbar into slot 2',
                'Yes',
            ],
        ]
        xlsx_bytes = build_minimal_xlsx_bytes(headers, rows)
        upload = SimpleUploadedFile(
            'wod_library.xlsx',
            xlsx_bytes,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        # Test 19: Preview classifies movements vs setup items
        prev_res = self.client.post(
            '/api/v1/tenant/wod/content-items/import/preview/',
            {'file': upload},
            format='multipart',
        )
        self.assertEqual(prev_res.status_code, status.HTTP_200_OK, prev_res.data)
        summary = prev_res.data['summary']
        self.assertEqual(summary['total_rows'], 2)
        self.assertEqual(summary['movement_rows'], 1)
        self.assertEqual(summary['setup_instruction_rows'], 1)

        row_star = prev_res.data['rows'][0]
        row_setup = prev_res.data['rows'][1]
        self.assertEqual(row_star['content_kind'], 'MOVEMENT')
        self.assertEqual(row_setup['content_kind'], 'SETUP')
        self.assertFalse(row_setup['is_wod_eligible'])

        # Test 20: Confirm import normalizes multi-value tags and downloads video from link
        conf_res = self.client.post(
            '/api/v1/tenant/wod/content-items/import/confirm/',
            {'rows': prev_res.data['rows'], 'auto_download_videos': True},
            format='json',
        )
        self.assertEqual(conf_res.status_code, status.HTTP_201_CREATED, conf_res.data)
        self.assertEqual(conf_res.data['created_count'], 2)
        self.assertEqual(conf_res.data['downloaded_videos_count'], 2)

        star_item = WorkoutContentItem.objects.using('tenant_test').get(
            organization=self.org, movement_name='Star Pose Right'
        )
        self.assertEqual(star_item.classification_status, 'COMPLETE')
        self.assertEqual(star_item.video_download_status, 'COMPLETED')
        self.assertTrue(star_item.has_stored_video)

        attached_tag_names = set(
            WorkoutContentTag.objects.using('tenant_test')
            .filter(content_item=star_item)
            .values_list('tag__name', flat=True)
        )
        self.assertTrue(
            {'Sweat Stretch', 'Sweat Total', 'Warm Up', 'Stretch', 'Moderate', 'Full Body', 'Balance', 'Exhale - Natural'}.issubset(
                attached_tag_names
            )
        )

    # -----------------------------------------------------------------------
    # Tests 21 - 23: Role-Based Access Control (403 Non-Admin, 200 Admin, Cross-Org Isolation)
    # -----------------------------------------------------------------------

    def test_21_non_admin_role_gets_403_on_all_wod_endpoints(self):
        self.auth_trainer()

        r_list = self.client.get('/api/v1/tenant/wod/content-items/')
        self.assertEqual(r_list.status_code, status.HTTP_403_FORBIDDEN)

        r_create = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {'movement_name': 'Unauthorized Movement'},
            format='json',
        )
        self.assertEqual(r_create.status_code, status.HTTP_403_FORBIDDEN)

        r_tags = self.client.get('/api/v1/tenant/wod/tags/')
        self.assertEqual(r_tags.status_code, status.HTTP_403_FORBIDDEN)

        r_tag_create = self.client.post(
            '/api/v1/tenant/wod/tags/',
            {'tag_group': 'PROGRAM', 'name': 'Hack'},
            format='json',
        )
        self.assertEqual(r_tag_create.status_code, status.HTTP_403_FORBIDDEN)

        r_import = self.client.post('/api/v1/tenant/wod/content-items/import/preview/', {'rows': []}, format='json')
        self.assertEqual(r_import.status_code, status.HTTP_403_FORBIDDEN)

    def test_22_23_admin_access_and_cross_organization_isolation(self):
        self.auth_admin()
        res_a = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {'movement_name': 'Org A Exclusive Movement', 'content_kind': 'MOVEMENT'},
            format='json',
        )
        self.assertEqual(res_a.status_code, status.HTTP_201_CREATED)
        org_a_item_id = res_a.data['id']

        # Switch to Org B Admin: cannot see or edit Org A's content item
        self.auth_org_b()
        list_b = self.client.get('/api/v1/tenant/wod/content-items/')
        self.assertEqual(list_b.status_code, status.HTTP_200_OK)
        self.assertEqual(list_b.data['count'], 0)

        detail_b = self.client.get(f'/api/v1/tenant/wod/content-items/{org_a_item_id}/')
        self.assertEqual(detail_b.status_code, status.HTTP_404_NOT_FOUND)

    # -----------------------------------------------------------------------
    # Tests 24 - 25: Direct Video Upload & Link Download Edge Cases
    # -----------------------------------------------------------------------

    def test_24_direct_video_upload_and_streaming(self):
        self.auth_admin()
        prog, sec1, _, inten, m1, _, _ = self._get_or_create_required_movement_tags()

        video_file = SimpleUploadedFile(
            'reformer_lunge.mp4',
            b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom_test_video_payload',
            content_type='video/mp4',
        )
        res = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {
                'movement_name': 'Reformer Reverse Lunge',
                'content_kind': 'MOVEMENT',
                'status': 'ACTIVE',
                'video_file_upload': video_file,
                'tag_ids': f'["{prog.id}", "{sec1.id}", "{inten.id}", "{m1.id}"]',
            },
            format='multipart',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['video_provider'], 'UPLOAD')
        self.assertEqual(res.data['video_download_status'], 'COMPLETED')
        self.assertTrue(res.data['has_stored_video'])
        self.assertEqual(res.data['classification_status'], 'COMPLETE')

        # Stream the uploaded video
        from django.core.signals import request_finished
        from django.db import close_old_connections

        stream_res = self.client.get(f"/api/v1/tenant/wod/content-items/{res.data['id']}/stream-video/")
        self.assertEqual(stream_res.status_code, status.HTTP_200_OK)
        request_finished.disconnect(close_old_connections)
        try:
            stream_res.close()
        finally:
            request_finished.connect(close_old_connections)

    def test_25_google_drive_folder_link_flagged_as_failed_and_needs_review(self):
        self.auth_admin()
        prog, sec1, _, inten, m1, _, _ = self._get_or_create_required_movement_tags()

        res = self.client.post(
            '/api/v1/tenant/wod/content-items/',
            {
                'movement_name': 'Movement With Folder Link',
                'content_kind': 'MOVEMENT',
                'status': 'ACTIVE',
                'video_url': 'https://drive.google.com/drive/folders/1FolderIdNotAFile',
                'tag_ids': [str(prog.id), str(sec1.id), str(inten.id), str(m1.id)],
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['video_download_status'], 'FAILED')
        self.assertEqual(res.data['classification_status'], 'NEEDS_REVIEW')
        self.assertFalse(res.data['is_wod_eligible'])
