from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Parallel-proof guard against duplicate class occurrences.

    Adds a PARTIAL unique constraint so at most one ACTIVE occurrence can exist per
    (branch, class_template, start_at). Cancelled/deactivated rows are exempt, so
    history (and the rows we deactivated during cleanup) is preserved.

    NOTE: this will fail to apply if any ACTIVE duplicate rows still exist for the same
    (branch, class_template, start_at). The schedule de-duplication must be completed
    first (including the booked-wave conflicts) so there are zero active collisions.
    """

    dependencies = [
        ('tenant_core', '0082_alter_attendancepolicyset_normal_max_advance_bookings_and_more'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='classoccurrence',
            constraint=models.UniqueConstraint(
                fields=['branch', 'class_template', 'start_at'],
                condition=~models.Q(status__in=['CANCELLED', 'DEACTIVATED']),
                name='uniq_active_occurrence_branch_tpl_start',
            ),
        ),
    ]
