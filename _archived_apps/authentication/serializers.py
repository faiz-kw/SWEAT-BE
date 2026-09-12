"""
Authentication Serializers with custom JWT payload claims.
Fulfills the 6-point architecture contract:
Claims: sub, tid, role, loc, act_loc, exp
"""

from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Customizes the JWT claims embedded inside the 15-minute access token.
    """
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # 1. User ID (sub)
        token['sub'] = str(user.id)

        # 2. Tenant ID (tid) — empty string for super admins (no tenant)
        token['tid'] = str(user.tenant.id) if user.tenant else ''

        # 3. Role
        token['role'] = str(user.role)

        # 4. is_superuser flag in token
        token['is_superuser'] = user.is_superuser

        # 5. Allowed locations (loc)
        allowed_locs = list(user.allowed_locations.values_list('id', flat=True))
        token['loc'] = allowed_locs if allowed_locs else []

        # 6. Active location (act_loc)
        if user.active_location:
            token['act_loc'] = str(user.active_location.id)
        elif allowed_locs:
            token['act_loc'] = allowed_locs[0]
        else:
            token['act_loc'] = ''

        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        # Add basic user details alongside access token
        data['user'] = {
            'id': self.user.id,
            'email': self.user.email,
            'role': self.user.role,
            'is_superuser': self.user.is_superuser,
            'tenantId': self.user.tenant.id if self.user.tenant else None,
            'tenantName': self.user.tenant.name if self.user.tenant else 'Global Platform HQ',
        }
        return data


class LoginRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True)


class TokenRefreshResponseSerializer(serializers.Serializer):
    access = serializers.CharField()
