"""
Regression tests for the class list "Booked" count (ClassOccurrenceSerializer.get_booking_count).

Rules under test:
* NO_SHOW bookings held a seat, so they ARE counted.
* CANCELLED / RESCHEDULED / WAITLISTED bookings released the seat, so they are NOT counted.
* A queryset annotation (annotated_booking_count), when present, takes precedence.

These tests use lightweight stand-ins instead of database rows, so they need no database.
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.tenant_core.serializers_classes import ClassOccurrenceSerializer


class _FakeBookingManager:
    def __init__(self, statuses):
        self._rows = [SimpleNamespace(status=status) for status in statuses]

    def all(self):
        return self._rows


def _occurrence(statuses, annotated_booking_count=None):
    return SimpleNamespace(
        annotated_booking_count=annotated_booking_count,
        bookings=_FakeBookingManager(statuses),
    )


class ClassOccurrenceBookingCountTests(SimpleTestCase):
    def setUp(self):
        self.serializer = ClassOccurrenceSerializer()

    def test_no_show_bookings_are_counted(self):
        occ = _occurrence(['NO_SHOW', 'NO_SHOW'])
        self.assertEqual(self.serializer.get_booking_count(occ), 2)

    def test_cancelled_booking_is_not_counted(self):
        occ = _occurrence(['CONFIRMED', 'CANCELLED'])
        self.assertEqual(self.serializer.get_booking_count(occ), 1)

    def test_only_cancelled_bookings_count_as_zero(self):
        occ = _occurrence(['CANCELLED', 'CANCELLED'])
        self.assertEqual(self.serializer.get_booking_count(occ), 0)

    def test_released_statuses_are_excluded_and_seat_holding_statuses_included(self):
        occ = _occurrence([
            'CONFIRMED', 'COMPLETED', 'NO_SHOW',          # hold a seat
            'CANCELLED', 'RESCHEDULED', 'WAITLISTED',     # seat released / not seated
        ])
        self.assertEqual(self.serializer.get_booking_count(occ), 3)

    def test_annotated_count_takes_precedence(self):
        occ = _occurrence(['NO_SHOW', 'NO_SHOW'], annotated_booking_count=9)
        self.assertEqual(self.serializer.get_booking_count(occ), 9)

    def test_trial_bookings_are_added_to_member_bookings(self):
        # 5 member bookings (all no-show) + 2 trial bookings = 7, same as the Details modal.
        occ = _occurrence(['NO_SHOW'] * 5)
        occ.annotated_trial_count = 2
        self.assertEqual(self.serializer.get_booking_count(occ), 7)

    def test_missing_trial_annotation_counts_members_only(self):
        occ = _occurrence(['NO_SHOW'] * 5)
        self.assertEqual(self.serializer.get_booking_count(occ), 5)

    def test_member_and_trial_split_adds_up_to_booking_count(self):
        occ = _occurrence(['NO_SHOW', 'CONFIRMED', 'COMPLETED', 'NO_SHOW', 'NO_SHOW', 'CANCELLED'])
        occ.annotated_trial_count = 2
        self.assertEqual(self.serializer.get_member_booking_count(occ), 5)
        self.assertEqual(self.serializer.get_trial_booking_count(occ), 2)
        self.assertEqual(self.serializer.get_booking_count(occ), 7)

    def test_split_without_trial_annotation(self):
        occ = _occurrence(['CONFIRMED', 'CANCELLED'])
        self.assertEqual(self.serializer.get_member_booking_count(occ), 1)
        self.assertEqual(self.serializer.get_trial_booking_count(occ), 0)

    def test_null_trial_annotation_is_treated_as_zero(self):
        occ = _occurrence(['CONFIRMED', 'CONFIRMED'])
        occ.annotated_trial_count = None
        self.assertEqual(self.serializer.get_booking_count(occ), 2)
