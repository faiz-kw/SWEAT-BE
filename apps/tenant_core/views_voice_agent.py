"""
apps/tenant_core/views_voice_agent.py — Real-time CRM Tools for Sarvam AI Voice Agents.

Provides:
1. VoiceAgentBranchesView: Real-time query of active gym/studio branches and locations.
2. VoiceAgentClassesView: Real-time, branch-aware inquiry of active programs, scheduled classes, and trial slots.
3. VoiceAgentBookTrialView: Instant trial booking creation with Atomic Capacity Locking (select_for_update),
   strict concurrency control, full CRM synchronization, and automated WhatsApp confirmation.
"""

import json
import logging
import uuid
import zoneinfo
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, Tuple

from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.master.models_tenant import Tenant
from apps.tenant_core.context import tenant_database_context
from config.routers import get_tenant_db_alias

logger = logging.getLogger(__name__)
IST = zoneinfo.ZoneInfo('Asia/Kolkata')


def _is_placeholder_phone(val: Any) -> bool:
    if not val:
        return True
    s = str(val).strip()
    if s.startswith('{{') and s.endswith('}}'):
        return True
    if 'X' in s or 'x' in s:
        return True
    digits = ''.join(c for c in s if c.isdigit())
    if len(digits) < 10 or len(set(digits)) <= 1:
        return True
    return False


def _authenticate_request(request, enforce_token: bool = False) -> Tuple[Optional[Tenant], Optional[Any], Optional[JsonResponse]]:
    """
    Validates token and resolves Tenant.
    Accepts token via query param ?token=, header X-Voice-Agent-Token, or Authorization header.
    For read-only catalog/availability queries (enforce_token=False), allows query if token is omitted.
    """
    token = None
    tenant_slug = 'sweat'

    # Search query params with stripped keys (handles whitespace/tabs like token\t)
    for k, v in request.GET.items():
        k_clean = k.strip().lower()
        if k_clean == 'token' and v.strip():
            token = v.strip()
        elif k_clean == 'tenant' and v.strip():
            tenant_slug = v.strip().lower()

    if not token:
        token = (
            request.headers.get('X-Voice-Agent-Token')
            or request.headers.get('token')
            or request.headers.get('Authorization', '').replace('Bearer ', '').strip()
            or None
        )

    # If POST, check body for token/tenant if not in query/headers
    if request.method == 'POST':
        try:
            body_data = json.loads(request.body.decode('utf-8'))
            if not token and body_data.get('token'):
                token = str(body_data.get('token')).strip()
            if body_data.get('tenant'):
                tenant_slug = str(body_data.get('tenant')).strip().lower()
        except Exception:
            pass

    tenant = Tenant.objects.filter(slug=tenant_slug, status='ACTIVE').first()
    if not tenant:
        tenant = Tenant.objects.filter(status='ACTIVE').first()

    if not tenant:
        return None, None, JsonResponse({'error': 'Tenant not found or inactive'}, status=404)

    # Check token against Sarvam Integration config in tenant DB
    with tenant_database_context(tenant.id):
        from apps.tenant_core.models import Integration
        sarvam_int = Integration.objects.filter(provider__iexact='SARVAM', status='ACTIVE').first()
        expected_token = (sarvam_int.configuration or {}).get('webhook_token') if sarvam_int else None

    import os
    env_secret = os.getenv('SARVAM_WEBHOOK_SECRET', '')

    # If token is provided, validate it strictly
    if token:
        if expected_token and token != expected_token and token != env_secret:
            logger.warning("Unauthorized Voice Agent tool call: token mismatch for tenant %s", tenant.slug)
            return None, None, JsonResponse({'error': 'Unauthorized: invalid token'}, status=401)
    elif enforce_token and expected_token:
        # Enforce token for mutations/booking actions
        logger.warning("Unauthorized Voice Agent tool call: missing token for tenant %s", tenant.slug)
        return None, None, JsonResponse({'error': 'Unauthorized: missing token'}, status=401)

    return tenant, sarvam_int, None


def _resolve_branch(alias: str, org, branch_query: Optional[str]):
    """
    Resolves branch by UUID, code, name (icontains), or location city/area.
    """
    if not branch_query:
        return None

    clean_query = str(branch_query).strip()
    if not clean_query:
        return None

    from apps.tenant_core.models_org import Branch

    # 1. Exact UUID match
    try:
        val_uuid = uuid.UUID(clean_query)
        branch = Branch.objects.using(alias).filter(id=val_uuid, status='ACTIVE').select_related('location').first()
        if branch:
            return branch
    except (ValueError, TypeError):
        pass

    # 2. Exact match on code (case-insensitive)
    branch = Branch.objects.using(alias).filter(code__iexact=clean_query, status='ACTIVE').select_related('location').first()
    if branch:
        return branch

    # 3. Exact match on name (case-insensitive)
    branch = Branch.objects.using(alias).filter(name__iexact=clean_query, status='ACTIVE').select_related('location').first()
    if branch:
        return branch

    # 4. Partial match on name
    branch = Branch.objects.using(alias).filter(name__icontains=clean_query, status='ACTIVE').select_related('location').first()
    if branch:
        return branch

    # 5. Partial match on location city or area
    branch = Branch.objects.using(alias).filter(
        Q(location__city__icontains=clean_query) | Q(location__area__icontains=clean_query),
        status='ACTIVE',
    ).select_related('location').first()

    return branch


def _get_branch_spoken_name(branch) -> str:
    """
    Returns the clean, natural spoken name of the studio branch (Andheri, Goregaon, Malad).
    """
    if not branch:
        return "SWEAT"
    code = (branch.code or "").upper()
    name = (branch.name or "").upper()
    loc = (branch.location.city or "") if getattr(branch, 'location', None) else ""
    if "ANDHERI" in code or "ANDHERI" in name or "ANDHERI" in loc.upper():
        return "Andheri"
    elif "GOREGAON" in code or "GOREGAON" in name or "GOREGAON" in loc.upper():
        return "Goregaon"
    elif "MALAD" in code or "MALAD" in name or "MALAD" in loc.upper():
        return "Malad"
    return branch.name.replace("SWEAT ", "").strip()


def _resolve_tenant_org(alias: str, branch=None):
    """
    Resolves the canonical organization for leads and bookings.
    Guarantees alignment with active studio branches and prevents orphaned records.
    """
    if branch and getattr(branch, 'organization', None):
        return branch.organization
    from apps.tenant_core.models_org import Organization
    return (
        Organization.objects.using(alias).filter(code='SWEAT_UAT_ORG', status='ACTIVE').first()
        or Organization.objects.using(alias).filter(status='ACTIVE').order_by('-created_at').first()
    )


def _lookup_lead_by_phone(alias: str, phone_val: Optional[str]):
    """
    Resilient phone lookup supporting normalized formats, raw formats, and 10-digit suffix matching.
    """
    if not phone_val:
        return None
    from apps.tenant_core.models_crm import Lead
    from apps.tenant_core.services_voice_calling import VoiceCallingService

    clean_val = str(phone_val).strip()
    norm = VoiceCallingService.normalize_phone(clean_val)
    raw_digits = ''.join(c for c in clean_val if c.isdigit())
    last_10 = raw_digits[-10:] if len(raw_digits) >= 10 else raw_digits

    q = Q(phone_normalized=norm) if norm else Q(pk=None)
    if last_10:
        q |= Q(phone_normalized__endswith=last_10)

    return (
        Lead.objects.using(alias)
        .filter(q)
        .select_related('branch', 'branch__location', 'organization')
        .order_by('-created_at')
        .first()
    )


@method_decorator(csrf_exempt, name='dispatch')
class VoiceAgentBranchesView(View):
    """
    Real-time inquiry of active gym/studio branches for Sarvam Voice Agents.
    Supports GET & POST. Returns branch names, addresses, and spoken summaries.
    """

    def get(self, request, *args, **kwargs):
        return self._process(request)

    def post(self, request, *args, **kwargs):
        return self._process(request)

    def _process(self, request):
        tenant, _, err_resp = _authenticate_request(request, enforce_token=False)
        if err_resp:
            return err_resp

        with tenant_database_context(tenant.id):
            alias = get_tenant_db_alias()
            from apps.tenant_core.models_org import Branch

            branches_qs = Branch.objects.using(alias).filter(status='ACTIVE').select_related('location').order_by('name')
            branch_list = []
            for b in branches_qs:
                city = b.location.city if b.location else ''
                addr = b.address or (f"{b.address_line_1}, {city}" if b.address_line_1 else b.name)
                clean_name = b.name.replace('SWEAT ', '').strip()
                branch_list.append({
                    'branch_id': str(b.id),
                    'name': b.name,
                    'short_name': clean_name,
                    'code': b.code,
                    'city': city,
                    'address': addr,
                    'phone': b.phone,
                    'business_hours': f"{b.business_open_time} - {b.business_close_time}",
                })

            if not branch_list:
                return JsonResponse({
                    'status': 'success',
                    'branches': [],
                    'spoken_summary': "We are currently operating our main flagship studio. Which program are you interested in?",
                })

            short_names = [b['short_name'] for b in branch_list]
            if len(short_names) == 1:
                spoken = f"Our studio is located in {short_names[0]}. Would you like to check our available trial slots there?"
            elif len(short_names) == 2:
                spoken = f"We have branches located in {short_names[0]} and {short_names[1]}. Which location is more convenient for you?"
            else:
                spoken = f"We have branches located in {', '.join(short_names[:-1])}, and {short_names[-1]}. Which location would you like to visit?"

            return JsonResponse({
                'status': 'success',
                'branches': branch_list,
                'spoken_summary': spoken,
            })


@method_decorator(csrf_exempt, name='dispatch')
class VoiceAgentClassesView(View):
    """
    Real-time Branch-Aware class schedule & availability inquiry for Sarvam Voice Agents.
    Supports GET & POST.
    
    If no branch is specified, returns the list of available branches and prompts the lead
    to pick their preferred location.
    
    Calculates actual real-time trial capacity considering existing regular bookings,
    active trial bookings, and branch-level capacity overrides.
    """

    def get(self, request, *args, **kwargs):
        return self._process(request)

    def post(self, request, *args, **kwargs):
        return self._process(request)

    def _process(self, request):
        tenant, _, err_resp = _authenticate_request(request, enforce_token=False)
        if err_resp:
            return err_resp

        # Extract search parameters
        params = request.GET.dict()
        if request.method == 'POST' and request.body:
            try:
                body_json = json.loads(request.body.decode('utf-8'))
                params.update(body_json)
            except Exception:
                pass

        branch_query = (
            params.get('branch')
            or params.get('branch_name')
            or params.get('branch_id')
            or params.get('location', '')
        )
        program_filter = (
            params.get('program')
            or params.get('class_name')
            or params.get('activity', '')
        ).strip().lower()
        time_of_day = params.get('time_of_day', '').strip().lower()  # 'morning', 'afternoon', 'evening'
        date_param = params.get('date', 'tomorrow').strip().lower()
        phone_number = params.get('phone_number') or params.get('user_phone_number')
        lead_id = params.get('lead_id')

        now_utc = timezone.now()
        now_ist = now_utc.astimezone(IST)

        # Resolve target date
        if date_param in ('today', 'now'):
            target_date = now_ist.date()
        elif date_param in ('tomorrow', 'tmrw'):
            target_date = now_ist.date() + timedelta(days=1)
        elif date_param in ('day_after_tomorrow', 'day after tomorrow'):
            target_date = now_ist.date() + timedelta(days=2)
        else:
            try:
                target_date = datetime.strptime(date_param, '%Y-%m-%d').date()
            except ValueError:
                target_date = now_ist.date() + timedelta(days=1)

        with tenant_database_context(tenant.id):
            alias = get_tenant_db_alias()
            from apps.tenant_core.models_org import Branch
            from apps.tenant_core.models_crm import Lead, TrialBooking
            from apps.tenant_core.models_catalog import Program
            from apps.tenant_core.models_classes import ClassOccurrence, ClassBranchAvailability
            from apps.tenant_core.models_bookings import Booking
            from apps.tenant_core.services_voice_calling import VoiceCallingService

            # 1. Resolve Lead if identifier provided (helps infer preferred branch)
            lead = None
            if lead_id:
                try:
                    lead = Lead.objects.using(alias).filter(id=lead_id).first()
                except Exception:
                    pass
            if not lead and phone_number:
                norm_phone = VoiceCallingService.normalize_phone(phone_number)
                lead = Lead.objects.using(alias).filter(phone_normalized=norm_phone).first()

            # 2. Resolve Branch
            resolved_branch = None
            if branch_query:
                resolved_branch = _resolve_branch(alias, None, branch_query)

            # If branch not in parameters, fallback to lead's branch in CRM
            if not resolved_branch and lead and lead.branch:
                resolved_branch = lead.branch

            # 3. If STILL no branch resolved, prompt the user for branch selection
            if not resolved_branch:
                branches_qs = Branch.objects.using(alias).filter(status='ACTIVE').select_related('location').order_by('name')
                branch_list = []
                for b in branches_qs:
                    clean_name = b.name.replace('SWEAT ', '').strip()
                    branch_list.append({
                        'branch_id': str(b.id),
                        'name': b.name,
                        'short_name': clean_name,
                        'city': b.location.city if b.location else '',
                        'address': b.address or b.name,
                    })

                short_names = [b['short_name'] for b in branch_list]
                if len(short_names) > 1:
                    spoken = f"We have branches in {', '.join(short_names[:-1])} and {short_names[-1]}. Which location would you like to visit?"
                elif len(short_names) == 1:
                    spoken = f"Our studio is in {short_names[0]}. Would you like to check the schedule there?"
                else:
                    spoken = "Could you tell me which location you would like to visit?"

                return JsonResponse({
                    'status': 'needs_branch',
                    'message': 'Branch is required to check real-time class schedule.',
                    'branches': branch_list,
                    'spoken_summary': spoken,
                })

            # Fetch active programs for context
            active_programs = list(
                Program.objects.using(alias)
                .filter(status='ACTIVE')
                .values('id', 'name', 'code', 'category')
            )

            # Query class occurrences for target date and branch
            qs = ClassOccurrence.objects.using(alias).filter(
                branch=resolved_branch,
                occurrence_date=target_date,
                status__in=['OPEN', 'SCHEDULED', 'ACTIVE'],
                start_at__gt=now_utc,
                class_template__status='ACTIVE',
                class_template__allow_trial=True,
            ).select_related('class_template', 'class_template__program', 'branch')

            if program_filter:
                qs = qs.filter(
                    Q(class_template__program__name__icontains=program_filter)
                    | Q(class_template__name__icontains=program_filter)
                )

            all_occurrences = list(qs.order_by('start_at'))

            filtered = []
            for occ in all_occurrences:
                ist_dt = occ.start_at.astimezone(IST)
                hour = ist_dt.hour

                # Time of day filter
                if time_of_day == 'morning' and hour >= 12:
                    continue
                if time_of_day in ('afternoon', 'noon') and (hour < 12 or hour >= 17):
                    continue
                if time_of_day == 'evening' and hour < 17:
                    continue

                # Branch-specific availability and capacity overrides
                branch_avail = ClassBranchAvailability.objects.using(alias).filter(
                    class_template=occ.class_template,
                    branch=resolved_branch,
                ).first()
                if branch_avail and branch_avail.status == 'DISABLED':
                    continue

                effective_capacity = (
                    occ.capacity
                    or (branch_avail.capacity_override if branch_avail and branch_avail.capacity_override else None)
                    or occ.class_template.default_capacity
                )
                effective_trial_cap = (
                    occ.trial_capacity
                    or (branch_avail.trial_capacity_override if branch_avail and branch_avail.trial_capacity_override else None)
                    or occ.class_template.default_trial_capacity
                    or 2  # default trial spots
                )

                if effective_trial_cap <= 0:
                    continue

                # Real-time occupancy check
                regular_booked = Booking.objects.using(alias).filter(
                    occurrence=occ,
                    status__in=['CONFIRMED', 'RESERVED', 'COMPLETED'],
                ).count()
                trial_booked = TrialBooking.objects.using(alias).filter(
                    class_occurrence_id=occ.id,
                    status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
                ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).count()

                remaining_total = max(0, effective_capacity - (regular_booked + trial_booked))
                remaining_trial = max(0, effective_trial_cap - trial_booked)
                available_spots = min(remaining_total, remaining_trial)

                if available_spots <= 0:
                    continue

                prog_name = occ.class_template.program.name if (occ.class_template and occ.class_template.program) else occ.class_template.name
                filtered.append({
                    'occurrence_id': str(occ.id),
                    'branch_id': str(resolved_branch.id),
                    'branch_name': resolved_branch.name,
                    'program_name': prog_name,
                    'class_name': occ.class_template.name,
                    'date_label': target_date.strftime('%A, %b %d'),
                    'time_label': ist_dt.strftime('%I:%M %p IST'),
                    'available_trial_spots': available_spots,
                })

            # Natural spoken summary for the voice agent
            date_name = (
                "today" if target_date == now_ist.date()
                else ("tomorrow" if target_date == now_ist.date() + timedelta(days=1)
                else target_date.strftime('%A'))
            )
            branch_short = _get_branch_spoken_name(resolved_branch)

            if filtered:
                times_list = ", ".join([
                    f"{item['class_name']} at {item['time_label'].replace(' IST', '')}"
                    for item in filtered[:3]
                ])
                spoken_summary = (
                    f"At our {branch_short} studio for {date_name}, we have {times_list}. "
                    f"Which time works best for you?"
                )
                resp_status = 'success'
            else:
                resp_status = 'no_slots_available'
                spoken_summary = (
                    f"I checked our schedule at our {branch_short} studio for {date_name}, but unfortunately all trial slots are currently full. "
                    f"Would you like me to check the next available day, or would you like our team to call you back?"
                )

            return JsonResponse({
                'status': resp_status,
                'branch': {
                    'branch_id': str(resolved_branch.id),
                    'name': resolved_branch.name,
                    'short_name': branch_short,
                    'code': resolved_branch.code,
                },
                'target_date': target_date.isoformat(),
                'programs': [p['name'] for p in active_programs],
                'available_classes': filtered[:8],
                'spoken_summary': spoken_summary,
            })


@method_decorator(csrf_exempt, name='dispatch')
class VoiceAgentBookTrialView(View):
    """
    Creates an instant confirmed trial booking in the CRM linked to the Lead.
    Called by Sarvam Voice Agent when the prospect confirms a slot.
    
    Guarantees:
    1. Atomic Capacity Locking (select_for_update) to prevent overbooking races.
    2. Branch validation and capacity limits enforcement.
    3. Prevents duplicate active trials for the same lead.
    4. Lead pipeline transition to TRIAL_CONFIRMED.
    5. Automatic WhatsApp booking confirmation dispatch.
    6. Complete CRM audit trail, outbox event, and activity logging.
    """

    def get(self, request, *args, **kwargs):
        return self._process(request)

    def post(self, request, *args, **kwargs):
        return self._process(request)

    def _process(self, request):
        tenant, _, err_resp = _authenticate_request(request)
        if err_resp:
            return err_resp

        try:
            body_json = json.loads(request.body.decode('utf-8'))
        except Exception:
            body_json = request.POST.dict() or request.GET.dict()

        phone_number = (
            body_json.get('phone_number')
            or body_json.get('phone')
            or body_json.get('user_phone_number')
            or body_json.get('user_identifier')
            or body_json.get('user_id')
            or body_json.get('caller_phone')
            or request.GET.get('phone_number')
            or request.GET.get('phone')
            or request.GET.get('user_phone_number')
        )
        lead_id = body_json.get('lead_id') or request.GET.get('lead_id')
        occurrence_id = (
            body_json.get('occurrence_id')
            or body_json.get('occurence_id')
            or request.GET.get('occurrence_id')
        )
        branch_param = (
            body_json.get('branch')
            or body_json.get('branch_name')
            or body_json.get('branch_id')
            or request.GET.get('branch')
        )
        class_name = body_json.get('class_name', '')
        time_slot = body_json.get('time_slot', '')
        lead_name_input = (
            body_json.get('lead_name')
            or body_json.get('name')
            or body_json.get('caller_name')
            or body_json.get('customer_name')
            or body_json.get('full_name')
            or request.GET.get('lead_name')
            or request.GET.get('name')
            or ''
        )
        notes = body_json.get('notes', 'Trial booked and confirmed via Sarvam AI Voice Agent (Mahek)')

        if _is_placeholder_phone(phone_number):
            phone_number = None
        if lead_id and (str(lead_id).startswith('{{') or not str(lead_id).strip()):
            lead_id = None

        with tenant_database_context(tenant.id):
            alias = get_tenant_db_alias()
            from apps.tenant_core.models_org import Branch, Organization
            from apps.tenant_core.models_crm import Lead, TrialBooking, TrialStatusHistory, LeadActivity, LeadStatusHistory, LeadSource, CallSession
            from apps.tenant_core.models_classes import ClassOccurrence, ClassBranchAvailability
            from apps.tenant_core.models_bookings import Booking
            from apps.tenant_core.services_voice_calling import VoiceCallingService
            from apps.tenant_core.services_reliability import record_business_audit, enqueue_outbox_event

            resolved_br = _resolve_branch(alias, None, branch_param) if branch_param else None
            org = _resolve_tenant_org(alias, resolved_br)

            # 1. Resolve Lead
            lead = None
            if lead_id:
                try:
                    lead = Lead.objects.using(alias).filter(id=lead_id).first()
                except Exception:
                    pass

            if not lead and phone_number:
                lead = _lookup_lead_by_phone(alias, phone_number)

            # Fallback: Correlate with active or recent CallSession within last 25 minutes
            if not lead:
                recent_cutoff = timezone.now() - timedelta(minutes=25)
                active_cs = (
                    CallSession.objects.using(alias)
                    .filter(organization=org, created_at__gte=recent_cutoff)
                    .exclude(status='FAILED')
                    .order_by('-created_at')
                    .first()
                )
                if active_cs and active_cs.lead:
                    lead = active_cs.lead
                    if not phone_number:
                        phone_number = active_cs.user_phone_number or active_cs.lead.phone_normalized
                    logger.info(
                        "Resolved lead %s (%s, %s) from active CallSession %s for voice agent trial booking",
                        lead.id, lead.first_name, phone_number, active_cs.id
                    )

            if not lead and not phone_number:
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'MISSING_LEAD',
                    'message': 'Lead phone number or lead ID is required to book a trial.',
                    'spoken_apology': "I didn't catch your contact details. Could you please share your phone number?",
                }, status=400)

            # If lead exists, update caller details if previously Guest or missing name
            if lead:
                updated_lead_fields = []
                if lead_name_input:
                    clean_name = str(lead_name_input).strip()
                    if clean_name and (lead.first_name in ('Guest', '', 'there') or not lead.last_name):
                        if ' ' in clean_name:
                            lead.first_name, lead.last_name = clean_name.split(' ', 1)
                        else:
                            lead.first_name = clean_name
                        updated_lead_fields.extend(['first_name', 'last_name'])
                if lead.organization_id != org.id:
                    lead.organization = org
                    updated_lead_fields.append('organization')
                if updated_lead_fields:
                    updated_lead_fields.append('updated_at')
                    lead.save(using=alias, update_fields=updated_lead_fields)

            # If lead doesn't exist yet, auto-create a lead record so the booking succeeds smoothly
            if not lead:
                if not org:
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'ORG_NOT_FOUND',
                        'message': 'Active organization not found for tenant.',
                    }, status=500)

                first_name = str(lead_name_input).strip() or 'Guest'
                last_name = ''
                if ' ' in first_name:
                    parts = first_name.split(' ', 1)
                    first_name, last_name = parts[0], parts[1]

                lead_src = (
                    LeadSource.objects.using(alias).filter(organization=org, source_type__in=['SARVAM', 'META', 'OTHER'], status='ACTIVE').first()
                    or LeadSource.objects.using(alias).filter(source_type__in=['SARVAM', 'META', 'OTHER'], status='ACTIVE').first()
                )
                norm_phone = VoiceCallingService.normalize_phone(phone_number) if phone_number else ''
                lead = Lead.objects.using(alias).create(
                    organization=org,
                    branch=resolved_br,
                    first_name=first_name,
                    last_name=last_name,
                    phone_normalized=norm_phone,
                    lead_source=lead_src,
                    current_status='NEW_LEAD',
                )
                logger.info("Auto-created new lead %s (%s) for phone %s via Voice Agent", lead.id, lead.first_name, norm_phone)

            # 2. Begin Atomic Transaction & Acquire Row Lock on ClassOccurrence
            with transaction.atomic(using=alias):
                resolved_occ_id = None
                if occurrence_id:
                    try:
                        resolved_occ_id = uuid.UUID(str(occurrence_id).strip())
                    except Exception:
                        pass

                if not resolved_occ_id:
                    now_utc = timezone.now()
                    qs = ClassOccurrence.objects.using(alias).filter(
                        status__in=['OPEN', 'SCHEDULED', 'ACTIVE'],
                        start_at__gt=now_utc,
                        class_template__status='ACTIVE',
                        class_template__allow_trial=True,
                    )
                    target_branch = _resolve_branch(alias, lead.organization, branch_param) if branch_param else lead.branch
                    if target_branch:
                        qs = qs.filter(branch=target_branch)

                    # Intelligent slug/string parsing from occurrence_id
                    occ_str = str(occurrence_id or "").lower()
                    class_search = class_name.strip().lower() if class_name else ""
                    known_classes = ['stretch', 'total', 'mobility', 'glutes', 'lift', 'athletic', 'bootcamp', 'pilates', 'abs']
                    if not class_search:
                        for kw in known_classes:
                            if kw in occ_str:
                                class_search = kw
                                break
                    if class_search:
                        qs = qs.filter(class_template__name__icontains=class_search)

                    # Parse target hour from occurrence_id (e.g. sweat_total_0800_andheri -> 8) or time_slot
                    target_hour = None
                    import re
                    hm = re.search(r'(\d{2})(\d{2})', occ_str)
                    if hm:
                        target_hour = int(hm.group(1))
                    elif time_slot:
                        tm = re.search(r'(\d{1,2})', str(time_slot))
                        if tm:
                            th = int(tm.group(1))
                            if 'pm' in str(time_slot).lower() and th < 12:
                                th += 12
                            target_hour = th

                    candidate_list = list(qs.order_by('start_at'))
                    if target_hour is not None:
                        matched = [c for c in candidate_list if c.start_at.astimezone(IST).hour == target_hour]
                        if matched:
                            resolved_occ_id = matched[0].id

                    if not resolved_occ_id and candidate_list:
                        resolved_occ_id = candidate_list[0].id

                occurrence = None
                if resolved_occ_id:
                    occurrence = (
                        ClassOccurrence.objects.using(alias)
                        .select_for_update()
                        .filter(id=resolved_occ_id)
                        .select_related('class_template', 'branch')
                        .first()
                    )

                if not occurrence:
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'NO_SLOT_AVAILABLE',
                        'message': 'No upcoming class occurrences found to book.',
                        'spoken_apology': "I'm sorry, I couldn't find an open slot for that session. Let me check other available times for you.",
                    }, status=404)

                # Validate start time
                if occurrence.start_at <= timezone.now():
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'SLOT_IN_PAST',
                        'message': 'Cannot book a trial for a past session.',
                        'spoken_apology': "That session has already begun. Let's find an upcoming session instead.",
                    }, status=400)

                # Validate Class Template
                if not occurrence.class_template or occurrence.class_template.status != 'ACTIVE' or not occurrence.class_template.allow_trial:
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'TRIAL_NOT_PERMITTED',
                        'message': 'Trials are not permitted for this class template.',
                        'spoken_apology': "Free trials are not permitted for this specific format. Would you like to try our Bootcamp or Pilates instead?",
                    }, status=400)

                # Validate Branch Availability
                branch_avail = ClassBranchAvailability.objects.using(alias).filter(
                    class_template=occurrence.class_template,
                    branch=occurrence.branch,
                ).first()
                if branch_avail and branch_avail.status == 'DISABLED':
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'BRANCH_DISABLED',
                        'message': 'This class is disabled at the selected branch.',
                        'spoken_apology': "This session is currently unavailable at this branch. Let's look at another branch.",
                    }, status=400)

                # 3. Atomic Capacity & Concurrency Enforcement
                effective_capacity = (
                    occurrence.capacity
                    or (branch_avail.capacity_override if branch_avail and branch_avail.capacity_override else None)
                    or occurrence.class_template.default_capacity
                )
                effective_trial_cap = (
                    occurrence.trial_capacity
                    or (branch_avail.trial_capacity_override if branch_avail and branch_avail.trial_capacity_override else None)
                    or occurrence.class_template.default_trial_capacity
                    or 2
                )

                regular_booked = Booking.objects.using(alias).filter(
                    occurrence=occurrence,
                    status__in=['CONFIRMED', 'RESERVED', 'COMPLETED'],
                ).count()
                trial_booked = TrialBooking.objects.using(alias).filter(
                    class_occurrence_id=occurrence.id,
                    status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
                ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).count()

                branch_name = occurrence.branch.name
                branch_short = _get_branch_spoken_name(occurrence.branch)

                # Check if lead already has an active trial for this exact session first!
                existing_for_slot = TrialBooking.objects.using(alias).filter(
                    lead=lead,
                    class_occurrence_id=occurrence.id,
                    status__in=['BOOKED', 'CONFIRMED', 'ATTENDED'],
                ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).first()

                if existing_for_slot:
                    ist_dt = occurrence.start_at.astimezone(IST)
                    prog_name = occurrence.class_template.program.name if (occurrence.class_template and occurrence.class_template.program) else occurrence.class_template.name
                    arrival_instructions = (
                        "Stay Hydrated: Carry an empty bottle and get it filled at our studio. "
                        "Comfy Clothes: Wear anything comfortable, such as shorts, tracks, leggings, pants, T-shirts, or tank tops. "
                        "Carry a Fresh Pair of Grip Shoes: Please bring a fresh pair of shoes and socks to maintain hygiene at all times."
                    )
                    spoken_conf = (
                        f"Good news, {lead.first_name}! You are already booked for {prog_name} ({occurrence.class_template.name}) "
                        f"at our {branch_short} studio on {ist_dt.strftime('%A at %I:%M %p')}. "
                        f"As a reminder: Stay Hydrated — carry an empty bottle and get it filled at our studio. "
                        f"Comfy Clothes — wear anything comfortable like shorts, tracks, or leggings. "
                        f"Carry a Fresh Pair of Grip Shoes — please bring a fresh pair of shoes and socks to maintain hygiene at all times. "
                        f"We look forward to seeing you!"
                    )
                    return JsonResponse({
                        'status': 'success',
                        'already_booked': True,
                        'booking_id': str(existing_for_slot.id),
                        'program_name': prog_name,
                        'class_name': occurrence.class_template.name,
                        'branch_name': branch_name,
                        'date': ist_dt.strftime('%A, %b %d'),
                        'time': ist_dt.strftime('%I:%M %p IST'),
                        'lead_name': lead.first_name,
                        'arrival_instructions': arrival_instructions,
                        'spoken_confirmation': spoken_conf,
                    })

                # Check if total capacity or trial capacity reached
                if regular_booked + trial_booked >= effective_capacity or (effective_trial_cap > 0 and trial_booked >= effective_trial_cap):
                    # Find alternative slot
                    alt_occ = ClassOccurrence.objects.using(alias).filter(
                        branch=occurrence.branch,
                        start_at__gt=occurrence.start_at,
                        class_template=occurrence.class_template,
                        status__in=['OPEN', 'SCHEDULED', 'ACTIVE'],
                    ).order_by('start_at').first()
                    alt_str = f" on {alt_occ.start_at.astimezone(IST).strftime('%A at %I:%M %p')}" if alt_occ else ""

                    logger.warning("Voice Agent trial booking rejected: capacity reached for occurrence %s", occurrence.id)
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'SLOT_FULL',
                        'message': 'Capacity reached for this session.',
                        'spoken_apology': (
                            f"I'm sorry, that session at our {branch_short} studio just filled up to capacity! "
                            f"We do have another open spot{alt_str}. Would that time work for you?"
                        ),
                    }, status=409)

                # 4. Create Confirmed TrialBooking in CRM under lock
                trial = TrialBooking.objects.using(alias).create(
                    lead=lead,
                    branch=occurrence.branch,
                    class_occurrence_id=occurrence.id,
                    trial_type='GROUP_CLASS',
                    scheduled_start=occurrence.start_at,
                    scheduled_end=occurrence.end_at,
                    status='CONFIRMED',
                    confirmation_status='CONFIRMED',
                    confirmation_channel='PHONE',
                    confirmed_at=timezone.now(),
                    booking_source='OTHER',
                    notes=notes,
                )

                # Record Trial Status History
                TrialStatusHistory.objects.using(alias).create(
                    trial_booking=trial,
                    from_status=None,
                    to_status='CONFIRMED',
                    reason_code='CONFIRMED_VIA_PHONE',
                )

                # Update Lead status & branch
                prior_status = lead.current_status
                lead.current_status = 'TRIAL_CONFIRMED'
                if not lead.branch_id:
                    lead.branch = occurrence.branch
                lead.save(using=alias, update_fields=['current_status', 'branch', 'updated_at'])

                # Record Lead Status History
                LeadStatusHistory.objects.using(alias).create(
                    lead=lead,
                    from_status=prior_status,
                    to_status='TRIAL_CONFIRMED',
                    reason_code='VOICE_AGENT_BOOKING',
                    reason_text=f"Complimentary trial booked for {occurrence.class_template.name} via Sarvam Voice Agent",
                )

                # Record Lead Activity
                ist_dt = occurrence.start_at.astimezone(IST)
                LeadActivity.objects.using(alias).create(
                    lead=lead,
                    activity_type='TRIAL',
                    outcome='Trial Booked & Confirmed via AI Voice Call',
                    notes=(
                        f"Sarvam AI Voice Agent (Mahek) booked and confirmed free trial for "
                        f"{occurrence.class_template.name} at SWEAT {branch_short} studio on "
                        f"{ist_dt.strftime('%A, %b %d at %I:%M %p IST')}."
                    ),
                )

                # Record Business Audit & Outbox Events
                record_business_audit(
                    organization=lead.organization,
                    branch=occurrence.branch,
                    actor_type='SYSTEM',
                    module='crm',
                    action_code='CRM_TRIAL_BOOKED',
                    entity_type='TrialBooking',
                    entity_id=trial.id,
                    event_description=f"Booked and confirmed trial for {lead.first_name} {lead.last_name} at {branch_name}",
                    after_data={
                        'trial_id': str(trial.id),
                        'lead_id': str(lead.id),
                        'branch_id': str(occurrence.branch.id),
                        'scheduled_start': occurrence.start_at.isoformat(),
                        'class_occurrence_id': str(occurrence.id),
                    },
                    db_alias=alias,
                )

                enqueue_outbox_event(
                    organization=lead.organization,
                    event_type='CRM_TRIAL_BOOKED',
                    aggregate_type='TrialBooking',
                    aggregate_id=str(trial.id),
                    payload={
                        'trial_id': str(trial.id),
                        'lead_id': str(lead.id),
                        'branch_id': str(occurrence.branch.id),
                        'scheduled_start': occurrence.start_at.isoformat(),
                    },
                    db_alias=alias,
                )

            # 5. Automated WhatsApp Booking Confirmation Dispatch
            wa_sent = False
            if lead.phone_normalized:
                try:
                    from apps.tenant_core.communication.service import CommunicationService
                    addr_str = occurrence.branch.address or (
                        f"{occurrence.branch.address_line_1}, {occurrence.branch.location.city}"
                        if occurrence.branch.address_line_1 and occurrence.branch.location else occurrence.branch.name
                    )
                    prog_name = occurrence.class_template.program.name if (occurrence.class_template and occurrence.class_template.program) else occurrence.class_template.name
                    wa_body = (
                        f"Hi {lead.first_name}! 🎉 Your complimentary trial session at SWEAT is confirmed!\n\n"
                        f"🏋️ Program: {prog_name}\n"
                        f"🤸 Class: {occurrence.class_template.name}\n"
                        f"📍 Branch: {branch_name} ({addr_str})\n"
                        f"📅 Date & Time: {ist_dt.strftime('%A, %b %d at %I:%M %p')}\n\n"
                        f"📋 Important Instructions for Your Visit:\n"
                        f"💧 Stay Hydrated: Carry an empty bottle and get it filled at our studio.\n"
                        f"👕 Comfy Clothes: Wear anything comfortable, such as shorts, tracks, leggings, pants, T-shirts, or tank tops.\n"
                        f"👟 Carry a Fresh Pair of Grip Shoes: Please bring a fresh pair of shoes and socks to maintain hygiene at all times.\n\n"
                        f"Please arrive 15 minutes early. If you need to reschedule or have questions, just reply to this message!"
                    )
                    CommunicationService.send_communication(
                        organization=lead.organization,
                        channel='WHATSAPP',
                        recipient=lead.phone_normalized,
                        lead=lead,
                        body=wa_body,
                        purpose='TRANSACTIONAL',
                        idempotency_key=f"VOICE_TRIAL_CONFIRM_WA_{trial.id}",
                        trigger_type='SYSTEM',
                    )
                    wa_sent = True
                    logger.info("Dispatched WhatsApp booking confirmation to %s for trial %s", lead.phone_normalized, trial.id)
                except Exception as wa_err:
                    logger.warning("Could not dispatch automated WhatsApp booking confirmation: %s", wa_err)

            prog_name = occurrence.class_template.program.name if (occurrence.class_template and occurrence.class_template.program) else occurrence.class_template.name

            arrival_instructions = (
                "Stay Hydrated: Carry an empty bottle and get it filled at our studio. "
                "Comfy Clothes: Wear anything comfortable, such as shorts, tracks, leggings, pants, T-shirts, or tank tops. "
                "Carry a Fresh Pair of Grip Shoes: Please bring a fresh pair of shoes and socks to maintain hygiene at all times."
            )
            spoken_conf = (
                f"You're all set, {lead.first_name}! Before we wrap up, let me reconfirm all your details: "
                f"Your complimentary trial is booked for {prog_name} ({occurrence.class_template.name}) "
                f"at our {branch_short} branch on {ist_dt.strftime('%A at %I:%M %p')}. "
                f"Please keep these instructions in mind before visiting: "
                f"First, Stay Hydrated: Carry an empty bottle and get it filled at our studio. "
                f"Second, Comfy Clothes: Wear anything comfortable, such as shorts, tracks, leggings, pants, T-shirts, or tank tops. "
                f"Third, Carry a Fresh Pair of Grip Shoes: Please bring a fresh pair of shoes and socks to maintain hygiene at all times. "
                f"I've also sent the confirmation and location details to your WhatsApp. "
                f"We are excited to see you at SWEAT!"
            )

            logger.info("Voice Agent successfully booked trial: lead=%s trial=%s occurrence=%s branch=%s", lead.id, trial.id, occurrence.id, branch_name)

            return JsonResponse({
                'status': 'success',
                'booking_id': str(trial.id),
                'lead_name': lead.first_name,
                'program_name': prog_name,
                'class_name': occurrence.class_template.name,
                'branch_name': branch_name,
                'date': ist_dt.strftime('%A, %b %d'),
                'time': ist_dt.strftime('%I:%M %p IST'),
                'arrival_instructions': arrival_instructions,
                'whatsapp_sent': wa_sent,
                'spoken_confirmation': spoken_conf,
            }, status=201)


@method_decorator(csrf_exempt, name='dispatch')
class VoiceAgentLeadContextView(View):
    """
    Pre-Call Lead Context Lookup for Sarvam Voice Agents (on_start hook).
    Allows Mahek to greet the lead by first name, reference their interested program,
    and detect if they already selected a preferred studio branch on Meta.
    Also returns active trial booking info if the lead is calling to reschedule or cancel.
    """

    def get(self, request, *args, **kwargs):
        return self._process(request)

    def post(self, request, *args, **kwargs):
        return self._process(request)

    def _process(self, request):
        tenant, _, err_resp = _authenticate_request(request, enforce_token=False)
        if err_resp:
            return err_resp

        lead_id_param = request.GET.get('lead_id')
        phone_param = request.GET.get('phone') or request.GET.get('user_phone_number')
        session_id_param = request.GET.get('call_session_id') or request.GET.get('session_id')

        if request.method == 'POST':
            try:
                body = json.loads(request.body.decode('utf-8'))
                lead_id_param = body.get('lead_id', lead_id_param)
                phone_param = body.get('phone', body.get('user_phone_number', phone_param))
                session_id_param = body.get('call_session_id', body.get('session_id', session_id_param))
            except Exception:
                pass

        with tenant_database_context(tenant.id):
            alias = get_tenant_db_alias()
            from apps.tenant_core.models_crm import Lead, TrialBooking, CallSession
            from apps.tenant_core.services_voice_calling import VoiceCallingService

            lead = None

            # 1. Resolve by lead_id
            if lead_id_param and not str(lead_id_param).startswith('{{'):
                try:
                    lead_uuid = uuid.UUID(str(lead_id_param).strip())
                    lead = Lead.objects.using(alias).filter(id=lead_uuid).select_related('branch', 'branch__location').first()
                except Exception:
                    pass

            # 2. Resolve by call_session_id
            if not lead and session_id_param and not str(session_id_param).startswith('{{'):
                try:
                    sess_uuid = uuid.UUID(str(session_id_param).strip())
                    session = CallSession.objects.using(alias).filter(id=sess_uuid).select_related('lead', 'lead__branch', 'lead__branch__location').first()
                    if session and session.lead:
                        lead = session.lead
                except Exception:
                    pass

            # 3. Resolve by phone number
            if not lead and phone_param and not _is_placeholder_phone(phone_param):
                lead = _lookup_lead_by_phone(alias, phone_param)

            # 4. Resolve by active recent CallSession
            if not lead:
                recent_cutoff = timezone.now() - timedelta(minutes=25)
                active_cs = (
                    CallSession.objects.using(alias)
                    .filter(created_at__gte=recent_cutoff)
                    .exclude(status='FAILED')
                    .order_by('-created_at')
                    .first()
                )
                if active_cs and active_cs.lead:
                    lead = active_cs.lead
                    logger.info("Resolved lead %s from active CallSession %s for voice agent context", lead.id, active_cs.id)

            if not lead:
                clean_caller_name = (
                    request.GET.get('name')
                    or request.GET.get('lead_name')
                    or request.GET.get('caller_name')
                    or (body.get('name') if request.method == 'POST' else None)
                    or (body.get('lead_name') if request.method == 'POST' else None)
                    or 'there'
                )
                return JsonResponse({
                    'status': 'new_caller',
                    'lead_id': None,
                    'first_name': clean_caller_name,
                    'has_preferred_branch': False,
                    'preferred_branch': None,
                    'has_active_trial': False,
                    'active_trial': None,
                    'suggested_greeting': (
                        f"Hi {clean_caller_name}! This is Mahek from SWEAT. I noticed you were interested in trying out a session with us. "
                        "Which of our studios is closest to you — Andheri, Goregaon, or Malad?"
                    ),
                }, status=200)

            first_name = lead.first_name or 'there'
            branch_info = None
            if lead.branch:
                branch_info = {
                    'id': str(lead.branch.id),
                    'name': lead.branch.name,
                    'code': lead.branch.code,
                    'city': lead.branch.location.city if lead.branch.location else '',
                }

            # Check active upcoming trial bookings
            now_utc = timezone.now()
            active_trial = (
                TrialBooking.objects.using(alias)
                .filter(lead=lead, status__in=['BOOKED', 'CONFIRMED'])
                .select_related('branch')
                .order_by('-scheduled_start')
                .first()
            )

            active_trial_info = None
            if active_trial:
                occ = None
                if active_trial.class_occurrence_id:
                    from apps.tenant_core.models_classes import ClassOccurrence
                    occ = (
                        ClassOccurrence.objects.using(alias)
                        .filter(id=active_trial.class_occurrence_id)
                        .select_related('class_template')
                        .first()
                    )
                cls_name = occ.class_template.name if (occ and occ.class_template) else 'Trial Workout'
                br_name = _get_branch_spoken_name(active_trial.branch)
                t_dt = active_trial.scheduled_start.astimezone(IST)
                active_trial_info = {
                    'trial_id': str(active_trial.id),
                    'status': active_trial.status,
                    'confirmation_status': active_trial.confirmation_status,
                    'class_name': cls_name,
                    'branch_name': br_name,
                    'date': t_dt.strftime('%A, %b %d'),
                    'time': t_dt.strftime('%I:%M %p IST'),
                    'start_iso': active_trial.scheduled_start.isoformat(),
                }
                suggested_greeting = (
                    f"Hi {first_name}! This is Mahek from SWEAT. I see you have a trial session scheduled for "
                    f"{cls_name} at our {br_name} studio on {t_dt.strftime('%A at %I:%M %p')}. "
                    f"Are you calling to reschedule, cancel, or do you have questions about your session?"
                )
            prog_name = ''
            if lead.interested_program:
                prog_name = getattr(lead.interested_program, 'name', str(lead.interested_program))

            if active_trial_info:
                suggested_greeting = suggested_greeting
            elif lead.branch and prog_name:
                br_spoken = _get_branch_spoken_name(lead.branch)
                suggested_greeting = (
                    f"Hi {first_name}! This is Mahek from SWEAT. I noticed you were interested in our {prog_name} "
                    f"at our {br_spoken} studio. Would you like me to find the earliest open slot for your complimentary trial?"
                )
            elif lead.branch:
                br_spoken = _get_branch_spoken_name(lead.branch)
                suggested_greeting = (
                    f"Hi {first_name}! This is Mahek from SWEAT. I noticed you were interested in our workout sessions "
                    f"at our {br_spoken} studio. Would you like me to find the earliest open slot for your complimentary trial?"
                )
            elif prog_name:
                suggested_greeting = (
                    f"Hi {first_name}! This is Mahek from SWEAT. I noticed you were interested in trying out {prog_name} with us. "
                    f"Which of our studios is closest to you — Andheri, Goregaon, or Malad?"
                )
            else:
                suggested_greeting = (
                    f"Hi {first_name}! This is Mahek from SWEAT. I noticed you were interested in trying out a workout session with us. "
                    f"Which of our studios is closest to you — Andheri, Goregaon, or Malad?"
                )

            return JsonResponse({
                'status': 'success',
                'lead_id': str(lead.id),
                'first_name': lead.first_name,
                'last_name': lead.last_name,
                'phone': lead.phone_normalized,
                'program': prog_name,
                'interested_program': prog_name,
                'fitness_goal': lead.fitness_goal or '',
                'preferred_branch': branch_info,
                'has_preferred_branch': bool(branch_info),
                'has_active_trial': bool(active_trial_info),
                'active_trial': active_trial_info,
                'suggested_greeting': suggested_greeting,
            }, status=200)


@method_decorator(csrf_exempt, name='dispatch')
class VoiceAgentRescheduleTrialView(View):
    """
    Mid-Call Trial Rescheduling for Sarvam Voice Agents.
    Finds the active trial, verifies and locks the requested new slot under select_for_update(),
    reschedules atomically via CRMLeadService, and dispatches an updated WhatsApp confirmation.
    """

    def get(self, request, *args, **kwargs):
        return self._process(request)

    def post(self, request, *args, **kwargs):
        return self._process(request)

    def _process(self, request):
        tenant, _, err_resp = _authenticate_request(request)
        if err_resp:
            return err_resp

        lead_id_param = request.GET.get('lead_id')
        phone_param = (
            request.GET.get('phone')
            or request.GET.get('phone_number')
            or request.GET.get('user_phone_number')
            or request.GET.get('user_identifier')
            or request.GET.get('user_id')
        )
        trial_id_param = request.GET.get('trial_id')
        new_occ_id_param = request.GET.get('new_occurrence_id') or request.GET.get('occurrence_id')
        branch_param = request.GET.get('branch')
        class_name_param = request.GET.get('class_name') or request.GET.get('program')
        reason_param = request.GET.get('reason', 'Rescheduled via AI Voice Agent')

        if request.method == 'POST':
            try:
                body = json.loads(request.body.decode('utf-8'))
                lead_id_param = body.get('lead_id', lead_id_param)
                phone_param = (
                    body.get('phone')
                    or body.get('phone_number')
                    or body.get('user_phone_number')
                    or body.get('user_identifier')
                    or body.get('user_id')
                    or body.get('caller_phone')
                    or phone_param
                )
                trial_id_param = body.get('trial_id', trial_id_param)
                new_occ_id_param = body.get('new_occurrence_id', body.get('occurrence_id', new_occ_id_param))
                branch_param = body.get('branch', branch_param)
                class_name_param = body.get('class_name', body.get('program', class_name_param))
                reason_param = body.get('reason', reason_param)
            except Exception:
                pass

        with tenant_database_context(tenant.id):
            alias = get_tenant_db_alias()
            from apps.tenant_core.models_crm import Lead, TrialBooking, LeadActivity
            from apps.tenant_core.models_classes import ClassOccurrence
            from apps.tenant_core.services_voice_calling import VoiceCallingService
            from apps.tenant_core.services_crm import CRMLeadService

            # 1. Resolve Lead
            lead = None
            if lead_id_param:
                try:
                    lead_uuid = uuid.UUID(str(lead_id_param).strip())
                    lead = Lead.objects.using(alias).filter(id=lead_uuid).first()
                except Exception:
                    pass

            if not lead and phone_param:
                lead = _lookup_lead_by_phone(alias, phone_param)

            if not lead:
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'LEAD_NOT_FOUND',
                    'message': 'Lead could not be identified by lead_id or phone.',
                    'spoken_apology': "I couldn't locate your details in our system. Let me connect you with our studio team.",
                }, status=404)

            # 2. Resolve Active Trial Booking
            trial = None
            if trial_id_param:
                try:
                    t_uuid = uuid.UUID(str(trial_id_param).strip())
                    trial = TrialBooking.objects.using(alias).filter(id=t_uuid, lead=lead).first()
                except Exception:
                    pass

            if not trial:
                trial = (
                    TrialBooking.objects.using(alias)
                    .filter(lead=lead, status__in=['BOOKED', 'CONFIRMED'])
                    .select_related('branch')
                    .order_by('-scheduled_start')
                    .first()
                )

            if not trial:
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'NO_ACTIVE_TRIAL',
                    'message': 'No active trial booking found to reschedule.',
                    'spoken_apology': "I couldn't find an active trial booking under your number. Would you like me to book a new free trial for you?",
                }, status=404)

            # 3. Resolve Target Class Occurrence
            target_occ_id = None
            if new_occ_id_param:
                try:
                    target_occ_id = uuid.UUID(str(new_occ_id_param).strip())
                except Exception:
                    pass

            if not target_occ_id:
                # Find upcoming occurrence with slug parsing
                now_utc = timezone.now()
                target_branch = _resolve_branch(alias, lead.organization, branch_param) if branch_param else trial.branch
                qs = ClassOccurrence.objects.using(alias).filter(
                    status__in=['OPEN', 'SCHEDULED'],
                    start_at__gt=now_utc,
                    class_template__status='ACTIVE',
                    class_template__allow_trial=True,
                )
                if target_branch:
                    qs = qs.filter(branch=target_branch)

                occ_str = str(new_occ_id_param or "").lower()
                class_search = class_name_param.strip().lower() if class_name_param else ""
                known_classes = ['stretch', 'total', 'mobility', 'glutes', 'lift', 'athletic', 'bootcamp', 'pilates', 'abs']
                if not class_search:
                    for kw in known_classes:
                        if kw in occ_str:
                            class_search = kw
                            break
                if class_search:
                    qs = qs.filter(class_template__name__icontains=class_search)

                if trial.class_occurrence_id:
                    qs = qs.exclude(id=trial.class_occurrence_id)

                target_hour = None
                import re
                hm = re.search(r'(\d{2})(\d{2})', occ_str)
                if hm:
                    target_hour = int(hm.group(1))

                candidate_list = list(qs.order_by('start_at'))
                if target_hour is not None:
                    matched = [c for c in candidate_list if c.start_at.astimezone(IST).hour == target_hour]
                    if matched:
                        target_occ_id = matched[0].id

                if not target_occ_id and candidate_list:
                    target_occ_id = candidate_list[0].id

            if not target_occ_id:
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'NO_SLOT_AVAILABLE',
                    'message': 'No upcoming class occurrences found to reschedule into.',
                    'spoken_apology': "I couldn't find an open session matching that time. Let's look at another day or time.",
                }, status=404)

            # 4. Atomic Reschedule Execution
            try:
                # Allow cross-branch reschedule if needed
                target_occ = ClassOccurrence.objects.using(alias).select_related('class_template', 'branch', 'branch__location').get(id=target_occ_id)
                if trial.branch_id != target_occ.branch_id:
                    with transaction.atomic(using=alias):
                        trial.branch = target_occ.branch
                        trial.save(using=alias, update_fields=['branch', 'updated_at'])

                rescheduled_trial = CRMLeadService.reschedule_trial(
                    trial=trial,
                    new_class_occurrence_id=target_occ.id,
                    reason=reason_param,
                    db_alias=alias,
                )

                # Maintain confirmed status on voice agent confirmation
                with transaction.atomic(using=alias):
                    rescheduled_trial.confirmation_status = 'CONFIRMED'
                    rescheduled_trial.status = 'CONFIRMED'
                    rescheduled_trial.save(using=alias, update_fields=['status', 'confirmation_status', 'updated_at'])

                    LeadActivity.objects.using(alias).create(
                        lead=lead,
                        activity_type='TRIAL',
                        outcome='Trial Rescheduled via AI Voice Call',
                        notes=(
                            f"Sarvam AI Voice Agent (Mahek) rescheduled trial for "
                            f"{target_occ.class_template.name} to {target_occ.start_at.astimezone(IST).strftime('%A, %b %d at %I:%M %p IST')} "
                            f"at SWEAT {target_occ.branch.name}."
                        ),
                    )

                new_ist_dt = target_occ.start_at.astimezone(IST)
                branch_name = target_occ.branch.name
                branch_short = branch_name.replace('SWEAT ', '').strip()

                # Dispatch automated WhatsApp rescheduling notification
                wa_sent = False
                if lead.phone_normalized:
                    try:
                        from apps.tenant_core.communication.service import CommunicationService
                        addr_str = target_occ.branch.address or (
                            f"{target_occ.branch.address_line_1}, {target_occ.branch.location.city}"
                            if target_occ.branch.address_line_1 and target_occ.branch.location else target_occ.branch.name
                        )
                        prog_name = target_occ.class_template.program.name if (target_occ.class_template and target_occ.class_template.program) else target_occ.class_template.name
                        wa_body = (
                            f"Hi {lead.first_name}! 🔄 Your SWEAT trial session has been successfully rescheduled.\n\n"
                            f"🏋️ Program: {prog_name}\n"
                            f"🤸 Class: {target_occ.class_template.name}\n"
                            f"📍 Branch: {branch_name} ({addr_str})\n"
                            f"📅 New Date & Time: {new_ist_dt.strftime('%A, %b %d at %I:%M %p')}\n\n"
                            f"📋 Important Instructions for Your Visit:\n"
                            f"💧 Stay Hydrated: Carry an empty bottle and get it filled at our studio.\n"
                            f"👕 Comfy Clothes: Wear anything comfortable, such as shorts, tracks, leggings, pants, T-shirts, or tank tops.\n"
                            f"👟 Carry a Fresh Pair of Grip Shoes: Please bring a fresh pair of shoes and socks to maintain hygiene at all times.\n\n"
                            f"Please arrive 15 minutes early. Reply here if you need any further assistance!"
                        )
                        CommunicationService.send_communication(
                            organization=lead.organization,
                            channel='WHATSAPP',
                            recipient=lead.phone_normalized,
                            lead=lead,
                            body=wa_body,
                            purpose='TRANSACTIONAL',
                            idempotency_key=f"VOICE_TRIAL_RESCHED_WA_{rescheduled_trial.id}_{int(new_ist_dt.timestamp())}",
                            trigger_type='SYSTEM',
                        )
                        wa_sent = True
                    except Exception as wa_err:
                        logger.warning("Could not dispatch automated WhatsApp reschedule notification: %s", wa_err)

                prog_name = target_occ.class_template.program.name if (target_occ.class_template and target_occ.class_template.program) else target_occ.class_template.name
                spoken_conf = (
                    f"You're all set, {lead.first_name}! I have rescheduled your trial session for "
                    f"{prog_name} ({target_occ.class_template.name}) at our {branch_short} branch to "
                    f"{new_ist_dt.strftime('%A, %b %d at %I:%M %p')}. "
                    f"As a reminder: Stay Hydrated — carry an empty bottle and get it filled at our studio. "
                    f"Comfy Clothes — wear anything comfortable like shorts, tracks, or leggings. "
                    f"Carry a Fresh Pair of Grip Shoes — please bring a fresh pair of shoes and socks to maintain hygiene. "
                    f"I've also sent the updated details to your WhatsApp. See you on the mat!"
                )

                return JsonResponse({
                    'status': 'success',
                    'booking_id': str(rescheduled_trial.id),
                    'class_name': target_occ.class_template.name,
                    'branch_name': branch_name,
                    'date': new_ist_dt.strftime('%A, %b %d'),
                    'time': new_ist_dt.strftime('%I:%M %p IST'),
                    'whatsapp_sent': wa_sent,
                    'spoken_confirmation': spoken_conf,
                }, status=200)

            except Exception as e:
                err_msg = str(e)
                logger.exception("Voice Agent trial reschedule error: %s", e)
                if "Maximum reschedules" in err_msg or "limit" in err_msg.lower():
                    spoken_apology = (
                        f"I see you have already rescheduled this trial twice, which is our studio's "
                        f"maximum permitted limit. Let me have our studio team call you to assist directly."
                    )
                    error_code = 'MAX_RESCHEDULES_EXCEEDED'
                else:
                    spoken_apology = "I ran into an issue rescheduling that session. Let me check another time slot for you."
                    error_code = 'RESCHEDULE_FAILED'

                return JsonResponse({
                    'status': 'error',
                    'error_code': error_code,
                    'message': err_msg,
                    'spoken_apology': spoken_apology,
                }, status=400)


@method_decorator(csrf_exempt, name='dispatch')
class VoiceAgentCancelTrialView(View):
    """
    Mid-Call Trial Cancellation for Sarvam Voice Agents.
    Finds active trial, releases capacity via CRMLeadService.cancel_trial,
    records timeline history, and dispatches a WhatsApp cancellation confirmation.
    """

    def get(self, request, *args, **kwargs):
        return self._process(request)

    def post(self, request, *args, **kwargs):
        return self._process(request)

    def _process(self, request):
        tenant, _, err_resp = _authenticate_request(request)
        if err_resp:
            return err_resp

        lead_id_param = request.GET.get('lead_id')
        phone_param = (
            request.GET.get('phone')
            or request.GET.get('phone_number')
            or request.GET.get('user_phone_number')
            or request.GET.get('user_identifier')
            or request.GET.get('user_id')
        )
        trial_id_param = request.GET.get('trial_id')
        reason_param = request.GET.get('reason', 'Cancelled via AI Voice Agent')

        if request.method == 'POST':
            try:
                body = json.loads(request.body.decode('utf-8'))
                lead_id_param = body.get('lead_id', lead_id_param)
                phone_param = (
                    body.get('phone')
                    or body.get('phone_number')
                    or body.get('user_phone_number')
                    or body.get('user_identifier')
                    or body.get('user_id')
                    or body.get('caller_phone')
                    or phone_param
                )
                trial_id_param = body.get('trial_id', trial_id_param)
                reason_param = body.get('reason', reason_param)
            except Exception:
                pass

        with tenant_database_context(tenant.id):
            alias = get_tenant_db_alias()
            from apps.tenant_core.models_crm import Lead, TrialBooking, LeadActivity
            from apps.tenant_core.services_voice_calling import VoiceCallingService
            from apps.tenant_core.services_crm import CRMLeadService

            # 1. Resolve Lead
            lead = None
            if lead_id_param:
                try:
                    lead_uuid = uuid.UUID(str(lead_id_param).strip())
                    lead = Lead.objects.using(alias).filter(id=lead_uuid).first()
                except Exception:
                    pass

            if not lead and phone_param:
                lead = _lookup_lead_by_phone(alias, phone_param)

            if not lead:
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'LEAD_NOT_FOUND',
                    'message': 'Lead could not be identified by lead_id or phone.',
                    'spoken_apology': "I couldn't locate your booking in our system. Let me connect you with our studio team.",
                }, status=404)

            # 2. Resolve Active Trial Booking
            trial = None
            if trial_id_param:
                try:
                    t_uuid = uuid.UUID(str(trial_id_param).strip())
                    trial = TrialBooking.objects.using(alias).filter(id=t_uuid, lead=lead).first()
                except Exception:
                    pass
                if not trial:
                    return JsonResponse({
                        'status': 'error',
                        'error_code': 'TRIAL_NOT_FOUND',
                        'message': f'Trial booking {trial_id_param} not found for this lead.',
                        'spoken_apology': "I couldn't locate that specific booking under your profile.",
                    }, status=404)
            else:
                trial = (
                    TrialBooking.objects.using(alias)
                    .filter(lead=lead, status__in=['BOOKED', 'CONFIRMED'])
                    .select_related('branch')
                    .order_by('-scheduled_start')
                    .first()
                )

            if not trial:
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'NO_ACTIVE_TRIAL',
                    'message': 'No active trial booking found to cancel.',
                    'spoken_apology': "I couldn't find an active trial booking under your number to cancel.",
                }, status=404)

            # 3. Execute Cancellation
            try:
                cls_name = 'Trial Session'
                if trial.class_occurrence_id:
                    from apps.tenant_core.models_classes import ClassOccurrence
                    occ = (
                        ClassOccurrence.objects.using(alias)
                        .filter(id=trial.class_occurrence_id)
                        .select_related('class_template')
                        .first()
                    )
                    if occ and occ.class_template:
                        cls_name = occ.class_template.name
                br_name = _get_branch_spoken_name(trial.branch)

                cancelled_trial = CRMLeadService.cancel_trial(
                    trial=trial,
                    reason=reason_param,
                    db_alias=alias,
                )

                LeadActivity.objects.using(alias).create(
                    lead=lead,
                    activity_type='TRIAL',
                    outcome='Trial Cancelled via AI Voice Call',
                    notes=f"Sarvam AI Voice Agent (Mahek) cancelled trial for {cls_name}. Reason: {reason_param}",
                )

                # Send WhatsApp cancellation confirmation
                wa_sent = False
                if lead.phone_normalized:
                    try:
                        from apps.tenant_core.communication.service import CommunicationService
                        wa_body = (
                            f"Hi {lead.first_name}, your SWEAT trial booking for {cls_name} has been cancelled as requested. "
                            f"Whenever you're ready to restart your fitness journey, reply to this chat or visit us at {br_name}!\n\n"
                            f"Stay fit, Team SWEAT."
                        )
                        CommunicationService.send_communication(
                            organization=lead.organization,
                            channel='WHATSAPP',
                            recipient=lead.phone_normalized,
                            lead=lead,
                            body=wa_body,
                            purpose='TRANSACTIONAL',
                            idempotency_key=f"VOICE_TRIAL_CANCEL_WA_{cancelled_trial.id}",
                            trigger_type='SYSTEM',
                        )
                        wa_sent = True
                    except Exception as wa_err:
                        logger.warning("Could not dispatch automated WhatsApp cancel notification: %s", wa_err)

                spoken_conf = (
                    f"I have cancelled your trial session for {cls_name} as requested, {lead.first_name}. "
                    f"I have also sent a confirmation to your WhatsApp. Whenever you'd like to book in the future, "
                    f"feel free to call us back anytime. Have a wonderful day!"
                )

                return JsonResponse({
                    'status': 'success',
                    'cancelled_booking_id': str(cancelled_trial.id),
                    'whatsapp_sent': wa_sent,
                    'spoken_confirmation': spoken_conf,
                }, status=200)

            except Exception as e:
                logger.exception("Voice Agent trial cancellation error: %s", e)
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'CANCEL_FAILED',
                    'message': str(e),
                    'spoken_apology': "I ran into an issue cancelling that booking. Let me flag this for our front desk.",
                }, status=400)


@method_decorator(csrf_exempt, name='dispatch')
class VoiceAgentCallbackView(View):
    """
    Real-Time Callback & Follow-Up Scheduling Tool for Sarvam Voice Agents.
    Called when a prospect requests a callback or asks to be contacted later.

    Guarantees:
    1. Resolves or creates the lead in CRM.
    2. Creates a SalesFollowupTask in CRM with due date/time and caller's request notes.
    3. Updates Lead current_status to FOLLOW_UP_PENDING and records LeadStatusHistory.
    4. Logs LeadActivity with activity_type='FOLLOW_UP' to populate the Lead 360 follow-up tab & timeline.
    5. Returns spoken confirmation for the voice agent.
    """

    def get(self, request, *args, **kwargs):
        return self._process(request)

    def post(self, request, *args, **kwargs):
        return self._process(request)

    def _process(self, request):
        tenant, _, err_resp = _authenticate_request(request, enforce_token=False)
        if err_resp:
            return err_resp

        params = request.GET.dict()
        if request.method == 'POST' and request.body:
            try:
                body_json = json.loads(request.body.decode('utf-8'))
                params.update(body_json)
            except Exception:
                pass

        phone_param = (
            params.get('phone')
            or params.get('phone_number')
            or params.get('user_phone_number')
            or params.get('user_identifier')
            or params.get('user_id')
            or params.get('caller_phone')
        )
        lead_id_param = params.get('lead_id')
        callback_time_str = (
            params.get('callback_time')
            or params.get('callback_date_time')
            or params.get('due_at')
            or params.get('preferred_time')
            or params.get('next_step_date_time')
            or ''
        )
        caller_notes = (
            params.get('notes')
            or params.get('reason')
            or params.get('request')
            or params.get('call_summary')
            or params.get('summary')
            or 'Caller requested a callback from our team.'
        )
        caller_name_param = (
            params.get('lead_name')
            or params.get('name')
            or params.get('caller_name')
            or ''
        )
        priority = (params.get('priority') or 'NORMAL').upper()
        if priority not in ('LOW', 'NORMAL', 'HIGH', 'URGENT'):
            priority = 'NORMAL'

        with tenant_database_context(tenant.id):
            alias = get_tenant_db_alias()
            from apps.tenant_core.models_org import Branch, Organization
            from apps.tenant_core.models_crm import Lead, SalesFollowupTask, LeadStatusHistory, LeadActivity, LeadSource
            from apps.tenant_core.models_users import TenantUser
            from apps.tenant_core.services_voice_calling import VoiceCallingService

            lead = None
            if lead_id_param:
                try:
                    lead = Lead.objects.using(alias).filter(id=uuid.UUID(str(lead_id_param))).first()
                except Exception:
                    pass

            if not lead and phone_param:
                lead = _lookup_lead_by_phone(alias, phone_param)

            norm_phone = VoiceCallingService.normalize_phone(str(phone_param)) if phone_param else ''

            if not lead and not norm_phone:
                return JsonResponse({
                    'status': 'error',
                    'error_code': 'MISSING_LEAD',
                    'message': 'Lead phone number or ID is required to schedule a callback.',
                    'spoken_apology': "I'd be happy to arrange a callback. Could you please share your phone number?",
                }, status=400)

            # Auto-create lead if not exists
            if not lead and norm_phone:
                branch_param = params.get('branch') or params.get('branch_name') or ''
                resolved_br = _resolve_branch(alias, None, branch_param) if branch_param else None
                org = _resolve_tenant_org(alias, resolved_br)
                first_name = str(caller_name_param).strip() or 'Guest'
                last_name = ''
                if ' ' in first_name:
                    first_name, last_name = first_name.split(' ', 1)
                lead_src = (
                    LeadSource.objects.using(alias).filter(organization=org, source_type__in=['SARVAM', 'META', 'OTHER'], status='ACTIVE').first()
                    or LeadSource.objects.using(alias).filter(source_type__in=['SARVAM', 'META', 'OTHER'], status='ACTIVE').first()
                )
                lead = Lead.objects.using(alias).create(
                    organization=org,
                    branch=resolved_br,
                    first_name=first_name,
                    last_name=last_name,
                    phone_normalized=norm_phone,
                    lead_source=lead_src,
                    current_status='FOLLOW_UP_PENDING',
                )

            # Update caller details if previously Guest
            if lead and caller_name_param:
                clean_name = str(caller_name_param).strip()
                if clean_name and (lead.first_name in ('Guest', '', 'there') or not lead.last_name):
                    if ' ' in clean_name:
                        lead.first_name, lead.last_name = clean_name.split(' ', 1)
                    else:
                        lead.first_name = clean_name
                    lead.save(using=alias, update_fields=['first_name', 'last_name', 'updated_at'])

            # Parse callback time or default to next business day at 10:00 AM IST
            now_ist = timezone.now().astimezone(IST)
            callback_dt = None
            if callback_time_str:
                clean_dt_str = str(callback_time_str).strip()
                try:
                    callback_dt = datetime.fromisoformat(clean_dt_str)
                    if not callback_dt.tzinfo:
                        callback_dt = callback_dt.replace(tzinfo=IST)
                except Exception:
                    pass
                if not callback_dt:
                    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d', '%d/%m/%Y %H:%M', '%I:%M %p', '%H:%M'):
                        try:
                            parsed_t = datetime.strptime(clean_dt_str, fmt)
                            if fmt in ('%I:%M %p', '%H:%M'):
                                callback_dt = datetime.combine(now_ist.date() + timedelta(days=1), parsed_t.time(), tzinfo=IST)
                            else:
                                callback_dt = parsed_t.replace(tzinfo=IST)
                            break
                        except Exception:
                            pass

            if not callback_dt:
                tomorrow = now_ist.date() + timedelta(days=1)
                callback_dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 10, 0, tzinfo=IST)

            # Assignee
            assigned_user = lead.assigned_sales_user
            if not assigned_user:
                assigned_user = TenantUser.objects.using(alias).filter(organization=lead.organization, is_active=True).first()
            if not assigned_user:
                assigned_user = TenantUser.objects.using(alias).filter(is_active=True).first()

            # Create SalesFollowupTask
            task = SalesFollowupTask.objects.using(alias).create(
                lead=lead,
                assigned_to_user=assigned_user,
                task_type='CALL',
                priority=priority,
                due_at=callback_dt,
                status='PENDING',
                outcome=caller_notes,
                created_by_user=assigned_user,
            )

            # Transition lead status if not already booked or converted
            if lead.current_status not in ('TRIAL_BOOKED', 'TRIAL_CONFIRMED', 'CONVERTED'):
                prior_status = lead.current_status
                lead.current_status = 'FOLLOW_UP_PENDING'
                lead.save(using=alias, update_fields=['current_status', 'updated_at'])

                LeadStatusHistory.objects.using(alias).create(
                    lead=lead,
                    from_status=prior_status,
                    to_status='FOLLOW_UP_PENDING',
                    reason_code='VOICE_CALLBACK_REQUESTED',
                    reason_text=caller_notes,
                    changed_by_user=assigned_user,
                )

            # Record LeadActivity of type FOLLOW_UP
            dt_fmt = callback_dt.astimezone(IST).strftime('%A, %b %d at %I:%M %p IST')
            LeadActivity.objects.using(alias).create(
                lead=lead,
                activity_type='FOLLOW_UP',
                outcome=f"Callback Requested for {dt_fmt}",
                notes=f"Sarvam AI Voice Agent (Mahek) scheduled callback. Request details: {caller_notes}",
                performed_by_user=assigned_user,
            )

            spoken_conf = (
                f"Got it, {lead.first_name}. I have scheduled a callback for you with our team for {callback_dt.astimezone(IST).strftime('%A at %I:%M %p')}. "
                f"They will reach out to you directly to help. Have a wonderful day!"
            )

            return JsonResponse({
                'status': 'success',
                'callback_id': str(task.id),
                'lead_id': str(lead.id),
                'due_at': callback_dt.isoformat(),
                'due_at_formatted': dt_fmt,
                'spoken_confirmation': spoken_conf,
            }, status=200)


@method_decorator(csrf_exempt, name='dispatch')
class VoiceAgentCallOutcomeView(View):
    """
    Call-End Outcome & Summary Logger for Sarvam Voice Agents (on_end hook).
    Logs the final conversation disposition, call summary, fitness goals/pain points,
    and chosen next step directly to the Lead CRM timeline and activity log.
    Synchronizes follow-up tasks for callback requests and cancellations for active trials.
    """

    def get(self, request, *args, **kwargs):
        return self._process(request)

    def post(self, request, *args, **kwargs):
        return self._process(request)

    def _process(self, request):
        tenant, _, err_resp = _authenticate_request(request, enforce_token=False)
        if err_resp:
            return err_resp

        data = request.GET.dict()
        if request.method == 'POST' and request.body:
            try:
                body_json = json.loads(request.body.decode('utf-8'))
                data.update(body_json)
            except Exception:
                pass

        def _clean_val(val: Any) -> str:
            if not val:
                return ''
            s = str(val).strip()
            if s.startswith('{{') and s.endswith('}}'):
                return ''
            return s

        # Extract identifiers & outcome details (supports both SWEAT & default Sarvam field names)
        phone = _clean_val(
            data.get('phone')
            or data.get('user_phone_number')
            or data.get('user_identifier')
            or data.get('user_id')
            or data.get('caller_phone')
            or data.get('userName', '')
        )
        lead_id = _clean_val(data.get('lead_id'))
        disposition = _clean_val(data.get('disposition') or data.get('primary_disposition'))
        call_summary = _clean_val(data.get('callSummary') or data.get('call_summary') or data.get('summary'))
        fitness_goals = _clean_val(data.get('painPointsMentioned') or data.get('fitness_goals') or data.get('pain_points'))
        chosen_next_step = _clean_val(data.get('chosenNextStep') or data.get('next_step'))
        next_step_dt = _clean_val(data.get('nextStepDateTime') or data.get('next_step_date_time'))
        branch_name = _clean_val(data.get('branch') or data.get('preferred_branch'))

        with tenant_database_context(tenant.id):
            alias = get_tenant_db_alias()
            from apps.tenant_core.models_org import Organization
            from apps.tenant_core.models_crm import Lead, LeadActivity, CallSession, SalesFollowupTask, LeadStatusHistory, TrialBooking
            from apps.tenant_core.models_users import TenantUser
            from apps.tenant_core.services_voice_calling import VoiceCallingService
            from apps.tenant_core.services_crm import CRMLeadService

            lead = None
            if lead_id:
                try:
                    lead = Lead.objects.using(alias).filter(id=uuid.UUID(str(lead_id))).first()
                except Exception:
                    pass

            norm_phone = VoiceCallingService.normalize_phone(str(phone)) if phone and not _is_placeholder_phone(phone) else ''
            if not lead and (norm_phone or phone) and not _is_placeholder_phone(phone):
                lead = _lookup_lead_by_phone(alias, phone)

            # Fallback to active CallSession if lead still not resolved
            if not lead:
                recent_cutoff = timezone.now() - timedelta(minutes=25)
                active_cs = (
                    CallSession.objects.using(alias)
                    .filter(created_at__gte=recent_cutoff)
                    .exclude(status='FAILED')
                    .order_by('-created_at')
                    .first()
                )
                if active_cs and active_cs.lead:
                    lead = active_cs.lead
                    logger.info("Resolved lead %s from active CallSession %s for call outcome", lead.id, active_cs.id)

            caller_name_param = _clean_val(
                data.get('lead_name')
                or data.get('name')
                or data.get('caller_name')
                or data.get('userName')
                or ''
            )

            # If lead doesn't exist yet, auto-create a lead record so the call outcome is preserved in CRM
            if not lead and norm_phone:
                resolved_br = _resolve_branch(alias, None, branch_name) if branch_name else None
                org = _resolve_tenant_org(alias, resolved_br)
                first_name = str(caller_name_param).strip() or 'Guest'
                last_name = ''
                if ' ' in first_name:
                    first_name, last_name = first_name.split(' ', 1)
                from apps.tenant_core.models_crm import LeadSource
                lead_src = (
                    LeadSource.objects.using(alias).filter(organization=org, source_type__in=['SARVAM', 'META', 'OTHER'], status='ACTIVE').first()
                    or LeadSource.objects.using(alias).filter(source_type__in=['SARVAM', 'META', 'OTHER'], status='ACTIVE').first()
                )
                lead = Lead.objects.using(alias).create(
                    organization=org,
                    branch=resolved_br,
                    first_name=first_name,
                    last_name=last_name,
                    phone_normalized=norm_phone,
                    lead_source=lead_src,
                    current_status='NEW_LEAD',
                )
                logger.info("Auto-created new lead %s (%s) from call outcome for phone %s", lead.id, first_name, norm_phone)

            # If lead exists, update caller details if previously Guest or missing name
            if lead and caller_name_param:
                clean_name = str(caller_name_param).strip()
                if clean_name and (lead.first_name in ('Guest', '', 'there') or not lead.last_name):
                    if ' ' in clean_name:
                        lead.first_name, lead.last_name = clean_name.split(' ', 1)
                    else:
                        lead.first_name = clean_name
                    lead.save(using=alias, update_fields=['first_name', 'last_name', 'updated_at'])

            # 1. Match active or recent CallSession to know true duration & telephony state
            recent_sess = None
            cs_id = data.get('call_session_id')
            if cs_id:
                try:
                    recent_sess = CallSession.objects.using(alias).filter(id=uuid.UUID(str(cs_id))).first()
                except Exception:
                    pass
            if not recent_sess and data.get('attempt_id'):
                recent_sess = CallSession.objects.using(alias).filter(provider_attempt_id=str(data.get('attempt_id'))).first()
            if not recent_sess and lead:
                recent_sess = CallSession.objects.using(alias).filter(lead=lead).order_by('-created_at').first()

            call_dur = 0.0
            if recent_sess and recent_sess.duration_seconds:
                call_dur = float(recent_sess.duration_seconds)
            elif active_cs and active_cs.duration_seconds:
                call_dur = float(active_cs.duration_seconds)

            recent_trial = TrialBooking.objects.using(alias).filter(lead=lead).order_by('-created_at').first() if lead else None
            is_recent_trial = bool(recent_trial and (timezone.now() - recent_trial.created_at).total_seconds() < 900)

            first_name = lead.first_name if lead else 'the lead'

            if not disposition:
                if is_recent_trial:
                    disposition = 'TRIAL_BOOKED'
                elif call_dur > 0 and call_dur < 25:
                    disposition = 'HUNG_UP'
                elif call_dur >= 25:
                    disposition = 'INQUIRY_ONLY'
                else:
                    disposition = 'CALL_COMPLETED'

            if not call_summary:
                if is_recent_trial:
                    cls_title = 'workout session'
                    if recent_trial and recent_trial.class_occurrence_id:
                        try:
                            from apps.tenant_core.models_scheduling import ClassOccurrence
                            occ = ClassOccurrence.objects.using(alias).filter(id=recent_trial.class_occurrence_id).select_related('class_template').first()
                            if occ and occ.class_template:
                                cls_title = occ.class_template.name
                        except Exception:
                            pass
                    br_title = recent_trial.branch.name if recent_trial.branch else 'studio'
                    call_summary = (
                        f"Spoke with {first_name}. Scheduled a complimentary trial for {cls_title} at our {br_title} studio. Confirmed attendance and shared arrival guidelines."
                    )
                elif disposition in ('HUNG_UP', 'DISCONNECTED_EARLY') or (call_dur > 0 and call_dur < 25):
                    call_summary = (
                        f"Call connected ({int(call_dur)}s). Agent delivered greeting, but {first_name} hung up before any conversation or trial booking."
                    )
                elif disposition in ('NO_ANSWER', 'MISSED'):
                    call_summary = f"Outbound call placed to {first_name}, but was not answered."
                else:
                    call_summary = f"Call connected with {first_name} ({int(call_dur)}s). Inquired about programs; no trial booked."

            # Format activity notes
            notes_lines = [f"Call Summary: {call_summary}"]
            if disposition:
                notes_lines.append(f"Disposition: {disposition}")
            if fitness_goals:
                notes_lines.append(f"Fitness Goals / Topics Discussed: {fitness_goals}")
            if chosen_next_step:
                notes_lines.append(f"Next Step: {chosen_next_step}")
            if next_step_dt:
                notes_lines.append(f"Next Step Date/Time: {next_step_dt}")
            if branch_name:
                notes_lines.append(f"Preferred Branch: {branch_name}")

            full_notes = "\n".join(notes_lines)

            if lead:
                disp_upper = str(disposition).upper()
                call_type_param = str(data.get('call_type') or data.get('type') or '').upper()
                is_reminder = (
                    'REMINDER' in disp_upper
                    or 'REMINDER' in call_type_param
                    or 'reminder' in str(call_summary).lower()
                )
                act_outcome = f"AI Reminder Call: {disposition}" if is_reminder else f"AI Voice Call: {disposition}"

                # 1. Record LeadActivity
                LeadActivity.objects.using(alias).create(
                    lead=lead,
                    activity_type='CALL',
                    outcome=act_outcome,
                    notes=full_notes,
                )

                # 2. Record LeadNote for the lead profile & timeline
                from apps.tenant_core.models_crm import LeadNote
                note_actor = (
                    lead.assigned_sales_user
                    or TenantUser.objects.using(alias).filter(organization=lead.organization, status='ACTIVE').first()
                    or TenantUser.objects.using(alias).filter(status='ACTIVE').first()
                )
                note_prefix = "[Sarvam Voice Agent Reminder Call" if is_reminder else "[Sarvam Voice Agent"
                if note_actor:
                    LeadNote.objects.using(alias).create(
                        lead=lead,
                        note_text=f"{note_prefix} {timezone.now().strftime('%Y-%m-%d')}]: {call_summary}",
                        note_type='VOICE_CALL',
                        created_by_user=note_actor,
                    )

                # 3. Associate with or create CallSession for timeline audit
                recent_sess = None
                cs_id = data.get('call_session_id')
                if cs_id:
                    try:
                        recent_sess = CallSession.objects.using(alias).filter(id=uuid.UUID(str(cs_id))).first()
                    except Exception:
                        pass
                if not recent_sess and data.get('attempt_id'):
                    recent_sess = CallSession.objects.using(alias).filter(provider_attempt_id=str(data.get('attempt_id'))).first()
                if not recent_sess:
                    recent_sess = CallSession.objects.using(alias).filter(lead=lead).order_by('-created_at').first()

                if recent_sess:
                    fields_to_save = ['transcript', 'metadata', 'updated_at']
                    if is_reminder:
                        if not isinstance(recent_sess.metadata, dict):
                            recent_sess.metadata = {}
                        recent_sess.metadata['call_type'] = 'REMINDER'
                    if not recent_sess.transcript:
                        recent_sess.transcript = full_notes
                    recent_sess.status = 'COMPLETED'
                    fields_to_save.append('status')
                    if not recent_sess.ended_at:
                        recent_sess.ended_at = timezone.now()
                        fields_to_save.append('ended_at')
                    if (not recent_sess.duration_seconds or recent_sess.duration_seconds <= 0) and recent_sess.started_at:
                        diff = (recent_sess.ended_at - recent_sess.started_at).total_seconds()
                        recent_sess.duration_seconds = max(1.0, round(diff, 1))
                        fields_to_save.append('duration_seconds')
                    rec_url_input = data.get('recording_url') or data.get('recording') or data.get('audio_url')
                    if rec_url_input:
                        if not isinstance(recent_sess.metadata, dict):
                            recent_sess.metadata = {}
                        recent_sess.metadata['recording_url'] = str(rec_url_input).strip()
                    recent_sess.save(using=alias, update_fields=fields_to_save)
                else:
                    CallSession.objects.using(alias).create(
                        organization=lead.organization,
                        lead=lead,
                        provider='SARVAM',
                        status='COMPLETED',
                        direction='OUTBOUND',
                        user_phone_number=lead.phone_normalized or str(phone),
                        transcript=full_notes,
                        metadata={'call_type': 'REMINDER' if is_reminder else 'VOICE_CALL', 'disposition': disposition},
                        started_at=timezone.now(),
                        ended_at=timezone.now(),
                    )

                # 3b. Attendance confirmation sync if caller confirmed attendance during reminder call
                is_attendance_confirmed = any(k in disp_upper for k in ('REMINDER_CONFIRMED', 'ATTENDING', 'CONFIRMED'))
                if is_attendance_confirmed:
                    active_trial = TrialBooking.objects.using(alias).filter(
                        lead=lead,
                        status__in=['BOOKED', 'CONFIRMED'],
                    ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).first()
                    if active_trial and active_trial.confirmation_status != 'CONFIRMED':
                        active_trial.confirmation_status = 'CONFIRMED'
                        active_trial.save(using=alias, update_fields=['confirmation_status', 'updated_at'])
                        TrialStatusHistory.objects.using(alias).create(
                            trial_booking=active_trial,
                            from_status=active_trial.status,
                            to_status=active_trial.status,
                            reason_code='REMINDER_CALL_CONFIRMED',
                        )

                # 4. Follow-up / Callback synchronization
                disp_upper = str(disposition).upper()
                next_lower = str(chosen_next_step).lower()
                is_callback = (
                    any(k in disp_upper for k in ('CALLBACK', 'FOLLOW_UP', 'FOLLOWUP', 'CALL_LATER', 'INTERESTED'))
                    or any(k in next_lower for k in ('callback', 'follow up', 'call back', 'call later'))
                )
                if is_callback:
                    recent_cutoff = timezone.now() - timedelta(hours=2)
                    has_recent_task = SalesFollowupTask.objects.using(alias).filter(lead=lead, created_at__gte=recent_cutoff).exists()
                    if not has_recent_task:
                        now_ist = timezone.now().astimezone(IST)
                        cb_dt = None
                        if next_step_dt:
                            try:
                                cb_dt = datetime.fromisoformat(str(next_step_dt).strip())
                                if not cb_dt.tzinfo:
                                    cb_dt = cb_dt.replace(tzinfo=IST)
                            except Exception:
                                pass
                        if not cb_dt:
                            tomorrow = now_ist.date() + timedelta(days=1)
                            cb_dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 10, 0, tzinfo=IST)

                        assigned_u = lead.assigned_sales_user or TenantUser.objects.using(alias).filter(organization=lead.organization, is_active=True).first() or TenantUser.objects.using(alias).filter(is_active=True).first()

                        SalesFollowupTask.objects.using(alias).create(
                            lead=lead,
                            assigned_to_user=assigned_u,
                            task_type='CALL',
                            priority='NORMAL',
                            due_at=cb_dt,
                            status='PENDING',
                            outcome=f"AI Voice Call outcome follow-up: {call_summary}",
                            created_by_user=assigned_u,
                        )
                        if lead.current_status not in ('TRIAL_BOOKED', 'TRIAL_CONFIRMED', 'CONVERTED'):
                            prior_st = lead.current_status
                            lead.current_status = 'FOLLOW_UP_PENDING'
                            lead.save(using=alias, update_fields=['current_status', 'updated_at'])
                            LeadStatusHistory.objects.using(alias).create(
                                lead=lead,
                                from_status=prior_st,
                                to_status='FOLLOW_UP_PENDING',
                                reason_code='VOICE_CALLBACK_REQUESTED',
                                reason_text=call_summary,
                                changed_by_user=assigned_u,
                            )
                        LeadActivity.objects.using(alias).create(
                            lead=lead,
                            activity_type='FOLLOW_UP',
                            outcome=f"Callback Scheduled ({disposition})",
                            notes=f"Follow-up task created from voice call outcome. Due: {cb_dt.strftime('%A, %b %d at %I:%M %p IST')}. Summary: {call_summary}",
                            performed_by_user=assigned_u,
                        )

                # 5. Cancellation synchronization
                is_cancelled = (
                    any(k in disp_upper for k in ('CANCEL', 'DECLINED', 'NOT_INTERESTED'))
                    or any(k in next_lower for k in ('cancel', 'decline'))
                )
                if is_cancelled:
                    active_trial = TrialBooking.objects.using(alias).filter(
                        lead=lead,
                        status__in=['BOOKED', 'CONFIRMED'],
                    ).exclude(confirmation_status__in=['CANCELLED', 'DECLINED']).first()
                    if active_trial:
                        CRMLeadService.cancel_trial(
                            trial=active_trial,
                            reason=f"Cancelled via AI Voice Call outcome ({disposition})",
                            db_alias=alias,
                        )
                        LeadActivity.objects.using(alias).create(
                            lead=lead,
                            activity_type='TRIAL',
                            outcome='Trial Cancelled via AI Voice Call Outcome',
                            notes=f"Active trial cancelled following call outcome disposition '{disposition}'. Summary: {call_summary}",
                        )

                lead_first_name = lead.first_name
            else:
                lead_first_name = 'Guest'
                logger.info("Voice Agent call outcome logged for non-CRM caller: phone=%s, disposition=%s", norm_phone, disposition)

            return JsonResponse({
                'status': 'success',
                'message': 'Call outcome recorded successfully.',
                'lead_name': lead_first_name,
                'disposition': disposition,
                'recorded_notes': full_notes,
            }, status=200)


@method_decorator(csrf_exempt, name='dispatch')
class VoiceAgentRecordingAudioView(View):
    """
    Streams the raw audio recording (WAV) for an AI voice CallSession.
    Fetches and caches the audio from Sarvam Analytics Recordings API if not already cached.
    Supports browser HTML5 audio playback, scrub-seeking, and byte ranges.
    """

    def get(self, request, session_id, *args, **kwargs):
        import os
        import requests
        from datetime import timedelta
        from django.conf import settings
        from django.utils import timezone
        from apps.master.models import Tenant, TenantDataSource
        from config.tenant_middleware import _register_tenant_connection
        from config.routers import build_tenant_db_alias, set_tenant_db_alias
        from apps.tenant_core.models_crm import CallSession
        from django.http import FileResponse, Http404, HttpResponse

        def _serve_audio(path):
            file_size = os.path.getsize(path)
            http_range = request.META.get('HTTP_RANGE', '')
            start = 0
            end = file_size - 1
            status_code = 200

            if http_range:
                import re
                match = re.match(r'bytes=(\d+)-(\d*)', http_range)
                if match:
                    status_code = 206
                    start = int(match.group(1))
                    if match.group(2):
                        end = min(int(match.group(2)), file_size - 1)

            length = end - start + 1

            def stream_chunks():
                with open(path, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk_size = min(65536, remaining)
                        data = f.read(chunk_size)
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            from django.http import StreamingHttpResponse
            resp = StreamingHttpResponse(
                stream_chunks(),
                status=status_code,
                content_type='audio/wav'
            )
            resp['Accept-Ranges'] = 'bytes'
            resp['Content-Length'] = str(length)
            if status_code == 206:
                resp['Content-Range'] = f'bytes {start}-{end}/{file_size}'
            resp['Content-Disposition'] = f'inline; filename="call_{session_id}.wav"'
            resp['Access-Control-Allow-Origin'] = '*'
            resp['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            resp['Access-Control-Allow-Headers'] = 'Range, Content-Type, Accept'
            resp['Access-Control-Expose-Headers'] = 'Content-Range, Content-Length, Accept-Ranges'
            return resp

        # 1. Immediate Disk Cache Check (Zero DB queries if already cached)
        cache_dir = os.path.join(settings.BASE_DIR, 'media', 'recordings')
        os.makedirs(cache_dir, exist_ok=True)
        local_path = os.path.join(cache_dir, f"{session_id}.wav")

        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return _serve_audio(local_path)

        # 2. Locate CallSession across tenants to fetch from Sarvam if not cached
        cs = None
        target_alias = None
        for t in Tenant.objects.all():
            try:
                ds = TenantDataSource.objects.filter(tenant=t).first()
                if not ds:
                    continue
                alias = build_tenant_db_alias(t.id)
                _register_tenant_connection(alias, db_name=ds.db_name, data_source=ds, tenant_id=t.id)
                found = CallSession.objects.using(alias).filter(id=session_id).first()
                if found:
                    cs = found
                    target_alias = alias
                    break
            except Exception:
                continue

        if not cs:
            raise Http404("Call session recording not found.")

        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            api_key = os.getenv('SARVAM_API_KEY', '')
            org_id = os.getenv('SARVAM_ORG_ID', '')
            workspace_id = os.getenv('SARVAM_WORKSPACE_ID', '')
            app_id = os.getenv('SARVAM_APP_ID', '')

            inter_id = cs.metadata.get('interaction_id') if isinstance(cs.metadata, dict) else None

            # If no interaction_id in metadata, look it up via interactions list
            if not inter_id and api_key and org_id and workspace_id and app_id:
                try:
                    now = timezone.now()
                    list_url = f"https://apps.sarvam.ai/api/analytics/v1/{org_id}/{workspace_id}/{app_id}/interactions"
                    params = {
                        'start_datetime': (now - timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                        'end_datetime': (now + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                        'include_transcript': 'true',
                    }
                    lr = requests.get(list_url, headers={'X-API-Key': api_key}, params=params, timeout=12)
                    if lr.status_code == 200:
                        items = lr.json().get('items', [])
                        if items:
                            latest_item = items[-1]
                            inter_id = latest_item.get('interaction_id')
                            if isinstance(cs.metadata, dict):
                                cs.metadata['interaction_id'] = inter_id
                                if latest_item.get('original_transcript') and not cs.transcript:
                                    cs.transcript = latest_item.get('original_transcript')
                                cs.save(using=target_alias, update_fields=['metadata', 'transcript', 'updated_at'])
                except Exception as list_err:
                    logger.warning("Failed to lookup interaction ID from Sarvam: %s", list_err)

            if inter_id and api_key and org_id and workspace_id and app_id:
                encoded_inter = str(inter_id).replace('/', '%2F')
                rec_url = f"https://apps.sarvam.ai/api/analytics/v1/{org_id}/{workspace_id}/{app_id}/recordings/{encoded_inter}"
                try:
                    resp = requests.get(rec_url, headers={'X-API-Key': api_key}, timeout=25)
                    if resp.status_code == 200 and resp.content:
                        with open(local_path, 'wb') as f:
                            f.write(resp.content)
                        # Normalize telephony sample rates to 44.1kHz for universal browser/WASAPI playback
                        try:
                            from scipy.io import wavfile
                            from scipy.signal import resample_poly
                            import numpy as np
                            sr, raw_data = wavfile.read(local_path)
                            if sr in (8000, 11025, 16000, 22050):
                                up_factor = 44100 // sr
                                resampled = resample_poly(raw_data, up_factor, 1, axis=0)
                                resampled = np.clip(resampled, -32768, 32767).astype(np.int16)
                                wavfile.write(local_path, 44100, resampled)
                        except Exception as resample_err:
                            logger.warning("Audio resample fallback: %s", resample_err)
                except Exception as dl_err:
                    logger.error("Failed to download recording from Sarvam: %s", dl_err)

        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return _serve_audio(local_path)

        return HttpResponse("Audio recording is being processed by Sarvam telephony.", status=404)

    def options(self, request, *args, **kwargs):
        from django.http import HttpResponse
        response = HttpResponse()
        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Range, Content-Type, Accept'
        response['Access-Control-Expose-Headers'] = 'Content-Range, Content-Length, Accept-Ranges'
        return response



