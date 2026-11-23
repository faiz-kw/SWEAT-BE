"""
Regression tests for the trial-booking count annotated onto class occurrences
(apps.tenant_core.views_classes.annotate_trial_booking_count).

TrialBooking.class_occurrence_id is a plain UUID column (not a ForeignKey), so trials never show up
in `occurrence.bookings`. The class list therefore needs a subquery annotation to count them.

Trials that hold a seat: BOOKED, CONFIRMED, ATTENDED, NO_SHOW, CONVERTED.
Trials that released it:  CANCELLED, RESCHEDULED.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models_classes import (
    ClassCategory,
    ClassOccurrence,
    ClassTemplate,
)
from apps.tenant_core.models_crm import Lead, TrialBooking
from apps.tenant_core.models_org import Branch, Location, Organization
from apps.tenant_core.views_classes import annotate_trial_booking_count

ALIAS = 'tenant_test'


class TrialBookingCountAnnotationTests(TestCase):
    databases = {'default', ALIAS}

    def setUp(self):
        set_tenant_db_alias(ALIAS)
        self.org = Organization.objects.using(ALIAS).create(code='TC-ORG', name='Trial Count Org', status='ACTIVE')
        location = Location.objects.using(ALIAS).create(
            organization=self.org, code='TC-LOC', name='Trial Count Location', status='ACTIVE',
        )
        self.branch = Branch.objects.using(ALIAS).create(
            organization=self.org, location=location, code='TC-BR', name='Trial Count Branch', status='ACTIVE',
        )
        category = ClassCategory.objects.using(ALIAS).create(organization=self.org, code='PILATES', name='Pilates')
        template = ClassTemplate.objects.using(ALIAS).create(
            organization=self.org, category=category, code='SWEAT-STRETCH', name='Sweat Stretch', status='ACTIVE',
        )
        self.end = timezone.now() - timedelta(hours=3)
        self.start = self.end - timedelta(hours=1)
        self.occ = self._occurrence(template)
        self.other_occ = self._occurrence(template)
        self.empty_occ = self._occurrence(template)
        self._lead_seq = 0

    def _occurrence(self, template):
        return ClassOccurrence.objects.using(ALIAS).create(
            class_template=template, branch=self.branch, occurrence_date=self.start.date(),
            start_at=self.start, end_at=self.end, capacity=7, trial_capacity=2, status='SCHEDULED',
        )

    def _trial(self, occurrence, status):
        self._lead_seq += 1
        lead = Lead.objects.using(ALIAS).create(
            organization=self.org, branch=self.branch, first_name='Trial', last_name=f'Lead{self._lead_seq}',
            email_normalized=f'trial{self._lead_seq}@tc.test', current_status='TRIAL_BOOKED',
        )
        return TrialBooking.objects.using(ALIAS).create(
            lead=lead, branch=self.branch, class_occurrence_id=occurrence.id,
            scheduled_start=self.start, scheduled_end=self.end, status=status,
        )

    def _counts(self):
        qs = annotate_trial_booking_count(ClassOccurrence.objects.using(ALIAS).all(), ALIAS)
        return {occ.id: occ.annotated_trial_count for occ in qs}

    def test_seat_holding_trials_are_counted_and_released_ones_are_not(self):
        for status in ('BOOKED', 'CONFIRMED', 'ATTENDED', 'NO_SHOW', 'CONVERTED'):
            self._trial(self.occ, status)
        for status in ('CANCELLED', 'RESCHEDULED'):
            self._trial(self.occ, status)
        self.assertEqual(self._counts()[self.occ.id], 5)

    def test_trials_are_counted_per_occurrence(self):
        self._trial(self.occ, 'NO_SHOW')
        self._trial(self.occ, 'NO_SHOW')
        self._trial(self.other_occ, 'BOOKED')
        counts = self._counts()
        self.assertEqual(counts[self.occ.id], 2)
        self.assertEqual(counts[self.other_occ.id], 1)

    def test_occurrence_without_trials_is_zero_not_null(self):
        self._trial(self.occ, 'NO_SHOW')
        self.assertEqual(self._counts()[self.empty_occ.id], 0)
