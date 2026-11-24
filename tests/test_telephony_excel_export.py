"""
backend/tests/test_telephony_excel_export.py
Comprehensive regression tests for Telephony & Call Logs Excel (.xlsx) Export functionality.
Verifies:
- 401 Unauthorized for unauthenticated requests
- 200 OK with proper openxmlformats MIME type and .xlsx filename
- Multi-sheet workbook integrity (Call Logs, Leaderboard, Dispositions, Hourly Stats, Summary)
- Resolution of lead contact names and sales rep data without missing fields
- Proper numeric formatting and openpyxl loadability
"""

import io
import openpyxl
from datetime import timedelta
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APIRequestFactory, force_authenticate

from apps.tenant_core.models_crm import (
    CallSession, Lead, TenantUser, Organization, Branch
)
from apps.tenant_core.models import Location
from apps.tenant_core.views_telephony import (
    TelephonyExportView, resolve_telephony_tenant_context, get_tenant_db_alias, set_tenant_db_alias
)
from apps.tenant_core.services_telephony_export import (
    export_telephony_to_excel_buffer
)


class TelephonyExcelExportTestCase(APITestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        super().setUp()
        set_tenant_db_alias('tenant_test')
        self.factory = APIRequestFactory()
        self.view = TelephonyExportView.as_view()

        self.org = Organization.objects.using('tenant_test').create(
            name="SWEAT Fitness Test Org",
            code="SWEAT_TEST_01",
            status="ACTIVE"
        )
        self.location = Location.objects.using('tenant_test').create(
            organization=self.org,
            name="Mumbai Andheri Test",
            code="LOC_TEST_01",
        )
        self.branch = Branch.objects.using('tenant_test').create(
            organization=self.org,
            location=self.location,
            name="Andheri West Test",
            code="ANDHERI_TEST_01",
        )
        self.rep = TenantUser.objects.using('tenant_test').create(
            organization=self.org,
            email="salesrep.test@sweatfit.com",
            first_name="Praveen",
            last_name="Nair",
            user_type="STAFF",
        )
        self.lead = Lead.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            first_name="Mariam",
            last_name="Darvesh",
            phone_normalized="+919137319246",
            current_status="NEW_LEAD"
        )
        self.call = CallSession.objects.using('tenant_test').create(
            organization=self.org,
            branch=self.branch,
            lead=self.lead,
            sales_rep=self.rep,
            user_phone_number="+919137319246",
            direction="OUTBOUND",
            status="COMPLETED",
            duration_seconds=95.0,
            is_meaningful=True,
            call_mode="HUMAN_MANUAL",
            disposition="TRIAL_BOOKED",
            disposition_notes="Lead agreed to attend intro trial session on Saturday"
        )

    def test_unauthenticated_request_rejected(self):
        """Unauthenticated requests must be rejected with 401."""
        req = self.factory.get('/api/v1/tenant/crm/telephony/export/')
        resp = self.view(req)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_export_returns_valid_xlsx(self):
        """Authenticated requests must return a valid, openable .xlsx file with proper headers."""
        with resolve_telephony_tenant_context():
            req = self.factory.get('/api/v1/tenant/crm/telephony/export/?timeframe=today')
            force_authenticate(req, user=self.rep)
            resp = self.view(req)

            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(
                resp.get('Content-Type'),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            disposition = resp.get('Content-Disposition')
            self.assertIn('.xlsx', disposition)
            self.assertIn('attachment;', disposition)

            # Validate that openpyxl can parse the binary content without errors
            wb = openpyxl.load_workbook(io.BytesIO(resp.content))
            self.assertIn('Call Logs', wb.sheetnames)
            self.assertIn('Rep Leaderboard', wb.sheetnames)
            self.assertIn('Call Dispositions', wb.sheetnames)
            self.assertIn('Hourly Answer Rates', wb.sheetnames)
            self.assertIn('Executive Summary', wb.sheetnames)

            ws_logs = wb['Call Logs']
            header_row = [cell.value for cell in ws_logs[1]]
            self.assertIn('Call ID', header_row)
            self.assertIn('Lead / Contact Name', header_row)
            self.assertIn('Sales Representative', header_row)
            self.assertIn('Duration (Seconds)', header_row)
            self.assertIn('Call Disposition', header_row)

            # Check that data row exists and matches our test call session
            self.assertGreaterEqual(ws_logs.max_row, 2)
            row2 = [cell.value for cell in ws_logs[2]]
            self.assertEqual(row2[0], str(self.call.id))
            self.assertEqual(row2[2], "Mariam Darvesh")
            self.assertEqual(row2[4], "Praveen Nair")
            self.assertEqual(row2[7], "OUTBOUND")
            self.assertEqual(row2[8], "COMPLETED")
            self.assertEqual(row2[9], 95)
            self.assertEqual(row2[10], "1m 35s")
            self.assertEqual(row2[11], "YES")
            self.assertEqual(row2[12], "Trial Booked / Scheduled")
            self.assertEqual(row2[14], "Lead agreed to attend intro trial session on Saturday")

    def test_export_type_call_logs_returns_call_logs_workbook(self):
        """When export_type=call_logs, response filename and sheets reflect Call Logs."""
        with resolve_telephony_tenant_context():
            req = self.factory.get('/api/v1/tenant/crm/telephony/export/?timeframe=today&export_type=call_logs')
            force_authenticate(req, user=self.rep)
            resp = self.view(req)

            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            disposition = resp.get('Content-Disposition')
            self.assertIn('call_logs_export', disposition)
            self.assertIn('.xlsx', disposition)

            wb = openpyxl.load_workbook(io.BytesIO(resp.content))
            self.assertIn('Call Logs', wb.sheetnames)

    def test_buffer_generation_without_lead_foreign_key_resolves_phone(self):
        """Verify that export_telephony_to_excel_buffer resolves lead name via phone number when lead foreign key is null."""
        with resolve_telephony_tenant_context():
            # Create a call session with null lead FK, but matching phone
            orphan_call = CallSession.objects.using('tenant_test').create(
                organization=self.org,
                user_phone_number="+919137319246",
                direction="INBOUND",
                status="COMPLETED",
                duration_seconds=42.0,
                is_meaningful=True,
                call_mode="HUMAN_MANUAL",
                disposition="INTERESTED_FOLLOWUP"
            )

            try:
                qs = CallSession.objects.using('tenant_test').filter(id=orphan_call.id)
                xlsx_bytes = export_telephony_to_excel_buffer(
                    qs=qs,
                    db_alias='tenant_test',
                    org=self.org,
                    timeframe='today'
                )

                wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
                ws = wb['Call Logs']
                row2 = [cell.value for cell in ws[2]]

                # Resolved lead name should be "Mariam Darvesh" from phone match!
                self.assertEqual(row2[2], "Mariam Darvesh")
                self.assertEqual(row2[7], "INBOUND")
                self.assertEqual(row2[9], 42)
                self.assertEqual(row2[10], "0m 42s")
            finally:
                orphan_call.delete()
