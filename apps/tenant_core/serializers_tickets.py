from rest_framework import serializers
from .models_tickets import SupportTicket


class SupportTicketSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    member_name = serializers.SerializerMethodField()
    member_phone = serializers.SerializerMethodField()
    member_email = serializers.SerializerMethodField()
    member_number = serializers.CharField(source='user_profile.member_number', read_only=True)
    active_package_name = serializers.SerializerMethodField()
    assigned_to_name = serializers.SerializerMethodField()
    resolved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicket
        fields = [
            'id',
            'ticket_number',
            'organization',
            'branch',
            'branch_name',
            'user_profile',
            'member_name',
            'member_phone',
            'member_email',
            'member_number',
            'active_package_name',
            'category',
            'priority',
            'status',
            'subject',
            'description',
            'assigned_to',
            'assigned_to_name',
            'resolution_notes',
            'resolved_by',
            'resolved_by_name',
            'resolved_at',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'ticket_number',
            'organization',
            'resolved_at',
            'resolved_by',
            'created_at',
            'updated_at',
        ]

    def get_member_name(self, obj):
        if not obj.user_profile:
            return 'Guest'
        fn = obj.user_profile.first_name_snapshot or (obj.user_profile.user.first_name if obj.user_profile.user else '')
        ln = obj.user_profile.last_name_snapshot or (obj.user_profile.user.last_name if obj.user_profile.user else '')
        return f"{fn} {ln}".strip() or getattr(obj.user_profile.user, 'display_name', 'Member')

    def get_member_phone(self, obj):
        if obj.user_profile and obj.user_profile.user:
            return obj.user_profile.user.phone or ''
        return ''

    def get_member_email(self, obj):
        if obj.user_profile and obj.user_profile.user:
            return obj.user_profile.user.email or ''
        return ''

    def get_active_package_name(self, obj):
        if not obj.user_profile:
            return None
        alias = obj._state.db or 'default'
        from .models_memberships import Membership
        m = Membership.objects.using(alias).filter(user_profile=obj.user_profile, status='ACTIVE').first()
        return m.package.name if (m and m.package) else None

    def get_assigned_to_name(self, obj):
        if obj.assigned_to:
            return f"{obj.assigned_to.first_name} {obj.assigned_to.last_name}".strip() or obj.assigned_to.email
        return None

    def get_resolved_by_name(self, obj):
        if obj.resolved_by:
            return f"{obj.resolved_by.first_name} {obj.resolved_by.last_name}".strip() or obj.resolved_by.email
        return None
