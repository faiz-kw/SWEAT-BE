"""
apps/tenant_core/tasks_body_scan.py — Background execution of the assessment body scan.

A four-view scan means four provider round-trips, which is far too slow to hold an HTTP
request open for. The capture endpoint stores the photos, marks the assessment QUEUED and
hands off to this task; the studio UI polls the scan endpoint until it settles.

Failure is always recorded on the assessment itself, never swallowed: a coach standing at the
machine needs to know whether to retake the photos or carry on without the scan.
"""

import logging

from celery import shared_task
from django.utils import timezone

from apps.tenant_core.context import tenant_database_context
from config.routers import TenantRoutingError, get_tenant_db_alias

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks_body_scan.run_assessment_body_scan',
    max_retries=2,
    default_retry_delay=20,
    acks_late=True,
)
def run_assessment_body_scan(self, tenant_id: str = None, assessment_id: str = None, **kwargs) -> dict:
    """
    Runs the four-view scan for one assessment inside its tenant database context.
    """
    if not tenant_id or not assessment_id:
        logger.error("Body scan task failed closed: tenant_id and assessment_id are both required.")
        return {'status': 'FAILED', 'error': 'tenant_id and assessment_id are required'}

    from .models_assessment import MemberAssessment
    from .services_body_scan import BodyScanService, get_scan_client

    try:
        with tenant_database_context(tenant_id):
            alias = get_tenant_db_alias()
            if not alias or alias == 'default':
                logger.error("Body scan task rejected: refusing to run against the master database.")
                return {'status': 'FAILED', 'error': 'invalid tenant alias'}

            assessment = MemberAssessment.objects.using(alias).filter(id=assessment_id).first()
            if not assessment:
                logger.warning("Body scan task: assessment %s no longer exists.", assessment_id)
                return {'status': 'FAILED', 'error': 'assessment not found'}

            assessment.scan_status = 'PROCESSING'
            assessment.scan_started_at = assessment.scan_started_at or timezone.now()
            assessment.save(using=alias, update_fields=['scan_status', 'scan_started_at', 'updated_at'])

            try:
                result = BodyScanService.analyse(assessment, client=get_scan_client())
                BodyScanService.apply_to_assessment(assessment, result, db_alias=alias)
            except Exception as exc:  # provider or parsing failure
                logger.exception("Body scan failed for assessment %s", assessment_id)
                assessment.scan_status = 'FAILED'
                assessment.scan_error = str(exc)[:2000]
                assessment.scan_completed_at = timezone.now()
                assessment.save(
                    using=alias,
                    update_fields=['scan_status', 'scan_error', 'scan_completed_at', 'updated_at'],
                )
                raise

            try:
                from .services_reliability import record_business_audit
                record_business_audit(
                    organization=assessment.organization,
                    module='core',
                    action_code='ASSESSMENT_BODY_SCAN',
                    entity_type='MemberAssessment',
                    entity_id=assessment.id,
                    actor_user=assessment.conducted_by_trainer,
                    event_description=(
                        f"Body scan {assessment.scan_status.lower()} for {assessment.member_name} "
                        f"across {len(result.get('views_usable') or [])} usable view(s)."
                    ),
                    after_data={
                        'scan_status': assessment.scan_status,
                        'views_submitted': result.get('views_submitted'),
                        'views_usable': result.get('views_usable'),
                        'model': result.get('model'),
                    },
                    db_alias=alias,
                )
            except Exception:
                logger.warning("Body scan audit record failed for assessment %s", assessment_id, exc_info=True)

            return {
                'status': assessment.scan_status,
                'assessment_id': str(assessment.id),
                'views_usable': result.get('views_usable'),
            }

    except TenantRoutingError as exc:
        logger.error("Body scan task tenant routing failed: %s", exc)
        return {'status': 'FAILED', 'error': str(exc)}
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return {'status': 'FAILED', 'error': str(exc)}
