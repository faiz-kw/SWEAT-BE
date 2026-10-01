"""Organisation-wide CRM configuration and development import APIs."""
import uuid
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from .meta_lead_rules import DESTINATION_FIELDS, normalize_field_data
from .models_meta_leads import MetaLeadMapping, MetaLeadImport
from .models_org import Branch
from .models_crm import LeadSource, Lead, SalesFollowupTask, CRMAgentAssignmentConfig
from .models_users import TenantUser
from .permissions import RequireActiveTenantAndOrg, TenantRBACPermission
from .services_meta_leads import require_tenant_alias, receive_simulation, process_simulation, simulator_enabled
from .services_reliability import record_business_audit
from .views_crm import get_user_effective_branch_ids


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
        validators = []  # Organisation is trusted server context, never a client field.

    def to_representation(self, instance):
        # UUIDField must receive IDs, not model instances.
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

        # Sales assignment validation
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

        # Initial stage validation
        initial_stage = value('initial_stage', 'NEW_LEAD')
        valid_stages = {s[0] for s in Lead.STATUSES}
        if initial_stage not in valid_stages:
            raise serializers.ValidationError({'initial_stage': 'Choose a supported CRM pipeline stage.'})

        # Follow-up task validation
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
    class Meta:
        model = MetaLeadImport
        fields = ['id', 'mode', 'page_id', 'form_id', 'external_lead_id', 'status', 'lead',
                  'mapping_version', 'attempt_count', 'error_code', 'error_message', 'received_at', 'processed_at']
        read_only_fields = fields


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
    required_submodule = 'settings'
    action_permission_map = {'list': 'crm.settings.view', 'retrieve': 'crm.settings.view', 'metadata': 'crm.settings.view',
                             'audit_history': 'crm.settings.view',
                             'create': 'crm.settings.edit', 'partial_update': 'crm.settings.edit',
                             'simulate': 'crm.settings.edit', 'retry': 'crm.settings.edit'}
    pagination_class = MetaPagination

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.alias = require_tenant_alias()
        # Integration routing is organisation-wide; branch staff must not see other branches' intake data.
        if getattr(request.user, '_auth_type', None) != 'tenant':
            raise PermissionDenied('Sign in as a tenant administrator.')
        self.organization = getattr(request.user, 'organization', None)
        if not self.organization or self.organization.status != 'ACTIVE':
            raise PermissionDenied('An active tenant organisation is required.')
        if get_user_effective_branch_ids(request.user, self.alias) is not None:
            raise PermissionDenied('Organisation-wide CRM settings access is required.')


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

        return Response({
            'connection_status': 'NOT_CONNECTED', 'live_available': False,
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


class MetaLeadImportViewSet(MetaAccessMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = ImportSerializer

    def get_queryset(self):
        qs = MetaLeadImport.objects.using(self.alias).filter(organization=self.organization, mode='SIMULATOR')
        status = self.request.query_params.get('status')
        return qs.filter(status=status) if status else qs

    def retrieve(self, request, *args, **kwargs):
        event = self.get_object()
        return Response({**self.get_serializer(event).data, 'field_data': event.field_data, 'mapping_snapshot': event.mapping_snapshot})

    @action(detail=False, methods=['post'])
    def simulate(self, request):
        serializer = SimulationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event, created = receive_simulation(self.organization, serializer.validated_data, request.user)
        return Response({**self.get_serializer(event).data, 'duplicate_delivery': not created}, status=201 if created else 200)

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        event = self.get_object()
        event = process_simulation(self.organization, event.id, request.user)
        return Response(self.get_serializer(event).data)
