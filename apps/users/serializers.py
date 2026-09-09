from rest_framework import serializers
from apps.tenants.models import Location
from .models import User, RoleDefinition, PermissionDefinition, RolePermission


class LocationBriefSerializer(serializers.ModelSerializer):
    """Minimal location info for embedding in user profile responses."""
    class Meta:
        model = Location
        fields = ['id', 'name', 'city', 'address']


class PermissionDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PermissionDefinition
        fields = ['id', 'module', 'action', 'label', 'scope', 'description']


class RolePermissionSerializer(serializers.ModelSerializer):
    permission = PermissionDefinitionSerializer(read_only=True)
    permission_id = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = RolePermission
        fields = ['id', 'role', 'permission', 'permission_id', 'granted']


class RoleDefinitionSerializer(serializers.ModelSerializer):
    code = serializers.CharField(required=False, allow_blank=True, default='')
    description = serializers.CharField(required=False, allow_blank=True, default='')
    permissions = RolePermissionSerializer(many=True, read_only=True)
    users_count = serializers.SerializerMethodField()
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)

    class Meta:
        model = RoleDefinition
        fields = [
            'id', 'tenant', 'tenant_name', 'name', 'code', 'scope', 'description', 
            'is_system', 'permissions', 'users_count', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'is_system']

    def get_users_count(self, obj):
        return obj.assigned_users.count() if hasattr(obj, 'assigned_users') else 0



class UserSerializer(serializers.ModelSerializer):
    tenant_id = serializers.SerializerMethodField()
    tenant_name = serializers.SerializerMethodField()
    active_location_id = serializers.CharField(source='active_location.id', read_only=True)
    active_location_name = serializers.CharField(source='active_location.name', read_only=True)
    allowed_locations = LocationBriefSerializer(many=True, read_only=True)
    allowed_locations_list = LocationBriefSerializer(source='allowed_locations', many=True, read_only=True)
    allowed_location_ids = serializers.ListField(child=serializers.CharField(), write_only=True, required=False)
    password = serializers.CharField(write_only=True, required=False, min_length=8)
    is_superuser = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id',
            'email',
            'first_name',
            'last_name',
            'full_name',
            'phone',
            'avatar_url',
            'emergency_contact',
            'tenant_id',
            'tenant_name',
            'role',
            'is_superuser',
            'role_definition',
            'active_location',
            'active_location_id',
            'active_location_name',
            'allowed_locations',
            'allowed_locations_list',
            'allowed_location_ids',
            'status',
            'is_active',
            'password',
            'last_login',
            'date_joined',
        ]
        read_only_fields = ['id', 'tenant_id', 'tenant_name', 'full_name', 'last_login', 'date_joined', 'is_superuser']

    def get_tenant_id(self, obj):
        return obj.tenant.id if obj.tenant else None

    def get_tenant_name(self, obj):
        if obj.tenant:
            return obj.tenant.name
        # Super admins have no tenant — return platform name
        return 'Global Platform HQ'

    def get_full_name(self, obj) -> str:
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name if name else obj.email.split('@')[0].title()

    def create(self, validated_data):
        allowed_location_ids = validated_data.pop('allowed_location_ids', [])
        password = validated_data.pop('password', 'Pass1234!')
        user = User.objects.create_user(password=password, **validated_data)
        if allowed_location_ids:
            locations = Location.objects.filter(id__in=allowed_location_ids)
            user.allowed_locations.set(locations)
        return user

    def update(self, instance, validated_data):
        allowed_location_ids = validated_data.pop('allowed_location_ids', None)
        password = validated_data.pop('password', None)
        if password:
            instance.set_password(password)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if allowed_location_ids is not None:
            locations = Location.objects.filter(id__in=allowed_location_ids)
            instance.allowed_locations.set(locations)
        return instance


class UserInviteSerializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    role = serializers.CharField(max_length=64, default='Trainer')
    location_ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)

