"""
apps/tenant_core/tasks_transformation.py — Background build of the transformation preview.

A three-milestone preview means three hosted image-edit round-trips, around a minute and a
half in total — far too long to hold an HTTP request open for (the browser abandons it and
the coach sees "preview could not be produced" even though the server is still working). The
transformation endpoint marks the assessment PROCESSING and hands off to this task; the
studio UI polls the transformation endpoint until it settles.

Failure is always recorded on the assessment itself, never swallowed, so the coach knows
whether to retry or carry on.
"""

import logging

from django.utils import timezone

from celery import shared_task

from apps.tenant_core.context import tenant_database_context
from config.routers import TenantRoutingError, get_tenant_db_alias

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name='apps.tenant_core.tasks_transformation.run_assessment_transformation',
    max_retries=1,
    default_retry_delay=15,
    acks_late=True,
)
def run_assessment_transformation(
    self,
    tenant_id: str = None,
    assessment_id: str = None,
    renderer_name: str = None,
    **kwargs,
) -> dict:
    """
    Builds the milestone previews for one assessment inside its tenant database context, and
    records the outcome on the assessment so the studio can poll for it.
    """
    if not tenant_id or not assessment_id:
        logger.error("Transformation task failed closed: tenant_id and assessment_id are both required.")
        return {'status': 'FAILED', 'error': 'tenant_id and assessment_id are required'}

    from .models_assessment import MemberAssessment
    from .services_transformation import (
        TransformationError,
        TransformationNotPermitted,
        TransformationPipeline,
        get_renderer,
    )

    try:
        with tenant_database_context(tenant_id):
            alias = get_tenant_db_alias()
            try:
                assessment = MemberAssessment.objects.using(alias).get(id=assessment_id)
            except MemberAssessment.DoesNotExist:
                logger.error("Transformation task: assessment %s not found", assessment_id)
                return {'status': 'FAILED', 'error': 'assessment not found'}

            assessment.transformation_status = 'PROCESSING'
            assessment.save(using=alias, update_fields=['transformation_status', 'updated_at'])

            renderer = get_renderer(renderer_name)
            try:
                result = TransformationPipeline.build(assessment, renderer=renderer, db_alias=alias)
            except TransformationNotPermitted as exc:
                assessment.transformation_status = 'REFUSED_NO_CONSENT'
                assessment.transformation_payload = {'error': str(exc)}
                assessment.save(using=alias, update_fields=[
                    'transformation_status', 'transformation_payload', 'updated_at'])
                return {'status': 'REFUSED_NO_CONSENT', 'error': str(exc)}
            except TransformationError as exc:
                assessment.transformation_status = 'FAILED'
                assessment.transformation_payload = {'error': str(exc)}
                assessment.save(using=alias, update_fields=[
                    'transformation_status', 'transformation_payload', 'updated_at'])
                return {'status': 'FAILED', 'error': str(exc)}

            accepted = result.get('accepted_count', 0)
            if accepted == 0:
                assessment.transformation_status = 'FAILED'
            elif result.get('rejected'):
                assessment.transformation_status = 'PARTIAL'
            else:
                assessment.transformation_status = 'COMPLETED'

            assessment.transformation_payload = result
            assessment.transformation_generated_at = timezone.now()
            assessment.save(using=alias)

            return {'status': assessment.transformation_status, 'accepted': accepted}

    except TenantRoutingError as exc:
        logger.error("Transformation task routing failed for tenant %s: %s", tenant_id, exc)
        return {'status': 'FAILED', 'error': str(exc)}
    except Exception as exc:  # noqa: BLE001 — record, never swallow
        logger.exception("Transformation task crashed for assessment %s", assessment_id)
        return {'status': 'FAILED', 'error': str(exc)}
