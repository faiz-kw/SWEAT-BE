import os
from django.http import HttpResponseRedirect
import urllib.parse
"""Organisation-wide CRM configuration, Meta OAuth lifecycle, and import APIs."""
import uuid
import logging
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers, viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from .meta_lead_rules import DESTINATION_FIELDS, normalize_field_data
from .meta_logging import log_meta_event, EVT_MANUAL_RETRY_TRIGGERED
from .models_meta_leads import MetaLeadMapping, MetaLeadImport, MetaConnection, MetaPageConnection
from .models_org import Branch
from .models_crm import LeadSource, Lead, SalesFollowupTask, CRMAgentAssignmentConfig
from .models_users import TenantUser
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from .services_meta_leads import (
    require_tenant_alias,
    receive_simulation,
    process_simulation,
    process_live_import,
    simulator_enabled,
    require_simulator,
)
from .services_crm import validate_lead_email, validate_lead_phone
from .services_reliability import record_business_audit
from .views_crm import get_user_effective_branch_ids
from .meta_crypto import encrypt_token, decrypt_token, mask_token
from .services_meta_graph import (
    MetaGraphClient,
    generate_oauth_state,
    verify_oauth_state,
    get_meta_app_credentials,
    MetaTokenExpiredError,
    MetaPermissionError,
    MetaGraphAPIError,
)
from apps.master.models_tenant import MetaPageRegistry

logger = logging.getLogger(__name__)


class MappingSerializer(serializers.ModelSerializer):
    branch = serializers.UUIDField(allow_null=True, required=False)
    fallback_branch = serializers.UUIDField(allow_null=True, required=False)
    lead_source = serializers.UUIDField()
    assigned_sales_user = serializers.UUIDField(allow_null=True, required=False)
    expected_version = serializers.IntegerField(write_only=True, required=False, min_value=1)

    class Meta:
        model = MetaLeadMapping
        fields = ['id', 'page_id', 'form_id', 'name', 'is_active', 'version', 'expected_version',
                  'field_mappings', 'field_defaults', 'branch_mode', 'branch', 'branch_field', 'branch_answers',
                  'unmatched_branch_policy', 'fallback_branch', 'lead_source', 'initial_stage', 'repeat_policy',
                  'assignment_mode', 'assigned_sales_user', 'create_followup_task', 'followup_task_type',
                  'followup_due_hours', 'updated_at']
        read_only_fields = ['id', 'version', 'updated_at']
        validators = []

    def validate_page_id(self, value):
        val = str(value).strip() if value is not None else ''
        if not val or not val.isdigit():
            raise serializers.ValidationError('Facebook Page ID must contain digits only.')
        return val

    def validate_form_id(self, value):
        val = str(value).strip() if value is not None else ''
        if not val or not val.isdigit():
            raise serializers.ValidationError('Facebook Form ID must contain digits only.')
        return val

    def to_representation(self, instance):
        result = super().to_representation({
            key: getattr(instance, key + '_id' if key in ('branch', 'fallback_branch', 'lead_source', 'assigned_sales_user') else key)
            for key in self.Meta.fields if key != 'expected_version'
        })
        return result

    def validate(self, attrs):
        org, alias = self.context['organization'], self.context['alias']
        def value(key, default=None):
            if key in attrs:
                return attrs[key]
            if self.instance:
                return getattr(self.instance, key + '_id' if key in ('branch', 'fallback_branch', 'lead_source', 'assigned_sales_user') else key)
            return default
        if self.instance and attrs.get('expected_version') != self.instance.version:
            raise serializers.ValidationError({'expected_version': 'Settings changed or version is missing. Reload and save again.'})

        page_id = value('page_id')
        if page_id is not None:
            p_val = str(page_id).strip()
            if not p_val or not p_val.isdigit():
                raise serializers.ValidationError({'page_id': 'Facebook Page ID must contain digits only.'})
            attrs['page_id'] = p_val

        form_id = value('form_id')
        if form_id is not None:
            f_val = str(form_id).strip()
            if not f_val or not f_val.isdigit():
                raise serializers.ValidationError({'form_id': 'Facebook Form ID must contain digits only.'})
            attrs['form_id'] = f_val

        field_map = value('field_mappings', {})
        if not isinstance(field_map, dict) or not field_map or len(field_map) > len(DESTINATION_FIELDS):
            raise serializers.ValidationError({'field_mappings': 'Map a name and at least one contact field.'})
        if any(k not in DESTINATION_FIELDS or not isinstance(v, str) or not v.strip() or len(v) > 100 for k, v in field_map.items()):
            raise serializers.ValidationError({'field_mappings': 'Use supported CRM fields and nonempty question names up to 100 characters.'})
        if 'full_name' in field_map and ('first_name' in field_map or 'last_name' in field_map):
            raise serializers.ValidationError({'field_mappings': 'Map either full name or separate first/last names.'})
        if not ({'full_name', 'first_name'} & field_map.keys()) or not ({'email', 'phone'} & field_map.keys()):
            raise serializers.ValidationError({'field_mappings': 'A name mapping and an email or phone mapping are required.'})

        defaults = value('field_defaults', {})
        if not isinstance(defaults, dict):
            raise serializers.ValidationError({'field_defaults': 'Field defaults must be an object of field-to-value pairs.'})
        DISALLOWED_DEFAULT_FIELDS = {
            'full_name', 'first_name', 'last_name',
            'email', 'phone',
            'consent_whatsapp', 'consent_email', 'consent_sms',
        }
        for k, v in defaults.items():
            if k not in DESTINATION_FIELDS:
                raise serializers.ValidationError({'field_defaults': f"'{k}' is not a supported CRM destination field."})
            if k in DISALLOWED_DEFAULT_FIELDS:
                if k in {'full_name', 'first_name', 'last_name'}:
                    raise serializers.ValidationError({'field_defaults': f"Default values are not permitted for name fields ('{DESTINATION_FIELDS.get(k, k)}'). Each lead must provide their own name."})
                if k in {'email', 'phone'}:
                    raise serializers.ValidationError({'field_defaults': f"Default values are not permitted for contact fields ('{DESTINATION_FIELDS.get(k, k)}'). Shared contact details cause duplicate collisions and data leakage."})
                if k.startswith('consent_'):
                    raise serializers.ValidationError({'field_defaults': f"Default values cannot grant consent ('{DESTINATION_FIELDS.get(k, k)}'). Consent must be affirmatively granted by the customer in the form."})
            if not isinstance(v, (str, int, float, bool)) or len(str(v)) > 200:
                raise serializers.ValidationError({'field_defaults': 'Default values must be simple values up to 200 characters.'})

        branch_mode = value('branch_mode', 'FIXED')
        if branch_mode == 'FIXED':
            branch_id = value('branch')
            if not branch_id or not Branch.objects.using(alias).filter(id=branch_id, organization=org, status='ACTIVE').exists():
                raise serializers.ValidationError({'branch': 'Choose an active branch in this organisation.'})
            attrs.update(branch_field='', branch_answers={}, fallback_branch=None)
        else:
            if not value('branch_field', '').strip():
                raise serializers.ValidationError({'branch_field': 'Enter the branch question name.'})
            answers = value('branch_answers', {})
            if not isinstance(answers, dict) or not answers or len(answers) > 100:
                raise serializers.ValidationError({'branch_answers': 'Map between 1 and 100 branch answers.'})
            normalized = {}
            for answer, branch_id in answers.items():
                if not isinstance(answer, str) or not answer.strip() or len(answer) > 200:
                    raise serializers.ValidationError({'branch_answers': 'Branch answers must be nonempty text up to 200 characters.'})
                key = answer.strip().casefold()
                if key in normalized:
                    raise serializers.ValidationError({'branch_answers': 'Duplicate branch answer after normalization.'})
                try:
                    branch_id = uuid.UUID(str(branch_id))
                except (ValueError, TypeError):
                    raise serializers.ValidationError({'branch_answers': 'Invalid branch identifier.'})
                if not Branch.objects.using(alias).filter(id=branch_id, organization=org, status='ACTIVE').exists():
                    raise serializers.ValidationError({'branch_answers': 'Every branch must be active and belong to this organisation.'})
                normalized[key] = str(branch_id)

            unmatched_policy = value('unmatched_branch_policy', 'HOLD')
            if unmatched_policy == 'FALLBACK_BRANCH':
                fb_id = value('fallback_branch')
                if not fb_id or not Branch.objects.using(alias).filter(id=fb_id, organization=org, status='ACTIVE').exists():
                    raise serializers.ValidationError({'fallback_branch': 'Choose an active fallback branch for unmatched answers.'})
                attrs['fallback_branch'] = Branch.objects.using(alias).get(id=fb_id, organization=org)
            else:
                attrs['fallback_branch'] = None
            attrs.update(branch=None, branch_answers=normalized)

        assignment_mode = value('assignment_mode', 'TENANT_POLICY')
        if assignment_mode == 'SPECIFIC_USER':
            user_id = value('assigned_sales_user')
            if not user_id:
                raise serializers.ValidationError({'assigned_sales_user': 'Select the designated sales representative.'})
            staff = TenantUser.objects.using(alias).filter(id=user_id, organization=org, status='ACTIVE', is_login_allowed=True).first()
            if not staff:
                raise serializers.ValidationError({'assigned_sales_user': 'Designated sales representative must be an active staff member in this organisation.'})
            attrs['assigned_sales_user'] = staff
        else:
            attrs['assigned_sales_user'] = None

        initial_stage = value('initial_stage', 'NEW_LEAD')
        valid_stages = {s[0] for s in Lead.STATUSES}
        if initial_stage not in valid_stages:
            raise serializers.ValidationError({'initial_stage': 'Choose a supported CRM pipeline stage.'})

        if value('create_followup_task', False):
            task_type = value('followup_task_type', 'CALL')
            valid_types = {t[0] for t in SalesFollowupTask.TASK_TYPES}
            if task_type not in valid_types:
                raise serializers.ValidationError({'followup_task_type': 'Choose a supported followup task type.'})
            due_hours = value('followup_due_hours', 24)
            if not isinstance(due_hours, int) or due_hours < 1 or due_hours > 720:
                raise serializers.ValidationError({'followup_due_hours': 'Follow-up due hours must be between 1 and 720 (30 days).'})

        source = LeadSource.objects.using(alias).filter(id=value('lead_source'), organization=org, status='ACTIVE', source_type='META').first()
        if not source:
            raise serializers.ValidationError({'lead_source': 'Choose an active Meta lead source in this organisation.'})
        duplicate = MetaLeadMapping.objects.using(alias).filter(organization=org, page_id=value('page_id'), form_id=value('form_id'))
        if self.instance:
            duplicate = duplicate.exclude(id=self.instance.id)
        if duplicate.exists():
            raise serializers.ValidationError('This Page/form already has a mapping.')
        attrs['lead_source'] = source
        if attrs.get('branch'):
            attrs['branch'] = Branch.objects.using(alias).get(id=attrs['branch'], organization=org)
        attrs.pop('expected_version', None)
        return attrs


class ImportSerializer(serializers.ModelSerializer):
    matched_lead = serializers.SerializerMethodField()
    resolution = serializers.SerializerMethodField()

    class Meta:
        model = MetaLeadImport
        fields = [
            'id', 'mode', 'page_id', 'form_id', 'external_lead_id', 'status', 'lead',
            'mapping_version', 'attempt_count', 'error_code', 'error_message',
            'campaign_id', 'campaign_name', 'adset_id', 'adset_name', 'ad_id', 'ad_name', 'is_organic',
            'received_at', 'processed_at', 'matched_lead', 'resolution', 'field_data', 'mapping_snapshot'
        ]
        read_only_fields = fields

    def get_resolution(self, obj):
        if isinstance(obj.mapping_snapshot, dict):
            return obj.mapping_snapshot.get('resolution')
        return None

    def get_matched_lead(self, obj):
        alias = getattr(self.context.get('view'), 'alias', None) or require_tenant_alias()
        lead = obj.lead
        if not lead:
            email = None
            phone = None
            if obj.field_data and isinstance(obj.field_data, list):
                for f in obj.field_data:
                    name = (f.get('name') or '').lower()
                    vals = f.get('values') or []
                    val = vals[0] if vals else None
                    if val:
                        if 'email' in name and not email:
                            email = validate_lead_email(val, required=False)
                        elif any(p in name for p in ('phone', 'mobile', 'contact')) and not phone:
                            phone = validate_lead_phone(val, required=False)

            contact = Q()
            if email:
                contact |= Q(email_normalized=email)
            if phone:
                contact |= Q(phone_normalized=phone)
            if contact:
                lead = Lead.objects.using(alias).filter(organization_id=obj.organization_id).filter(contact).first()

        if lead:
            name = f"{lead.first_name} {lead.last_name}".strip()
            return {
                'id': str(lead.id),
                'name': name or lead.phone_normalized or lead.email_normalized or 'Existing Lead',
                'first_name': lead.first_name,
                'last_name': lead.last_name,
                'email': lead.email_normalized,
                'phone': lead.phone_normalized,
                'status': lead.current_status,
                'created_at': lead.created_at.isoformat() if lead.created_at else None,
            }
        return None


class SimulationSerializer(serializers.Serializer):
    page_id = serializers.CharField(max_length=100)
    form_id = serializers.CharField(max_length=100)
    external_lead_id = serializers.CharField(max_length=100)
    field_data = serializers.JSONField()

    def validate_field_data(self, value):
        try:
            normalize_field_data(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc))
        return value


class MetaPagination(PageNumberPagination):
    page_size = 25


class MetaAccessMixin:
    permission_classes = [RequireActiveTenantAndOrg, TenantRBACPermission]
    required_module = 'crm'
    required_submodule = 'leads'
    action_permission_map = {
        'list': 'crm.settings.view',
        'retrieve': 'crm.settings.view',
        'metadata': 'crm.settings.view',
        'audit_history': 'crm.settings.view',
        'connection': 'crm.settings.view',
        'pages': 'crm.settings.view',
        'forms': 'crm.settings.view',
        'form_fields': 'crm.settings.view',
        'create': 'crm.settings.edit',
        'partial_update': 'crm.settings.edit',
        'simulate': 'crm.settings.edit',
        'retry': 'crm.settings.edit',
        'resolve': 'crm.settings.edit',
        'oauth_init': 'crm.settings.edit',
        'oauth_init_alias': 'crm.settings.edit',
        'oauth_callback': 'crm.settings.edit',
        'oauth_callback_alias': 'crm.settings.edit',
        'disconnect': 'crm.settings.edit',
        'reconnect': 'crm.settings.edit',
        'health': 'crm.settings.view',
        'retry_failed': 'crm.settings.edit',
    }
    pagination_class = MetaPagination

    def get_authenticators(self):
        """Allow unauthenticated public access only for external OAuth callback from Meta."""
        if getattr(self, 'action', None) in ('oauth_callback', 'oauth_callback_alias'):
            return []
        return super().get_authenticators()

    def get_permissions(self):
        """Allow unauthenticated public access only for external OAuth callback from Meta."""
        if getattr(self, 'action', None) in ('oauth_callback', 'oauth_callback_alias'):
            from rest_framework.permissions import AllowAny
            return [AllowAny()]
        return super().get_permissions()

    alias = None
    organization = None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if getattr(self, 'action', None) in ('oauth_callback', 'oauth_callback_alias'):
            self.alias = None
            self.organization = None
            return
        self.alias = require_tenant_alias()
        if getattr(request.user, '_auth_type', None) != 'tenant':
            raise PermissionDenied('Sign in as a tenant administrator.')
        self.organization = getattr(request.user, 'organization', None)
        if not self.organization or self.organization.status != 'ACTIVE':
            raise PermissionDenied('An active tenant organisation is required.')
        if get_user_effective_branch_ids(request.user, self.alias) is not None:
            raise PermissionDenied('Organisation-wide CRM settings access is required.')

    def get_tenant_id(self) -> str:
        """
        Resolve current Master Tenant UUID from authenticated user context, JWT claims,
        or active DB alias mapping. Strictly validates against Master Tenant DB ('default').
        Fails closed -- never falls back to self.organization.id or unverified parameters.
        """
        from apps.master.models_tenant import Tenant
        from apps.master.models_infra import TenantDataSource

        request = getattr(self, 'request', None)

        # 1. Authoritative: user._tenant_id set by TenantJWTAuthentication
        if request and hasattr(request, 'user'):
            user_tid = getattr(request.user, '_tenant_id', None)
            if user_tid:
                t = Tenant.objects.using('default').filter(id=user_tid, status='ACTIVE').first()
                if t:
                    return str(t.id)

            # 2. JWT auth claims dictionary
            if hasattr(request, 'auth') and isinstance(request.auth, dict):
                jwt_tid = request.auth.get('tid')
                if jwt_tid:
                    t = Tenant.objects.using('default').filter(id=jwt_tid, status='ACTIVE').first()
                    if t:
                        return str(t.id)

        # 3. Dynamic tenant DB alias format: tenant_<32 hex chars>
        alias = self.alias or require_tenant_alias()
        if alias and alias.startswith('tenant_'):
            clean_hex = alias[7:]
            if len(clean_hex) == 32:
                try:
                    tenant_uuid = uuid.UUID(hex=clean_hex)
                    t = Tenant.objects.using('default').filter(id=tenant_uuid, status='ACTIVE').first()
                    if t:
                        return str(t.id)
                except (ValueError, TypeError):
                    pass

        # 4. Canonical TenantDataSource mapping for alias / database name
        if alias:
            clean_name = alias.replace('tenant_tenant_', 'tenant_').replace('tenant_', '')
            ds = TenantDataSource.objects.using('default').filter(
                Q(db_name__icontains=clean_name) | Q(database_name__icontains=clean_name),
                status='ACTIVE'
            ).first()
            if ds and ds.tenant_id:
                t = Tenant.objects.using('default').filter(id=ds.tenant_id, status='ACTIVE').first()
                if t:
                    return str(t.id)

        raise PermissionDenied('Could not resolve authoritative master tenant context.')


def is_synthetic_token(raw_or_encrypted_token: str) -> bool:
    """Detect whether a token is synthetic/mock rather than a genuine Meta OAuth token."""
    if not raw_or_encrypted_token:
        return True
    try:
        from .meta_crypto import decrypt_token
        dec = decrypt_token(raw_or_encrypted_token)
    except Exception:
        dec = raw_or_encrypted_token
    token_str = (dec or '').strip()
    return (
        token_str.startswith(('mock_', 'EAA_mock_', 'EAAMockToken', 'EAAMock'))
        or 'mock' in token_str.lower()
        or len(token_str) < 30
    )


class MetaLeadMappingViewSet(MetaAccessMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = MappingSerializer

    def get_queryset(self):
        return MetaLeadMapping.objects.using(self.alias).filter(organization=self.organization)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), 'organization': self.organization, 'alias': self.alias}

    def create(self, request):
        from django.db import IntegrityError
        try:
            with transaction.atomic(using=self.alias):
                serializer = self.get_serializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                mapping = MetaLeadMapping.objects.using(self.alias).create(organization=self.organization, **serializer.validated_data)
                self._audit(mapping, 'META_MAPPING_CREATED')
        except IntegrityError:
            raise ValidationError('A mapping for this Page/form already exists. Reload the mapping list.')
        return Response(self.get_serializer(mapping).data, status=201)

    def partial_update(self, request, pk=None):
        from django.db import IntegrityError
        try:
            with transaction.atomic(using=self.alias):
                mapping = get_object_or_404(self.get_queryset().select_for_update(), pk=pk)
                before = dict(self.get_serializer(mapping).data)
                serializer = self.get_serializer(mapping, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                for key, value in serializer.validated_data.items():
                    setattr(mapping, key, value)
                mapping.version += 1
                mapping.save(using=self.alias)
                self._audit(mapping, 'META_MAPPING_UPDATED', before)
        except IntegrityError:
            raise ValidationError('A mapping for this Page/form already exists.')
        return Response(self.get_serializer(mapping).data)

    def _audit(self, mapping, action_code, before=None):
        record_business_audit(organization=self.organization, module='crm', action_code=action_code,
                              entity_type='MetaLeadMapping', entity_id=mapping.id, actor_user=self.request.user,
                              before_data=before, after_data=dict(self.get_serializer(mapping).data),
                              metadata={'version': mapping.version, 'is_active': mapping.is_active}, db_alias=self.alias)

    @action(detail=True, methods=['get'], url_path='audit-history')
    def audit_history(self, request, pk=None):
        mapping = get_object_or_404(self.get_queryset(), pk=pk)
        from .models_audit_outbox import BusinessAuditEvent
        logs = BusinessAuditEvent.objects.using(self.alias).filter(
            organization=self.organization,
            entity_type='MetaLeadMapping',
            entity_id=mapping.id,
        ).select_related('actor_user').order_by('-occurred_at')[:50]
        results = []
        for log in logs:
            actor_name = 'System'
            if log.actor_user:
                name_str = f"{log.actor_user.first_name} {log.actor_user.last_name}".strip()
                actor_name = name_str or log.actor_user.email
            results.append({
                'id': str(log.id),
                'action_code': log.action_code,
                'occurred_at': log.occurred_at.isoformat(),
                'actor_name': actor_name,
                'actor_type': log.actor_type,
                'version': (log.metadata or {}).get('version'),
                'is_active': (log.metadata or {}).get('is_active'),
                'before_data': log.before_data,
                'after_data': log.after_data,
            })
        return Response({'results': results})

    @action(detail=False, methods=['get', 'post'], url_path='oauth_init')
    def oauth_init_alias(self, request):
        """Snake-case alias for frontend callers requesting /oauth_init/."""
        return self.oauth_init(request)

    @action(detail=False, methods=['get', 'post'], url_path='oauth_init')
    def oauth_init_alias(self, request):
        """Snake-case alias for frontend callers requesting /oauth_init/."""
        return self.oauth_init(request)

    @action(detail=False, methods=['get', 'post'], url_path='oauth-init')
    def oauth_init(self, request):
        # Strict redirect URI resolution: prioritize configured public tunnel / META_REDIRECT_URI
        redirect_uri = None
        env_redirect_uri = getattr(settings, 'META_REDIRECT_URI', None) or os.getenv('META_REDIRECT_URI', '')
        if env_redirect_uri and env_redirect_uri.strip():
            redirect_uri = env_redirect_uri.strip()
        else:
            base_url = (getattr(settings, 'BACKEND_PUBLIC_URL', None) or os.getenv('BACKEND_PUBLIC_URL', '')).rstrip('/')
            if base_url:
                redirect_uri = f"{base_url}/api/v1/tenant/meta-lead-mappings/oauth-callback/"

        if not redirect_uri:
            req_redirect = (
                request.data.get('redirect_uri')
                if request.method == 'POST'
                else request.query_params.get('redirect_uri')
            )
            # Disallow localhost:8000 when external/ngrok tunnel is needed
            if req_redirect and 'localhost:8000' not in req_redirect and '127.0.0.1:8000' not in req_redirect:
                redirect_uri = req_redirect
            elif getattr(settings, 'DEPLOYMENT_ENVIRONMENT', '') == 'production' or not settings.DEBUG:
                redirect_uri = "https://api.fitness.vibecopilot.ai/api/v1/tenant/meta-lead-mappings/oauth-callback/"
            else:
                redirect_uri = request.build_absolute_uri('/api/v1/tenant/meta-lead-mappings/oauth-callback/')

        client = MetaGraphClient()
        if not client.app_id:
            return Response({
                'live_available': False,
                'error': 'Meta Lead Ads integration is pending platform setup. META_APP_ID is not configured in backend environment.',
            }, status=status.HTTP_400_BAD_REQUEST)

        master_tenant_id = self.get_tenant_id()
        state_token = generate_oauth_state(
            tenant_id=master_tenant_id,
            organization_id=str(self.organization.id),
            user_id=str(request.user.id),
            redirect_uri=redirect_uri,
        )
        auth_url = client.build_authorize_url(state=state_token, redirect_uri=redirect_uri)

        record_business_audit(
            organization=self.organization, module='crm', action_code='META_OAUTH_INITIATED',
            entity_type='MetaConnection', entity_id=None, actor_user=request.user,
            metadata={'redirect_uri': redirect_uri}, db_alias=self.alias
        )
        return Response({
            'authorize_url': auth_url,
            'auth_url': auth_url,
            'state': state_token,
            'live_available': True
        })

    def _persist_meta_connection(
        self,
        db_alias: str,
        organization,
        tenant_id: str,
        initiating_user_id: str,
        request,
        user_token: str,
        meta_user_id: str,
        meta_user_name: str,
        expires_at,
        client: MetaGraphClient,
    ):
        """Helper to save Meta connection, discovered pages, and registry records inside tenant DB."""
        from apps.tenant_core.models_users import TenantUser
        from apps.master.models_tenant import Tenant, MetaPageRegistry

        with transaction.atomic(using=db_alias):
            conn, _ = MetaConnection.objects.using(db_alias).update_or_create(
                organization=organization,
                defaults={
                    'status': 'CONNECTED',
                    'meta_user_id': meta_user_id,
                    'meta_user_name': meta_user_name,
                    'encrypted_user_access_token': encrypt_token(user_token),
                    'token_expires_at': expires_at,
                    'scopes': ['leads_retrieval', 'pages_show_list', 'pages_read_engagement', 'pages_manage_ads'],
                    'last_connected_at': timezone.now(),
                    'last_error': '',
                }
            )

            discovered_pages = []
            try:
                pages_data = client.fetch_user_pages(user_token)
                for p in pages_data:
                    p_id = str(p['id'])
                    p_name = p.get('name', f"Page {p_id}")
                    p_token = p.get('access_token', '')

                    subscribed = False
                    if p_token:
                        try:
                            subscribed = client.subscribe_page_to_webhooks(p_id, p_token)
                        except Exception as sub_err:
                            logger.warning("Could not subscribe page %s to webhooks: %s", p_id, sub_err)

                    MetaPageConnection.objects.using(db_alias).update_or_create(
                        organization=organization,
                        page_id=p_id,
                        defaults={
                            'connection': conn,
                            'page_name': p_name,
                            'encrypted_page_access_token': encrypt_token(p_token),
                            'is_subscribed_to_webhooks': subscribed,
                            'subscribed_at': timezone.now() if subscribed else None,
                            'is_active': True,
                        }
                    )

                    if tenant_id:
                        tenant_obj = Tenant.objects.using('default').filter(id=tenant_id).first()
                        if tenant_obj:
                            MetaPageRegistry.objects.using('default').update_or_create(
                                page_id=p_id,
                                defaults={'tenant': tenant_obj, 'page_name': p_name, 'is_active': True}
                            )

                    discovered_pages.append({
                        'page_id': p_id,
                        'page_name': p_name,
                        'is_subscribed': subscribed,
                    })

            except Exception as page_exc:
                logger.error("Failed to discover pages for user %s: %s", meta_user_id, page_exc)

            actor_user = None
            if hasattr(request, 'user') and getattr(request.user, 'is_authenticated', False) and getattr(request.user, '_auth_type', None) == 'tenant':
                actor_user = request.user
            elif initiating_user_id:
                actor_user = TenantUser.objects.using(db_alias).filter(id=initiating_user_id).first()

            record_business_audit(
                organization=organization, module='crm', action_code='META_ACCOUNT_CONNECTED',
                entity_type='MetaConnection', entity_id=conn.id, actor_user=actor_user,
                actor_type='USER' if actor_user else 'SYSTEM',
                metadata={'meta_user_name': meta_user_name, 'pages_count': len(discovered_pages)}, db_alias=db_alias
            )

        return discovered_pages

    @action(detail=False, methods=['get', 'post'], url_path='oauth_callback', authentication_classes=[], permission_classes=[AllowAny])
    def oauth_callback_alias(self, request):
        """Snake-case alias for oauth-callback endpoint."""
        return self.oauth_callback(request)

    @action(detail=False, methods=['get', 'post'], url_path='oauth-callback', authentication_classes=[], permission_classes=[AllowAny])
    def oauth_callback(self, request):
        is_browser_redirect = (request.method == 'GET')
        frontend_origin = (getattr(settings, 'FRONTEND_URL', None) or os.getenv('FRONTEND_URL', '')).rstrip('/')
        target_path = '/crm/setup?tab=meta'

        # Ensure instance attributes exist immediately
        self.alias = None
        self.organization = None

        # Check for Meta authorization errors / user cancellation
        fb_error = (
            request.query_params.get('error')
            or request.query_params.get('error_code')
            or request.query_params.get('error_reason')
            if is_browser_redirect else
            (request.data.get('error') or request.data.get('error_code') or request.data.get('error_reason'))
        )
        fb_desc = (
            request.query_params.get('error_description')
            or request.query_params.get('error_message')
            or ''
            if is_browser_redirect else
            (request.data.get('error_description') or request.data.get('error_message') or '')
        )
        if fb_error:
            logger.warning("Meta OAuth error callback received: error=%s desc=%s", fb_error, fb_desc)
            if is_browser_redirect:
                clean_err = urllib.parse.quote(str(fb_error)[:100])
                clean_desc = urllib.parse.quote(str(fb_desc)[:200])
                return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_error={clean_err}&meta_desc={clean_desc}")
            return Response({'error': str(fb_error), 'description': str(fb_desc)}, status=status.HTTP_400_BAD_REQUEST)

        if is_browser_redirect:
            code = request.query_params.get('code')
            state_token = request.query_params.get('state')
            redirect_uri = request.query_params.get('redirect_uri')
        else:
            code = request.data.get('code')
            state_token = request.data.get('state')
            redirect_uri = request.data.get('redirect_uri')

        if not code or not state_token:
            if is_browser_redirect:
                return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_error=missing_code_or_state")
            raise ValidationError('code and state are required.')

        try:
            # Cryptographically verify HMAC signature, 15-min TTL, and atomically consume single-use nonce
            state_data = verify_oauth_state(state_token, consume=True)
        except ValueError as exc:
            logger.warning("OAuth callback rejected: %s", exc)
            if is_browser_redirect:
                return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_error=invalid_state&meta_desc={urllib.parse.quote(str(exc))}")
            raise ValidationError({'state': str(exc)})

        # Extract verified binding values from signed state
        target_tenant_id = state_data.get('tenant_id')
        expected_org_id = state_data.get('organization_id')
        initiating_user_id = state_data.get('user_id')

        if not target_tenant_id or not expected_org_id:
            logger.warning("OAuth callback rejected: state missing tenant_id or organization_id")
            if is_browser_redirect:
                return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_error=invalid_state&meta_desc=missing_tenant_or_org")
            raise ValidationError('OAuth state missing tenant or organisation binding.')

        # Resolve tenant from Master DB using tenant_id
        from apps.master.models_tenant import Tenant
        tenant = Tenant.objects.using('default').filter(id=target_tenant_id).first()
        if not tenant:
            logger.warning("OAuth callback rejected: Tenant %s not found in master DB", target_tenant_id)
            if is_browser_redirect:
                return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_error=tenant_not_found")
            raise PermissionDenied('Tenant bound to OAuth state not found.')

        if tenant.status != 'ACTIVE':
            logger.warning("OAuth callback rejected: Tenant %s is inactive (%s)", target_tenant_id, tenant.status)
            if is_browser_redirect:
                return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_error=tenant_inactive")
            raise PermissionDenied('Tenant bound to OAuth state is inactive.')

        from apps.tenant_core.context import tenant_database_context
        from apps.tenant_core.models_org import Organization

        with tenant_database_context(str(tenant.id)) as resolved_alias:
            resolved_org = Organization.objects.using(resolved_alias).filter(id=expected_org_id).first()
            if not resolved_org:
                logger.error("OAuth callback: Organization %s not found in tenant DB %s", expected_org_id, resolved_alias)
                if is_browser_redirect:
                    return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_error=organization_not_found")
                raise PermissionDenied('Tenant organisation bound to state not found.')

            if resolved_org.status != 'ACTIVE':
                logger.warning("OAuth callback rejected: Organization %s is inactive", expected_org_id)
                if is_browser_redirect:
                    return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_error=organization_inactive")
                raise PermissionDenied('Tenant organisation bound to state is inactive.')

            # Explicitly initialize callback context on view instance
            self.alias = resolved_alias
            self.organization = resolved_org

            # Resolve redirect_uri: prefer state-preserved URI, then env/settings, then fallback
            if not redirect_uri:
                redirect_uri = state_data.get('redirect_uri')
            if not redirect_uri:
                env_redirect_uri = getattr(settings, 'META_REDIRECT_URI', None) or os.getenv('META_REDIRECT_URI', '')
                if env_redirect_uri and env_redirect_uri.strip():
                    redirect_uri = env_redirect_uri.strip()
                else:
                    base_url = (getattr(settings, 'BACKEND_PUBLIC_URL', None) or os.getenv('BACKEND_PUBLIC_URL', '')).rstrip('/')
                    if base_url:
                        redirect_uri = f"{base_url}/api/v1/tenant/meta-lead-mappings/oauth-callback/"
                    else:
                        redirect_uri = request.build_absolute_uri('/api/v1/tenant/meta-lead-mappings/oauth-callback/')

            client = MetaGraphClient()
            try:
                tokens_data = client.exchange_code_for_tokens(code=code, redirect_uri=redirect_uri)
            except Exception as exc:
                logger.error("Meta token exchange failed: %s", exc)
                if is_browser_redirect:
                    return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_error=token_exchange_failed&meta_desc={urllib.parse.quote(str(exc)[:150])}")
                return Response({'error': f'Failed to exchange authorization code with Meta: {exc}'}, status=status.HTTP_400_BAD_REQUEST)

            user_token = tokens_data['user_access_token']
            meta_user_id = tokens_data['meta_user_id']
            meta_user_name = tokens_data['meta_user_name']
            from datetime import timedelta
            expires_at = timezone.now() + timedelta(seconds=tokens_data.get('expires_in', 5184000))

            discovered_pages = self._persist_meta_connection(
                db_alias=resolved_alias,
                organization=resolved_org,
                tenant_id=str(tenant.id),
                initiating_user_id=initiating_user_id,
                request=request,
                user_token=user_token,
                meta_user_id=meta_user_id,
                meta_user_name=meta_user_name,
                expires_at=expires_at,
                client=client,
            )

            if is_browser_redirect:
                return HttpResponseRedirect(f"{frontend_origin}{target_path}&meta_connected=true&pages={len(discovered_pages)}")

            return Response({
                'status': 'CONNECTED',
                'meta_user_name': meta_user_name,
                'pages': discovered_pages,
                'expires_at': expires_at.isoformat(),
            })

    @action(detail=False, methods=['get'], url_path='connection')
    def connection(self, request):
        conn = MetaConnection.objects.using(self.alias).filter(organization=self.organization).first()
        if not conn:
            return Response({'status': 'NOT_CONNECTED', 'pages': []})

        pages = list(MetaPageConnection.objects.using(self.alias).filter(
            organization=self.organization, is_active=True
        ).values('id', 'page_id', 'page_name', 'is_subscribed_to_webhooks', 'subscribed_at'))

        token_is_synthetic = is_synthetic_token(conn.encrypted_user_access_token)
        sim_active = simulator_enabled()

        if conn.status == 'NOT_CONNECTED' or not conn.encrypted_user_access_token:
            conn_status = 'NOT_CONNECTED'
            is_live_conn = False
        elif token_is_synthetic:
            conn_status = 'SIMULATOR_MOCK' if sim_active else 'NOT_CONNECTED'
            is_live_conn = False
        else:
            conn_status = conn.status
            is_live_conn = conn.status in ('CONNECTED', 'LIVE_CONNECTED')

        return Response({
            'status': conn_status,
            'is_connected': is_live_conn,
            'is_synthetic': token_is_synthetic,
            'meta_user_id': conn.meta_user_id if (not token_is_synthetic or sim_active) else '',
            'meta_user_name': conn.meta_user_name if (not token_is_synthetic or sim_active) else '',
            'token_expires_at': conn.token_expires_at.isoformat() if conn.token_expires_at else None,
            'scopes': conn.scopes,
            'last_connected_at': conn.last_connected_at.isoformat() if conn.last_connected_at else None,
            'last_error': conn.last_error,
            'pages': pages if (not token_is_synthetic or sim_active) else [],
        })

    @action(detail=False, methods=['post'], url_path='disconnect')
    def disconnect(self, request):
        conn = MetaConnection.objects.using(self.alias).filter(organization=self.organization).first()
        if not conn:
            return Response({'status': 'NOT_CONNECTED'})

        with transaction.atomic(using=self.alias):
            client = MetaGraphClient()
            for p_conn in MetaPageConnection.objects.using(self.alias).filter(organization=self.organization):
                token = decrypt_token(p_conn.encrypted_page_access_token)
                if token:
                    try:
                        client.unsubscribe_page_from_webhooks(p_conn.page_id, token)
                    except Exception:
                        pass
                p_conn.encrypted_page_access_token = ''
                p_conn.is_subscribed_to_webhooks = False
                p_conn.is_active = False
                p_conn.save(using=self.alias)

            conn.status = 'DISCONNECTED'
            conn.encrypted_user_access_token = ''
            conn.last_error = ''
            conn.save(using=self.alias)

            record_business_audit(
                organization=self.organization, module='crm', action_code='META_ACCOUNT_DISCONNECTED',
                entity_type='MetaConnection', entity_id=conn.id, actor_user=request.user,
                metadata={'status': 'DISCONNECTED'}, db_alias=self.alias
            )

        return Response({'status': 'DISCONNECTED'})

    @action(detail=False, methods=['get'], url_path='pages')
    def pages(self, request):
        pages = list(MetaPageConnection.objects.using(self.alias).filter(
            organization=self.organization, is_active=True
        ).values('id', 'page_id', 'page_name', 'is_subscribed_to_webhooks'))
        return Response({'results': pages})

    @action(detail=False, methods=['get'], url_path='forms')
    def forms(self, request):
        page_id = request.query_params.get('page_id')
        if not page_id:
            raise ValidationError({'page_id': 'Query parameter page_id is required.'})

        page_conn = MetaPageConnection.objects.using(self.alias).filter(
            organization=self.organization, page_id=page_id, is_active=True
        ).first()

        if not page_conn:
            return Response({'results': [], 'notice': 'Page is not connected. Enter form ID manually.'})

        token = decrypt_token(page_conn.encrypted_page_access_token)
        if not token:
            return Response({'results': [], 'notice': 'Page token not available. Re-authorize connection.'})

        client = MetaGraphClient()
        try:
            forms_data = client.fetch_page_forms(page_id, token)
            results = []
            for f in forms_data:
                results.append({
                    'id': f['id'],
                    'name': f.get('name', f"Form {f['id']}"),
                    'status': f.get('status', 'ACTIVE'),
                    'questions_count': len(f.get('questions', [])),
                })
            return Response({'results': results})
        except MetaTokenExpiredError:
            return Response({'results': [], 'error': 'TOKEN_EXPIRED', 'message': 'Page token expired. Please reconnect.'}, status=401)
        except Exception as exc:
            logger.warning("Could not fetch forms for page %s: %s", page_id, exc)
            return Response({'results': [], 'error': str(exc)})

    @action(detail=False, methods=['get'], url_path='form-fields')
    def form_fields(self, request):
        page_id = request.query_params.get('page_id')
        form_id = request.query_params.get('form_id')
        if not page_id or not form_id:
            raise ValidationError('Both page_id and form_id are required.')

        page_conn = MetaPageConnection.objects.using(self.alias).filter(
            organization=self.organization, page_id=page_id, is_active=True
        ).first()

        if not page_conn:
            return Response({'questions': []})

        token = decrypt_token(page_conn.encrypted_page_access_token)
        if not token:
            return Response({'questions': []})

        client = MetaGraphClient()
        try:
            form_data = client.fetch_form_details(form_id, token)
            questions = form_data.get('questions', [])
            mapped_questions = []
            for q in questions:
                mapped_questions.append({
                    'key': q.get('key') or q.get('field_key') or q.get('name', ''),
                    'label': q.get('label') or q.get('name', ''),
                    'type': q.get('type', 'CUSTOM'),
                    'options': [opt.get('value') or opt.get('key') for opt in q.get('options', [])] if q.get('options') else [],
                })
            return Response({'questions': mapped_questions, 'form_name': form_data.get('name', '')})
        except Exception as exc:
            logger.warning("Could not fetch form fields for %s: %s", form_id, exc)
            return Response({'questions': [], 'error': str(exc)})

    @action(detail=False, methods=['get'])
    def metadata(self, request):
        config = CRMAgentAssignmentConfig.objects.using(self.alias).filter(organization=self.organization).first()
        policy_summary = {
            'mode_allowed': config.assignment_mode_allowed if config else 'BOTH',
            'auto_strategy': config.auto_assignment_strategy if config else 'ROUND_ROBIN',
            'allow_unassigned_fallback': config.allow_unassigned_fallback if config else True,
            'require_branch_match': config.require_branch_match if config else True,
        }
        eligible_users = list(TenantUser.objects.using(self.alias).filter(
            organization=self.organization, status='ACTIVE', is_login_allowed=True
        ).order_by('first_name', 'last_name').values('id', 'first_name', 'last_name', 'email'))
        for u in eligible_users:
            u['name'] = f"{u['first_name']} {u['last_name']}".strip() or u['email']

        conn = MetaConnection.objects.using(self.alias).filter(organization=self.organization).first()
        app_id, _, _ = get_meta_app_credentials()

        token_is_synthetic = is_synthetic_token(conn.encrypted_user_access_token) if conn else True
        sim_active = simulator_enabled()
        if token_is_synthetic:
            conn_status = 'SIMULATOR_MOCK' if (sim_active and conn and conn.status == 'CONNECTED') else (conn.status if (conn and conn.status != 'CONNECTED') else 'NOT_CONNECTED')
            is_live_conn = False
        else:
            conn_status = conn.status if conn else 'NOT_CONNECTED'
            is_live_conn = conn.status in ('CONNECTED', 'LIVE_CONNECTED') if conn else False

        conn_details = None
        if conn and is_live_conn:
            conn_details = {
                'id': str(conn.id),
                'is_connected': is_live_conn,
                'meta_user_id': conn.meta_user_id,
                'meta_user_name': conn.meta_user_name,
                'scopes': conn.scopes or [],
                'expires_at': conn.token_expires_at.isoformat() if conn.token_expires_at else None,
                'created_at': conn.created_at.isoformat(),
                'updated_at': conn.updated_at.isoformat(),
            }

        pages_list = list(MetaPageConnection.objects.using(self.alias).filter(
            organization=self.organization, is_active=True
        ).values('id', 'page_id', 'page_name', 'is_subscribed_to_webhooks', 'subscribed_at')) if (conn and is_live_conn) else []

        return Response({
            'connection_status': conn_status,
            'connected_user_name': conn.meta_user_name if (conn and (not token_is_synthetic or sim_active)) else '',
            'connected_user_id': conn.meta_user_id if (conn and (not token_is_synthetic or sim_active)) else '',
            'token_expires_at': conn.token_expires_at.isoformat() if conn and conn.token_expires_at else None,
            'is_connected': is_live_conn,
            'is_synthetic': token_is_synthetic,
            'connection': conn_details,
            'pages': pages_list,
            'live_available': bool(app_id),
            'app_id_configured': bool(app_id),
            'meta_app_configured': bool(app_id),
            'webhook_endpoint': '/api/v1/webhooks/meta/leads/',
            'simulator_enabled': simulator_enabled(),
            'simulator_requirement': 'Development/test deployment, DEBUG enabled, META_LEAD_SIMULATOR_ENABLED enabled and COMMUNICATIONS_OUTBOUND_ENABLED disabled.',
            'destination_fields': [{'value': k, 'label': v} for k, v in DESTINATION_FIELDS.items()],
            'allowed_default_fields': [
                {'value': k, 'label': v}
                for k, v in DESTINATION_FIELDS.items()
                if k not in {
                    'full_name', 'first_name', 'last_name',
                    'email', 'phone',
                    'consent_whatsapp', 'consent_email', 'consent_sms',
                }
            ],
            'disallowed_default_fields': [
                'full_name', 'first_name', 'last_name',
                'email', 'phone',
                'consent_whatsapp', 'consent_email', 'consent_sms',
            ],
            'branch_modes': [{'value': k, 'label': v} for k, v in MetaLeadMapping._meta.get_field('branch_mode').choices],
            'unmatched_branch_policies': [{'value': k, 'label': v} for k, v in MetaLeadMapping.UNMATCHED_BRANCH_POLICIES],
            'repeat_policies': [{'value': k, 'label': v} for k, v in MetaLeadMapping._meta.get_field('repeat_policy').choices],
            'initial_stages': [{'value': k, 'label': v} for k, v in Lead.STATUSES if k in ('NEW_LEAD', 'FOLLOW_UP_PENDING', 'INTERESTED', 'HOT_LEAD')],
            'assignment_modes': [{'value': k, 'label': v} for k, v in MetaLeadMapping.ASSIGNMENT_MODES],
            'task_types': [{'value': k, 'label': v} for k, v in SalesFollowupTask.TASK_TYPES],
            'branches': list(Branch.objects.using(self.alias).filter(organization=self.organization, status='ACTIVE').values('id', 'name', 'code')),
            'lead_sources': list(LeadSource.objects.using(self.alias).filter(organization=self.organization, source_type='META', status='ACTIVE').values('id', 'name')),
            'eligible_users': eligible_users,
            'tenant_assignment_policy': policy_summary,
            'phone_validation': 'Current CRM accepts Indian mobile numbers. Use email-only tests for other countries until international phone support is implemented.',
        })


    @action(detail=False, methods=['get'], url_path='health')
    def health(self, request):
        now = timezone.now()
        since_24h = now - timedelta(hours=24)

        conn = MetaConnection.objects.using(self.alias).filter(organization=self.organization).first()
        app_id, _, _ = get_meta_app_credentials()

        token_is_synthetic = is_synthetic_token(conn.encrypted_user_access_token) if conn else True
        sim_active = simulator_enabled()

        if not conn or not conn.encrypted_user_access_token:
            conn_status = 'NOT_CONNECTED'
            is_connected = False
        elif token_is_synthetic:
            conn_status = 'SIMULATOR_MOCK' if sim_active else 'NOT_CONNECTED'
            is_connected = False
        else:
            conn_status = conn.status
            is_connected = conn.status in ('CONNECTED', 'LIVE_CONNECTED')

        live_imports = MetaLeadImport.objects.using(self.alias).filter(
            organization=self.organization, mode='LIVE'
        )

        received_24h = live_imports.filter(received_at__gte=since_24h).count()
        success_24h = live_imports.filter(status='IMPORTED', received_at__gte=since_24h).count()

        pending_count = live_imports.filter(status='PENDING').count()
        processing_count = live_imports.filter(status='PROCESSING').count()
        retrying_count = live_imports.filter(status='RETRYING').count()
        failed_count = live_imports.filter(status='FAILED').count()
        dead_letter_count = live_imports.filter(status='DEAD_LETTER').count()
        needs_mapping_count = live_imports.filter(status='NEEDS_MAPPING').count()
        needs_review_count = live_imports.filter(status='NEEDS_REVIEW').count()

        last_received = live_imports.order_by('-received_at').first()
        last_success = live_imports.filter(status='IMPORTED').order_by('-processed_at').first()
        last_failed = live_imports.filter(status__in=['FAILED', 'DEAD_LETTER']).order_by('-updated_at').first()

        oldest_pending = live_imports.filter(status__in=['PENDING', 'PROCESSING']).order_by('received_at').first()
        oldest_pending_age_seconds = int((now - oldest_pending.received_at).total_seconds()) if oldest_pending else 0

        page_count = MetaPageConnection.objects.using(self.alias).filter(
            organization=self.organization, is_active=True
        ).count()

        form_mapping_count = MetaLeadMapping.objects.using(self.alias).filter(
            organization=self.organization, is_active=True
        ).count()

        if not is_connected:
            overall_status = 'NOT_CONNECTED'
        elif dead_letter_count > 0 or failed_count > 5 or oldest_pending_age_seconds > 600:
            overall_status = 'NEEDS_ATTENTION'
        elif retrying_count > 0 or needs_mapping_count > 0:
            overall_status = 'DEGRADED'
        else:
            overall_status = 'HEALTHY'

        return Response({
            'overall_status': overall_status,
            'connection_status': conn_status,
            'is_connected': is_connected,
            'meta_app_configured': bool(app_id),
            'last_webhook_received_at': last_received.received_at.isoformat() if last_received else None,
            'last_successful_lead_at': last_success.processed_at.isoformat() if (last_success and last_success.processed_at) else None,
            'last_failed_lead_at': last_failed.updated_at.isoformat() if last_failed else None,
            'received_count_24h': received_24h,
            'success_count_24h': success_24h,
            'pending_count': pending_count,
            'processing_count': processing_count,
            'retrying_count': retrying_count,
            'failed_count': failed_count,
            'dead_letter_count': dead_letter_count,
            'needs_mapping_count': needs_mapping_count,
            'needs_review_count': needs_review_count,
            'oldest_pending_age_seconds': oldest_pending_age_seconds,
            'page_connection_count': page_count,
            'active_form_mapping_count': form_mapping_count,
        })


class MetaLeadImportViewSet(MetaAccessMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = ImportSerializer

    def get_queryset(self):
        qs = MetaLeadImport.objects.using(self.alias).filter(organization=self.organization)
        params = getattr(self.request, 'query_params', getattr(self.request, 'GET', {}))
        mode = params.get('mode')
        if mode:
            qs = qs.filter(mode=mode)
        status = params.get('status')
        if status:
            qs = qs.filter(status=status)
        return qs

    def retrieve(self, request, *args, **kwargs):
        event = self.get_object()
        return Response({
            **self.get_serializer(event).data,
            'field_data': event.field_data,
            'mapping_snapshot': event.mapping_snapshot,
        })

    @action(detail=False, methods=['post'])
    def simulate(self, request):
        require_simulator()
        serializer = SimulationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event, created = receive_simulation(self.organization, serializer.validated_data, request.user)
        return Response({**self.get_serializer(event).data, 'duplicate_delivery': not created}, status=201 if created else 200)

    @action(detail=True, methods=['post'])
    def resolve(self, request, pk=None):
        event = self.get_object()

        # Idempotent response if already resolved
        if event.status == 'RESOLVED':
            return Response(self.get_serializer(event).data, status=status.HTTP_200_OK)

        if event.status == 'IMPORTED':
            raise ValidationError({'detail': 'Imported submissions cannot be dismissed as repeat enquiries.'})

        reason = (request.data.get('reason') or '').strip()
        if not reason:
            raise ValidationError({'reason': 'A resolution reason is required.'})

        action_type = request.data.get('action') or 'DISMISSED'

        serializer = self.get_serializer(event)
        matched_lead_data = serializer.get_matched_lead(event)
        matched_lead = None
        if matched_lead_data:
            matched_lead = Lead.objects.using(self.alias).filter(
                id=matched_lead_data['id'], organization_id=self.organization.id
            ).first()

        actor_name = 'User'
        if request.user:
            name_parts = f"{getattr(request.user, 'first_name', '')} {getattr(request.user, 'last_name', '')}".strip()
            actor_name = name_parts or getattr(request.user, 'email', str(request.user.id))

        now_iso = timezone.now().isoformat()
        resolution_data = {
            'resolved_by_id': str(request.user.id),
            'resolved_by_name': actor_name,
            'resolved_at': now_iso,
            'reason': reason,
            'action': action_type,
            'previous_status': event.status,
            'previous_error_code': event.error_code,
            'matched_lead_id': str(matched_lead.id) if matched_lead else (str(event.lead_id) if event.lead_id else None),
        }

        if not isinstance(event.mapping_snapshot, dict):
            event.mapping_snapshot = {}
        event.mapping_snapshot['resolution'] = resolution_data

        if not event.lead and matched_lead:
            event.lead = matched_lead

        before_status = event.status
        event.status = 'RESOLVED'
        event.error_message = f"Resolved by {actor_name}: {reason}"[:500]
        event.processed_at = timezone.now()
        event.save(using=self.alias)

        record_business_audit(
            organization=self.organization,
            module='crm',
            action_code='META_IMPORT_RESOLVED',
            entity_type='MetaLeadImport',
            entity_id=event.id,
            actor_user=request.user,
            before_data={'status': before_status, 'error_code': event.error_code},
            after_data={'status': 'RESOLVED', 'reason': reason, 'action': action_type},
            metadata={
                'resolved_by': actor_name,
                'resolved_by_id': str(request.user.id),
                'resolved_at': now_iso,
                'reason': reason,
                'action': action_type,
                'matched_lead_id': str(event.lead_id) if event.lead_id else None,
                'external_lead_id': event.external_lead_id,
            },
            db_alias=self.alias,
        )

        return Response(self.get_serializer(event).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        event = self.get_object()
        if event.status in ('IMPORTED', 'RESOLVED'):
            return Response(
                {'error': 'CANNOT_RETRY_SUCCESS', 'detail': 'Cannot retry an import that has already succeeded.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        log_meta_event(
            EVT_MANUAL_RETRY_TRIGGERED,
            f"Manual retry initiated by user {request.user.id}",
            import_id=str(event.id),
            tenant_id=str(self.organization.id),
            page_id=event.page_id,
            form_id=event.form_id,
            leadgen_id=event.external_lead_id,
            status=event.status,
        )

        event.attempt_count += 1
        event.status = 'PENDING'
        event.save(using=self.alias, update_fields=['attempt_count', 'status', 'updated_at'])

        if event.mode == 'LIVE':
            event = process_live_import(self.organization, event.id, request.user, alias=self.alias)
        else:
            event = process_simulation(self.organization, event.id, request.user)

        record_business_audit(
            organization=self.organization,
            module='crm',
            action_code='META_IMPORT_MANUALLY_RETRIED',
            entity_type='MetaLeadImport',
            entity_id=event.id,
            actor_user=request.user,
            metadata={'attempt_count': event.attempt_count, 'new_status': event.status},
            db_alias=self.alias,
        )

        return Response(self.get_serializer(event).data)

    @action(detail=False, methods=['post'], url_path='retry-failed')
    def retry_failed(self, request):
        failed_qs = MetaLeadImport.objects.using(self.alias).filter(
            organization=self.organization,
            mode='LIVE',
            status__in=['FAILED', 'DEAD_LETTER'],
        ).order_by('-received_at')[:20]

        retried_ids = []
        for imp in failed_qs:
            imp.attempt_count += 1
            imp.status = 'PENDING'
            imp.save(using=self.alias, update_fields=['attempt_count', 'status', 'updated_at'])
            from apps.tenant_core.tasks_meta_leads import process_meta_lead_import_task
            tenant_id = self.get_tenant_id()
            if tenant_id:
                process_meta_lead_import_task.delay(str(tenant_id), str(imp.id))
                retried_ids.append(str(imp.id))

        return Response({
            'status': 'QUEUED',
            'retried_count': len(retried_ids),
            'retried_ids': retried_ids,
        }, status=status.HTTP_200_OK)
