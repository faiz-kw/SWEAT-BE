"""
Joins the two assessment migration leaves.

The v1-alignment work (governance fields, previous-assessment link, opt-in consents, the
progression model) was branched from 0088 and landed as 0089/0090 on its own line, while the
body-scan work followed main through 0089-0092 and landed as 0093. Both touch
MemberAssessment but their field sets are disjoint, so this merge is a graph join with no
operations of its own.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tenant_core', '0093_assessment_body_scan'),
        ('tenant_core', '0090_assessment_v1_alignment'),
    ]

    operations = []
