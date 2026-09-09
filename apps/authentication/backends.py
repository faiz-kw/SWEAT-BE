from rest_framework_simplejwt.authentication import JWTAuthentication
from drf_spectacular.extensions import OpenApiAuthenticationExtension

class CookieJWTAuthentication(JWTAuthentication):
    """
    Extends JWTAuthentication to allow extracting the access token
    from either the Authorization header (primary) or an access_token cookie (optional fallback).
    """
    def authenticate(self, request):
        # 1. Try standard Authorization header (Bearer <token>)
        header = self.get_header(request)
        if header is not None:
            raw_token = self.get_raw_token(header)
            if raw_token is not None:
                validated_token = self.get_validated_token(raw_token)
                return self.get_user(validated_token), validated_token

        # 2. Check cookie if authorization header wasn't provided
        raw_token = request.COOKIES.get('access')
        if raw_token is not None:
            validated_token = self.get_validated_token(raw_token)
            return self.get_user(validated_token), validated_token

        return None


class CookieJWTScheme(OpenApiAuthenticationExtension):
    target_class = 'apps.authentication.backends.CookieJWTAuthentication'
    name = 'jwtAuth'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'JWT',
            'description': 'Enter JWT Bearer token format: Bearer <token>'
        }

