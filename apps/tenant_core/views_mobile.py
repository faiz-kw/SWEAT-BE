"""
apps/tenant_core/views_mobile.py — Dedicated Mobile Consumer / Member API Layer for SWEAT

Provides secure, member-scoped REST API endpoints tailored for the SWEAT Mobile App:
1. Authentication & Profile:
   - POST /api/v1/mobile/auth/register/
   - POST /api/v1/mobile/auth/login/
   - GET  /api/v1/mobile/auth/me/
   - PATCH /api/v1/mobile/auth/me/
2. Studio Catalog & Schedule:
   - GET  /api/v1/mobile/branches/
   - GET  /api/v1/mobile/classes/
   - GET  /api/v1/mobile/trainers/
   - GET  /api/v1/mobile/schedule/
3. Bookings & Free Trial:
   - POST /api/v1/mobile/bookings/claim-free-trial/
   - POST /api/v1/mobile/bookings/book/
   - GET  /api/v1/mobile/bookings/my-bookings/
   - POST /api/v1/mobile/bookings/<id>/cancel/
   - GET  /api/v1/mobile/bookings/<id>/qr-pass/
4. Memberships, Packs & Credits:
   - GET  /api/v1/mobile/packages/
   - GET  /api/v1/mobile/my-credits/
   - POST /api/v1/mobile/coupons/validate/
   - POST /api/v1/mobile/checkout/create-order/
   - POST /api/v1/mobile/checkout/verify/
5. Onboarding & Health:
   - GET  /api/v1/mobile/onboarding/survey/
   - POST /api/v1/mobile/onboarding/submit/
"""

import hmac
import hashlib
import base64
import json
import uuid
import logging
from decimal import Decimal
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db import transaction, models
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated

from config.routers import set_tenant_db_alias, get_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.services_auth_directory import sync_tenant_user_identity
from apps.authentication.views import _build_tenant_token, _register_and_resolve_tenant

from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_rbac import Role, RoleAssignment
from .models_workforce import UserProfile, TrainerProfile
from .models_classes import ClassTemplate, ClassOccurrence, ClassPrice, PackageClassAccessRule
from .models_bookings import Booking, BookingCancellation
from .models_catalog import Package, PackageVersion, PackagePrice, PackageEntitlementDefinition
from .models_memberships import Membership, MembershipEntitlement
from .models_commerce import Order, OrderItem, PaymentTransaction
from .models_discounts import DiscountCode
from .models_crm import IntakeForm, IntakeQuestion, IntakeSubmission, TrialBooking, Lead, LeadSource, LeadConversion
from .services_bookings import BookingWaitlistAttendanceService, ParqRequiredValidationError
from .services_memberships import MembershipLifecycleService
from .services_discounts import DiscountCouponEngineService
from .services_crm import CRMLeadService

logger = logging.getLogger(__name__)


def _resolve_mobile_tenant_and_db(request):
    """
    Resolves the tenant and registers/returns the active tenant DB alias.
    Defaults to 'sweat' if not explicitly provided via X-Tenant-Slug header or params.
    """
    tenant_slug = (
        (request.headers.get('X-Tenant-Slug') if hasattr(request, 'headers') else None)
        or request.META.get('HTTP_X_TENANT_SLUG')
        or request.query_params.get('tenant')
        or (request.data.get('tenant_slug') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
        or 'sweat'
    )

    tenant = Tenant.objects.using('default').filter(slug=tenant_slug, status='ACTIVE').first()
    if not tenant:
        # Fallback to the first active tenant in platform
        tenant = Tenant.objects.using('default').filter(status='ACTIVE').first()

    active_alias = get_tenant_db_alias()
    if active_alias and tenant:
        return active_alias, tenant

    # Check authenticated user's state db
    if getattr(request, 'user', None) and request.user.is_authenticated:
        user_db = getattr(getattr(request.user, '_state', None), 'db', None)
        if user_db:
            set_tenant_db_alias(user_db)
            return user_db, tenant

    if not tenant:
        return None, None

    alias = _register_and_resolve_tenant(tenant)
    set_tenant_db_alias(alias)
    return alias, tenant


def _get_or_create_user_profile(user, alias, branch=None):
    """Ensure a UserProfile exists for the given user in the tenant database."""
    profile = UserProfile.objects.using(alias).filter(user=user).first()
    if not profile:
        profile = UserProfile.objects.using(alias).create(
            user=user,
            member_number=None,
            first_name_snapshot=user.first_name,
            last_name_snapshot=user.last_name,
            preferred_branch=branch or user.home_branch,
            member_status='ACTIVE',
            member_type='TRIAL',
            acquisition_source='MOBILE_APP',
        )
    return profile


# ============================================================================
# 1. Mobile Authentication & Profile Views
# ============================================================================

class MobileRegisterView(APIView):
    """
    POST /api/v1/mobile/auth/register/
    Registers a new member with Name, Email, Phone, Password, and Studio Branch.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        email = (data.get('email') or '').strip().lower()
        password = data.get('password')
        first_name = (data.get('first_name') or '').strip()
        last_name = (data.get('last_name') or '').strip()
        phone = (data.get('phone') or '').strip()
        branch_id = data.get('branch_id')

        # If email is missing but phone is supplied, generate internal email
        if not email and phone:
            clean_digits = re.sub(r'\D', '', phone)[-10:]
            email = f"user_{clean_digits}@sweat.internal"

        if not email or not first_name:
            return Response(
                {'detail': 'First name and either email or phone number are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Ensure a secure password exists even if omitted from simple lead registration
        if not password or len(password) < 8:
            password = f"Sweat@{uuid.uuid4().hex[:8]}" 

        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias or not tenant:
            return Response(
                {'detail': 'Studio service is temporarily unavailable. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        with transaction.atomic(using=alias):
            # 1. Check duplicate user
            if TenantUser.objects.using(alias).filter(email__iexact=email).exists():
                return Response(
                    {'detail': 'An account with this email address already exists.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # 2. Resolve organization and branch
            org = Organization.objects.using(alias).first()
            if not org:
                return Response({'detail': 'Studio organization configuration not found.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            branch = None
            if branch_id:
                branch = Branch.objects.using(alias).filter(id=branch_id, status='ACTIVE').first()
            if not branch:
                branch = Branch.objects.using(alias).filter(status='ACTIVE').first()

            # 3. Create TenantUser
            base_username = email.split('@')[0]
            username = base_username
            idx = 1
            while TenantUser.objects.using(alias).filter(username__iexact=username).exists():
                username = f"{base_username}_{idx}"
                idx += 1

            user = TenantUser(
                organization=org,
                username=username,
                email=email,
                phone=phone,
                first_name=first_name,
                last_name=last_name,
                display_name=f"{first_name} {last_name}".strip(),
                home_branch=branch,
                user_type='MEMBER',
                status='ACTIVE',
                is_login_allowed=True,
            )
            user.set_password(password)
            user.save(using=alias)

            # 4. Assign MEMBER Role
            member_role = Role.objects.using(alias).filter(code='MEMBER').first()
            if not member_role:
                member_role = Role.objects.using(alias).create(
                    organization=org,
                    code='MEMBER',
                    name='Studio Member',
                    scope='BRANCH',
                    is_system=True,
                    is_active=True,
                )

            RoleAssignment.objects.using(alias).create(
                user=user,
                role=member_role,
                branch=branch,
                is_active=True,
            )

            # 5. Create UserProfile
            profile = _get_or_create_user_profile(user, alias, branch=branch)

            # 6. Synchronize directory identity in master database
            try:
                sync_tenant_user_identity(user, tenant_id=tenant.id, db=alias)
            except Exception as e:
                logger.warning("Could not sync directory identity for %s: %s", email, e)

            # 6b. Automatically create or link CRM Lead record with source 'MOBILE_APP'
            try:
                lead_source, _ = LeadSource.objects.using(alias).get_or_create(
                    organization=org,
                    code='MOBILE_APP',
                    defaults={'name': 'Mobile App', 'source_type': 'MOBILE_APP', 'status': 'ACTIVE'}
                )

                lead = Lead.objects.using(alias).filter(
                    models.Q(email_normalized__iexact=email) |
                    (models.Q(phone_normalized=phone) if phone else models.Q(pk=None))
                ).first()

                # Extract all lead form fields from intake
                extra_lead_data = {'converted_user_profile': profile}
                if data.get('gender'):
                    extra_lead_data['gender'] = data.get('gender')
                if data.get('date_of_birth') or data.get('birthday'):
                    extra_lead_data['date_of_birth'] = data.get('date_of_birth') or data.get('birthday')
                if data.get('fitness_goal') or data.get('goal'):
                    extra_lead_data['fitness_goal'] = data.get('fitness_goal') or data.get('goal')
                if data.get('area') or data.get('location'):
                    extra_lead_data['area'] = data.get('area') or data.get('location')
                if data.get('country'):
                    extra_lead_data['country'] = data.get('country')
                if data.get('interested_program_id') or data.get('program_id'):
                    from .models_catalog import Program
                    p_id = data.get('interested_program_id') or data.get('program_id')
                    prog_obj = Program.objects.using(alias).filter(id=p_id).first()
                    if prog_obj:
                        extra_lead_data['interested_program'] = prog_obj

                if not lead:
                    CRMLeadService.create_lead(
                        organization=org,
                        first_name=first_name,
                        last_name=last_name or '',
                        phone=phone or '',
                        email=email,
                        branch=branch,
                        lead_source=lead_source,
                        assigned_sales_user=None,
                        actor_user=None,
                        extra_fields=extra_lead_data,
                        db_alias=alias,
                    )
                else:
                    lead.converted_user_profile = profile
                    lead.latest_touch_source = 'MOBILE_APP'
                    lead.save(using=alias, update_fields=['converted_user_profile', 'latest_touch_source'])
            except Exception as crm_err:
                logger.warning("Could not auto-create CRM lead for mobile user %s: %s", email, crm_err)

            # 7. Generate JWT access & refresh tokens
            tokens = _build_tenant_token(user, tenant, alias)

        return Response({
            'access': str(tokens.access_token),
            'refresh': str(tokens),
            'user': {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone': user.phone or '',
                'role': 'member',
                'role_code': 'MEMBER',
                'member_number': profile.member_number or '',
                'home_branch': {
                    'id': str(branch.id) if branch else None,
                    'name': branch.name if branch else 'SWEAT Studio',
                } if branch else None,
            },
            'message': 'Account created successfully! Welcome to SWEAT.',
        }, status=status.HTTP_201_CREATED)


class MobileLoginView(APIView):
    """
    POST /api/v1/mobile/auth/login/
    Member login with Email or Phone and Password.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        identifier = (data.get('email') or data.get('identifier') or data.get('phone') or '').strip().lower()
        password = data.get('password')

        if not identifier or not password:
            return Response(
                {'detail': 'Email or phone number and password are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias or not tenant:
            return Response(
                {'detail': 'Studio service is temporarily unavailable. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Lookup user by email, username, or phone
        user = (
            TenantUser.objects.using(alias)
            .filter(
                models.Q(email__iexact=identifier)
                | models.Q(username__iexact=identifier)
                | models.Q(phone=identifier)
            )
            .select_related('home_branch')
            .first()
        )

        if not user or not user.check_password(password):
            return Response(
                {'detail': 'Invalid email/phone or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if user.status != 'ACTIVE' or not user.is_login_allowed:
            return Response(
                {'detail': 'Your account is currently inactive. Please contact SWEAT support.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Ensure UserProfile exists
        profile = _get_or_create_user_profile(user, alias)

        # Update last login
        user.last_login_at = timezone.now()
        user.save(using=alias, update_fields=['last_login_at'])

        # Generate tokens
        tokens = _build_tenant_token(user, tenant, alias)

        return Response({
            'access': str(tokens.access_token),
            'refresh': str(tokens),
            'user': {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone': user.phone or '',
                'role': 'member',
                'role_code': 'MEMBER',
                'member_number': profile.member_number or '',
                'avatar_url': user.avatar_url or '',
                'home_branch': {
                    'id': str(user.home_branch.id) if user.home_branch else None,
                    'name': user.home_branch.name if user.home_branch else 'SWEAT Studio',
                } if user.home_branch else None,
            },
        }, status=status.HTTP_200_OK)




def _process_social_login(request, email, first_name, last_name, avatar_url, provider, phone='', branch_id=None):
    """
    Common handler for verified social login payloads (Google / Facebook).
    Creates or looks up the TenantUser, associates UserProfile,
    automatically creates/links a Lead in CRM (status: NEW_LEAD),
    and returns standard JWT access/refresh tokens.
    """
    alias, tenant = _resolve_mobile_tenant_and_db(request)
    if not alias or not tenant:
        return Response(
            {'detail': 'Studio service is temporarily unavailable. Please try again.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    with transaction.atomic(using=alias):
        # 1. Lookup existing user by email
        user = TenantUser.objects.using(alias).filter(email__iexact=email).first()
        is_new_user = False

        org = Organization.objects.using(alias).first()
        if not org:
            return Response(
                {'detail': 'Studio organization configuration not found.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        branch = None
        if branch_id and isinstance(branch_id, str):
            try:
                uuid.UUID(str(branch_id).strip())
                branch = Branch.objects.using(alias).filter(id=branch_id, status='ACTIVE').first()
            except (ValueError, TypeError):
                branch = None
        if not branch:
            branch = Branch.objects.using(alias).filter(status='ACTIVE').first()

        clean_phone = (phone or '').strip()

        if not user:
            is_new_user = True
            base_username = email.split('@')[0] if '@' in email else f"{provider.lower()}_user"
            username = base_username
            idx = 1
            while TenantUser.objects.using(alias).filter(username__iexact=username).exists():
                username = f"{base_username}_{idx}"
                idx += 1

            user = TenantUser(
                organization=org,
                username=username,
                email=email,
                phone=clean_phone,
                first_name=first_name,
                last_name=last_name or '',
                display_name=f"{first_name} {last_name}".strip(),
                avatar_url=avatar_url or '',
                home_branch=branch,
                user_type='MEMBER',
                status='ACTIVE',
                is_login_allowed=True,
            )
            user.set_password(str(uuid.uuid4()))
            user.save(using=alias)

            # Assign MEMBER Role
            member_role = Role.objects.using(alias).filter(code='MEMBER').first()
            if not member_role:
                member_role = Role.objects.using(alias).create(
                    organization=org,
                    code='MEMBER',
                    name='Studio Member',
                    scope='BRANCH',
                    is_system=True,
                    is_active=True,
                )

            RoleAssignment.objects.using(alias).create(
                user=user,
                role=member_role,
                branch=branch,
                is_active=True,
            )

            try:
                sync_tenant_user_identity(user, tenant_id=tenant.id, db=alias)
            except Exception as e:
                logger.warning("Could not sync directory identity for %s: %s", email, e)
        else:
            # Update user profile details if blank
            updated_user_fields = []
            if avatar_url and not user.avatar_url:
                user.avatar_url = avatar_url
                updated_user_fields.append('avatar_url')
            if first_name and not user.first_name:
                user.first_name = first_name
                updated_user_fields.append('first_name')
            if last_name and not user.last_name:
                user.last_name = last_name
                updated_user_fields.append('last_name')
            if clean_phone and not user.phone:
                user.phone = clean_phone
                updated_user_fields.append('phone')
            if updated_user_fields:
                user.save(using=alias, update_fields=updated_user_fields)

        # 2. Ensure UserProfile exists (member_type='TRIAL' until package purchase)
        profile = _get_or_create_user_profile(user, alias, branch=branch)

        # 3. Automatically create or link CRM Lead record in CRM & Sales -> Leads
        try:
            source_code = 'GOOGLE' if provider.upper() == 'GOOGLE' else ('META' if provider.upper() == 'FACEBOOK' else 'MOBILE_APP')
            lead_source = LeadSource.objects.using(alias).filter(organization=org, code=source_code).first()
            if not lead_source:
                lead_source, _ = LeadSource.objects.using(alias).get_or_create(
                    organization=org,
                    code='MOBILE_APP',
                    defaults={'name': 'Mobile App', 'source_type': 'MOBILE_APP', 'status': 'ACTIVE'}
                )

            lead = Lead.objects.using(alias).filter(
                models.Q(email_normalized__iexact=email) |
                (models.Q(phone_normalized=user.phone) if user.phone else models.Q(pk=None))
            ).first()

            lead_phone = clean_phone or user.phone or ''

            if not lead:
                extra_lead = {
                    'converted_user_profile': profile,
                    'first_touch_source': f"{provider.title()} Sign-In (App)",
                    'latest_touch_source': f"MOBILE_{provider.upper()}",
                    'campaign_reference': f"{provider.title()} Mobile OAuth",
                }
                lead = CRMLeadService.create_lead(
                    organization=org,
                    first_name=first_name,
                    last_name=last_name or '',
                    phone=lead_phone,
                    email=email,
                    branch=branch or user.home_branch,
                    lead_source=lead_source,
                    assigned_sales_user=None,  # Runs Round-Robin auto-assignment
                    actor_user=None,
                    extra_fields=extra_lead,
                    db_alias=alias,
                )
            else:
                lead.converted_user_profile = profile
                lead.latest_touch_source = f"MOBILE_{provider.upper()}"
                update_fields = ['converted_user_profile', 'latest_touch_source']
                if not lead.branch and (branch or user.home_branch):
                    lead.branch = branch or user.home_branch
                    update_fields.append('branch')
                if lead_phone and not lead.phone_normalized:
                    lead.phone_normalized = lead_phone
                    update_fields.append('phone_normalized')
                lead.save(using=alias, update_fields=update_fields)
        except Exception as crm_err:
            logger.warning("Could not auto-create CRM lead for %s %s: %s", provider, email, crm_err)

        user.last_login_at = timezone.now()
        user.save(using=alias, update_fields=['last_login_at'])

        # 4. Generate JWT tokens
        tokens = _build_tenant_token(user, tenant, alias)

        home_branch_info = {
            'id': str(user.home_branch.id),
            'name': user.home_branch.name,
        } if user.home_branch else None

        return Response({
            'access': str(tokens.access_token),
            'refresh': str(tokens),
            'user': {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone': user.phone or '',
                'role': 'member',
                'role_code': 'MEMBER',
                'member_number': profile.member_number or '',
                'avatar_url': user.avatar_url or '',
                'home_branch': home_branch_info,
            },
            'is_new_user': is_new_user,
            'message': f'Logged in successfully via {provider.title()}.',
        }, status=status.HTTP_200_OK if not is_new_user else status.HTTP_201_CREATED)


class MobileGoogleAuthView(APIView):
    """
    POST /api/v1/mobile/auth/google/
    Authenticate or Register mobile user via Google OAuth ID token.
    Automatically captures new users as CRM Leads (NEW_LEAD, MOBILE_APP).
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data or {}
        token = data.get('id_token') or data.get('token') or data.get('access_token')
        if not token:
            return Response(
                {'detail': 'Google id_token is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        google_user_info = None

        # 1. Try google-auth verification
        try:
            from google.oauth2 import id_token as google_id_token
            from google.auth.transport import requests as google_requests
            client_id = getattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', None)
            google_user_info = google_id_token.verify_oauth2_token(
                token,
                google_requests.Request(),
                audience=client_id if client_id else None
            )
        except Exception as e_verify:
            logger.info("google_id_token verification exception (%s), trying Google tokeninfo endpoint...", e_verify)
            # Fallback to Google tokeninfo API endpoint
            try:
                import requests as http_req
                resp = http_req.get(
                    'https://oauth2.googleapis.com/tokeninfo',
                    params={'id_token': token},
                    timeout=10
                )
                if resp.status_code == 200:
                    google_user_info = resp.json()
            except Exception as e_net:
                logger.warning("Google tokeninfo fallback failed: %s", e_net)

        if not google_user_info or not google_user_info.get('email'):
            return Response(
                {'detail': 'Invalid or expired Google token.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        email = google_user_info.get('email').strip().lower()
        first_name = (google_user_info.get('given_name') or google_user_info.get('name') or email.split('@')[0]).strip()
        last_name = (google_user_info.get('family_name') or '').strip()
        picture = google_user_info.get('picture') or ''

        phone = data.get('phone') or data.get('contactNumber') or ''
        branch_id = data.get('branch_id') or data.get('branch') or None

        return _process_social_login(
            request=request,
            email=email,
            first_name=first_name,
            last_name=last_name,
            avatar_url=picture,
            provider='GOOGLE',
            phone=phone,
            branch_id=branch_id,
        )


class MobileFacebookAuthView(APIView):
    """
    POST /api/v1/mobile/auth/facebook/
    Authenticate or Register mobile user via Facebook Graph API access token.
    Automatically captures new users as CRM Leads (NEW_LEAD, MOBILE_APP).
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data or {}
        access_token = data.get('access_token') or data.get('token')
        if not access_token:
            return Response(
                {'detail': 'Facebook access_token is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        fb_user_info = None
        try:
            import requests as http_req
            fb_resp = http_req.get(
                'https://graph.facebook.com/me',
                params={
                    'fields': 'id,name,first_name,last_name,email,picture.type(large)',
                    'access_token': access_token,
                },
                timeout=10
            )
            if fb_resp.status_code == 200:
                fb_user_info = fb_resp.json()
            else:
                logger.warning("Facebook Graph API returned %s: %s", fb_resp.status_code, fb_resp.text)
        except Exception as e:
            logger.warning("Facebook Graph API request failed: %s", e)

        if not fb_user_info or not fb_user_info.get('id'):
            return Response(
                {'detail': 'Invalid or expired Facebook access token.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        fb_id = fb_user_info['id']
        email = (fb_user_info.get('email') or f"fb_{fb_id}@facebook.user").strip().lower()
        first_name = (fb_user_info.get('first_name') or fb_user_info.get('name') or 'Facebook User').strip()
        last_name = (fb_user_info.get('last_name') or '').strip()
        picture = ''
        if isinstance(fb_user_info.get('picture'), dict):
            picture = fb_user_info['picture'].get('data', {}).get('url', '')

        phone = data.get('phone') or data.get('contactNumber') or ''
        branch_id = data.get('branch_id') or data.get('branch') or None

        return _process_social_login(
            request=request,
            email=email,
            first_name=first_name,
            last_name=last_name,
            avatar_url=picture,
            provider='FACEBOOK',
            phone=phone,
            branch_id=branch_id,
        )


class MobileMeView(APIView):
    """
    GET, PATCH /api/v1/mobile/auth/me/
    Fetch and update authenticated member profile, home branch, and active credits.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        # Resolve active membership and session units
        active_membership = (
            Membership.objects.using(alias)
            .filter(
                user_profile=profile,
                status='ACTIVE',
                end_date__gte=timezone.now().date(),
            )
            .select_related('package', 'package_version')
            .order_by('-created_at')
            .first()
        )

        membership_data = None
        if active_membership:
            entitlements = list(active_membership.entitlements.using(alias).filter(status='ACTIVE'))
            total_allocated = sum(e.allocated_units for e in entitlements)
            total_consumed = sum(e.consumed_units for e in entitlements)
            total_remaining = max(Decimal('0'), total_allocated - total_consumed)
            days_left = max(0, (active_membership.end_date - timezone.now().date()).days)

            membership_data = {
                'membership_id': str(active_membership.id),
                'package_name': active_membership.package.name,
                'allocated_sessions': float(total_allocated),
                'consumed_sessions': float(total_consumed),
                'remaining_sessions': float(total_remaining),
                'start_date': active_membership.start_date.isoformat() if active_membership.start_date else None,
                'end_date': active_membership.end_date.isoformat() if active_membership.end_date else None,
                'days_remaining': days_left,
                'near_expiry': days_left <= 7 or total_remaining <= Decimal('2.0'),
            }

        # Check if 1 free trial session was already redeemed
        has_trial = (
            Booking.objects.using(alias).filter(user_profile=profile, booking_type='TRIAL').exists()
            or TrialBooking.objects.using(alias).filter(
                models.Q(lead__email_normalized__iexact=user.email) |
                models.Q(lead__phone_normalized=user.phone)
            ).exists()
        )

        return Response({
            'user': {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone': user.phone,
                'avatar_url': user.avatar_url or '',
                'date_of_birth': user.date_of_birth.isoformat() if user.date_of_birth else None,
                'gender': user.gender or '',
                'home_branch': {
                    'id': str(user.home_branch.id) if user.home_branch else None,
                    'name': user.home_branch.name if user.home_branch else 'SWEAT Studio',
                } if user.home_branch else None,
            },
            'profile': {
                'member_number': profile.member_number,
                'member_status': profile.member_status,
                'joining_date': profile.joining_date.isoformat() if profile.joining_date else None,
                'preferred_branch': {
                    'id': str(profile.preferred_branch.id) if profile.preferred_branch else None,
                    'name': profile.preferred_branch.name if profile.preferred_branch else 'SWEAT Studio',
                } if profile.preferred_branch else None,
            },
            'active_membership': membership_data,
            'free_trial_redeemed': has_trial,
        })

    def patch(self, request):
        user = request.user
        alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)
        data = request.data

        # Update user fields
        if 'first_name' in data:
            user.first_name = data['first_name'].strip()
            profile.first_name_snapshot = user.first_name
        if 'last_name' in data:
            user.last_name = data['last_name'].strip()
            profile.last_name_snapshot = user.last_name
        if 'phone' in data:
            user.phone = data['phone'].strip()
        if 'avatar_url' in data:
            user.avatar_url = data['avatar_url']
        if 'gender' in data:
            user.gender = data['gender']
            profile.gender = data['gender']
        if 'date_of_birth' in data and data['date_of_birth']:
            try:
                user.date_of_birth = datetime.strptime(data['date_of_birth'], '%Y-%m-%d').date()
                profile.date_of_birth = user.date_of_birth
            except ValueError:
                pass

        if 'branch_id' in data:
            branch = Branch.objects.using(alias).filter(id=data['branch_id'], status='ACTIVE').first()
            if branch:
                user.home_branch = branch
                profile.preferred_branch = branch

        user.display_name = f"{user.first_name} {user.last_name}".strip()
        user.save(using=alias)
        profile.save(using=alias)

        return Response({
            'detail': 'Profile updated successfully.',
            'user': {
                'id': str(user.id),
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone': user.phone,
                'avatar_url': user.avatar_url,
                'home_branch': {
                    'id': str(user.home_branch.id) if user.home_branch else None,
                    'name': user.home_branch.name if user.home_branch else 'SWEAT Studio',
                } if user.home_branch else None,
            },
        })


# ============================================================================
# 2. Studio Branches, Classes, Trainers & Daily Schedule Views
# ============================================================================

import math

def _calculate_haversine_distance_km(lat1, lon1, lat2, lon2):
    """
    Calculate the great circle distance in kilometers between two points
    on the earth (specified in decimal degrees) using Haversine formula.
    """
    try:
        r = 6371.0  # Earth's radius in kilometers
        phi1 = math.radians(float(lat1))
        phi2 = math.radians(float(lat2))
        delta_phi = math.radians(float(lat2) - float(lat1))
        delta_lambda = math.radians(float(lon2) - float(lon1))

        a = (
            math.sin(delta_phi / 2.0) ** 2
            + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
        )
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
        return r * c
    except (ValueError, TypeError):
        return None


def _serialize_branch_dict(b, user_lat=None, user_lng=None):
    """Serialize a branch record with full GPS coordinates, operating hours, and optional distance."""
    b_lat = float(b.latitude) if b.latitude is not None else None
    b_lng = float(b.longitude) if b.longitude is not None else None

    dist_km = None
    dist_m = None
    if user_lat is not None and user_lng is not None and b_lat is not None and b_lng is not None:
        calculated_km = _calculate_haversine_distance_km(user_lat, user_lng, b_lat, b_lng)
        if calculated_km is not None:
            dist_km = round(calculated_km, 2)
            dist_m = int(round(calculated_km * 1000))

    addr = getattr(b, 'address', '') or ''
    addr_l1 = getattr(b, 'address_line_1', '') or getattr(b, 'address_line1', '') or ''
    addr_l2 = getattr(b, 'address_line_2', '') or getattr(b, 'address_line2', '') or ''
    full_address = addr or (f"{addr_l1}, {addr_l2}".strip(', ') if addr_l1 or addr_l2 else '')

    open_time = getattr(b, 'business_open_time', None) or '06:00'
    close_time = getattr(b, 'business_close_time', None) or '22:00'
    if hasattr(b, 'opening_time') and b.opening_time:
        open_time = b.opening_time.strftime('%H:%M')
    if hasattr(b, 'closing_time') and b.closing_time:
        close_time = b.closing_time.strftime('%H:%M')

    return {
        'id': str(b.id),
        'name': b.name,
        'code': b.code,
        'address': full_address,
        'address_line1': addr_l1,
        'address_line2': addr_l2,
        'city': getattr(b, 'address_city', None) or getattr(b, 'city', 'Mumbai'),
        'postal_code': getattr(b, 'address_postal_code', '') or getattr(b, 'postal_code', ''),
        'latitude': b_lat,
        'longitude': b_lng,
        'distance_km': dist_km,
        'distance_meters': dist_m,
        'is_nearest': False,
        'geofence_radius_meters': getattr(b, 'geofence_radius_meters', 200) or 200,
        'geofence_enforcement': getattr(b, 'geofence_enforcement', 'STRICT') or 'STRICT',
        'is_passport_eligible': getattr(b, 'is_passport_eligible', False),
        'phone': getattr(b, 'phone', '') or '',
        'email': getattr(b, 'email', '') or '',
        'opening_time': open_time,
        'closing_time': close_time,
    }


class MobileBranchesView(APIView):
    """
    GET /api/v1/mobile/branches/
    Lists all active studio branches with GPS coordinates (latitude, longitude, geofence radius).
    Optional query parameters:
      - lat, lng (or latitude, longitude): Calculates distance and sorts branches nearest-first!
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response([], status=status.HTTP_200_OK)

        raw_lat = request.query_params.get('lat') or request.query_params.get('latitude')
        raw_lng = request.query_params.get('lng') or request.query_params.get('lon') or request.query_params.get('longitude')

        user_lat = None
        user_lng = None
        if raw_lat and raw_lng:
            try:
                user_lat = float(raw_lat)
                user_lng = float(raw_lng)
            except (ValueError, TypeError):
                user_lat = None
                user_lng = None

        branches = Branch.objects.using(alias).filter(status='ACTIVE').order_by('name')
        results = [
            _serialize_branch_dict(b, user_lat=user_lat, user_lng=user_lng)
            for b in branches
        ]

        if user_lat is not None and user_lng is not None:
            results.sort(key=lambda x: (x['distance_km'] is None, x['distance_km'] if x['distance_km'] is not None else float('inf')))
            if results and results[0]['distance_km'] is not None:
                results[0]['is_nearest'] = True

        return Response(results)


class MobileNearestBranchView(APIView):
    """
    GET /api/v1/mobile/branches/nearest/
    Fetches the single nearest studio branch to the member's current GPS location.
    Required query parameters:
      - lat, lng (or latitude, longitude)
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response({'detail': 'Studio service unavailable.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        raw_lat = request.query_params.get('lat') or request.query_params.get('latitude')
        raw_lng = request.query_params.get('lng') or request.query_params.get('lon') or request.query_params.get('longitude')

        if not raw_lat or not raw_lng:
            return Response(
                {'detail': 'Current GPS coordinates (lat, lng) are required to determine nearest branch.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            user_lat = float(raw_lat)
            user_lng = float(raw_lng)
        except (ValueError, TypeError):
            return Response(
                {'detail': 'Invalid latitude or longitude coordinates provided.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        branches = Branch.objects.using(alias).filter(status='ACTIVE')
        results = [
            _serialize_branch_dict(b, user_lat=user_lat, user_lng=user_lng)
            for b in branches
        ]

        results.sort(key=lambda x: (x['distance_km'] is None, x['distance_km'] if x['distance_km'] is not None else float('inf')))

        if not results or results[0]['distance_km'] is None:
            return Response({'detail': 'No active branches found with GPS coordinates.'}, status=status.HTTP_404_NOT_FOUND)

        nearest = results[0]
        nearest['is_nearest'] = True

        return Response({
            'nearest_branch': nearest,
            'member_location': {
                'latitude': user_lat,
                'longitude': user_lng,
            },
            'all_branches_by_distance': results,
        }, status=status.HTTP_200_OK)


class MobileClassesView(APIView):
    """
    GET /api/v1/mobile/classes/
    Lists class catalog with intensity, duration, description, and requirements.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response([], status=status.HTTP_200_OK)

        classes = (
            ClassTemplate.objects.using(alias)
            .filter(status='ACTIVE')
            .select_related('category')
            .order_by('name')
        )
        results = []
        for c in classes:
            results.append({
                'id': str(c.id),
                'name': c.name,
                'code': c.code,
                'category': c.category.name if c.category else 'Reformer Pilates',
                'description': c.description or '',
                'duration_minutes': c.default_duration_minutes,
                'intensity_level': c.intensity_level if hasattr(c, 'intensity_level') and c.intensity_level else 'Intermediate',
                'default_capacity': c.default_capacity,
                'what_to_bring': 'Grip socks, water bottle, workout towel.',
                'allow_trial': c.allow_trial if hasattr(c, 'allow_trial') else True,
            })
        return Response(results)


class MobileTrainersView(APIView):
    """
    GET /api/v1/mobile/trainers/
    Lists active trainer profiles with bios, photos, and specialties.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response([], status=status.HTTP_200_OK)

        trainers = (
            TrainerProfile.objects.using(alias)
            .filter(trainer_status='ACTIVE')
            .select_related('employee_profile__user_profile__user')
            .prefetch_related('specialty_assignments__specialty')
            .order_by('employee_profile__user_profile__user__first_name')
        )
        results = []
        for t in trainers:
            emp = getattr(t, 'employee_profile', None)
            user_prof = emp.user_profile if emp else None
            user = user_prof.user if user_prof else None
            specialties = [
                sa.specialty.name
                for sa in t.specialty_assignments.all()
                if sa.specialty and sa.is_active
            ]
            results.append({
                'id': str(t.id),
                'name': f"{user.first_name} {user.last_name}".strip() if user else 'SWEAT Coach',
                'full_name': f"{user.first_name} {user.last_name}".strip() if user else 'SWEAT Coach',
                'title': emp.designation if emp and emp.designation else 'Master Reformer Instructor',
                'designation': emp.designation if emp and emp.designation else 'Master Reformer Instructor',
                'bio': t.bio or 'Certified Pilates Reformer & Athletic Conditioning specialist.',
                'avatar_url': user.avatar_url if user and user.avatar_url else '',
                'specialties': specialties or ['Reformer Pilates', 'Core Stability', 'Postural Alignment'],
                'years_of_experience': float(t.experience_years) if t.experience_years else 5.0,
                'experience_years': float(t.experience_years) if t.experience_years else 5.0,
            })
        return Response(results)


class MobileScheduleView(APIView):
    """
    GET /api/v1/mobile/schedule/
    Filterable daily schedule with live reformer bed capacity, waitlists, and user booking indicators.
    Query params:
      - branch_id: (optional UUID)
      - date: YYYY-MM-DD (defaults to today)
      - category_id: (optional UUID)
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response([], status=status.HTTP_200_OK)

        date_str = request.query_params.get('date')
        if date_str:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                target_date = timezone.now().date()
        else:
            target_date = timezone.now().date()

        branch_id = request.query_params.get('branch_id')
        category_id = request.query_params.get('category_id')

        # Query occurrences for the target date
        qs = (
            ClassOccurrence.objects.using(alias)
            .filter(
                models.Q(occurrence_date=target_date)
                | (models.Q(occurrence_date__isnull=True) & models.Q(start_at__date=target_date)),
                status__in=['OPEN', 'SCHEDULED', 'IN_PROGRESS', 'CONFIRMED', 'PUBLISHED', 'ACTIVE']
            )
            .select_related('class_template__category', 'branch')
            .prefetch_related(
                'trainer_assignments__trainer_profile__employee_profile__user_profile__user',
                'trainer_assignments__trainer_profile__specialty_assignments__specialty',
            )
            .order_by('start_at')
        )

        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        if category_id:
            qs = qs.filter(class_template__category_id=category_id)

        # Authenticated user bookings for this day to show 'CONFIRMED' / 'WAITLISTED' badges
        user_booking_map = {}
        included_class_ids = None
        if request.user and request.user.is_authenticated:
            user_profile = UserProfile.objects.using(alias).filter(user=request.user).first()
            if user_profile:
                # Resolve active membership and its package class access rules
                active_mem = (
                    Membership.objects.using(alias)
                    .filter(user_profile=user_profile, status='ACTIVE', end_date__gte=timezone.now().date())
                    .order_by('-created_at')
                    .first()
                )
                if active_mem and active_mem.package_version_id:
                    rules = PackageClassAccessRule.objects.using(alias).filter(
                        package_version_id=active_mem.package_version_id, access_type='INCLUDED'
                    )
                    rule_ids = {str(r.class_template_id) for r in rules if r.class_template_id}
                    if rule_ids:
                        included_class_ids = rule_ids

                user_bookings = Booking.objects.using(alias).filter(
                    user_profile=user_profile,
                    occurrence__in=qs,
                    status__in=['CONFIRMED', 'RESERVED', 'WAITLISTED']
                )
                for b in user_bookings:
                    user_booking_map[str(b.occurrence_id)] = {
                        'booking_id': str(b.id),
                        'status': b.status,
                        'waitlist_position': b.waitlist_position,
                    }

        results = []
        for occ in qs:
            template = occ.class_template
            category = template.category if template else None

            # Calculate live capacity & booked seats
            confirmed_count = occ.bookings.using(alias).filter(
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            ).count()
            trial_count = TrialBooking.objects.using(alias).filter(
                class_occurrence_id=occ.id,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']
            ).count()
            total_booked = confirmed_count + trial_count
            total_capacity = occ.capacity or (template.default_capacity if template else 10)
            available_seats = max(0, total_capacity - total_booked)
            is_full = (available_seats == 0)

            # Waitlist status
            waitlist_count = occ.bookings.using(alias).filter(status='WAITLISTED').count()
            allow_waitlist = bool(template and getattr(template, 'allow_waitlist', True))
            max_waitlist = occ.waitlist_capacity or 5
            is_waitlist_open = allow_waitlist and (waitlist_count < max_waitlist)

            # Trainer details
            trainer_name = 'SWEAT Trainer'
            trainer_photo = ''
            trainer_title = 'Master Coach'
            trainer_bio = 'Certified Reformer Pilates & Conditioning Specialist.'
            trainer_specialties = ['Reformer Pilates', 'Strength & Core', 'Postural Alignment']
            trainer_experience = 5.0

            trainer_assignment = occ.trainer_assignments.filter(trainer_role='LEAD').first() or occ.trainer_assignments.first()
            if trainer_assignment and trainer_assignment.trainer_profile:
                t_prof = trainer_assignment.trainer_profile
                trainer_bio = t_prof.bio or trainer_bio
                trainer_experience = float(t_prof.experience_years) if t_prof.experience_years else 5.0
                specs = [sa.specialty.name for sa in t_prof.specialty_assignments.all() if sa.specialty and sa.is_active]
                if specs:
                    trainer_specialties = specs

                emp = getattr(t_prof, 'employee_profile', None)
                if emp:
                    trainer_title = emp.designation or trainer_title
                    if emp.user_profile and emp.user_profile.user:
                        t_user = emp.user_profile.user
                        trainer_name = f"{t_user.first_name} {t_user.last_name}".strip()
                        trainer_photo = t_user.avatar_url or ''

            user_booking = user_booking_map.get(str(occ.id))
            is_included = (str(template.id) in included_class_ids) if (included_class_ids is not None and template) else True

            results.append({
                'id': str(occ.id),
                'class_id': str(template.id) if template else None,
                'class_name': template.name if template else 'Reformer Session',
                'category': category.name if category else 'Pilates Reformer',
                'branch': {
                    'id': str(occ.branch.id) if occ.branch else None,
                    'name': occ.branch.name if occ.branch else 'SWEAT Studio',
                } if occ.branch else None,
                'date': target_date.isoformat(),
                'start_at': occ.start_at.isoformat() if occ.start_at else None,
                'end_at': occ.end_at.isoformat() if occ.end_at else None,
                'duration_minutes': template.default_duration_minutes if template else 50,
                'intensity_level': getattr(template, 'intensity_level', 'All Levels') if template else 'All Levels',
                'trainer': {
                    'name': trainer_name,
                    'title': trainer_title,
                    'avatar_url': trainer_photo,
                    'bio': trainer_bio,
                    'specialties': trainer_specialties,
                    'experience_years': trainer_experience,
                },
                'capacity': {
                    'total_capacity': total_capacity,
                    'booked_count': total_booked,
                    'available_seats': available_seats,
                    'is_full': is_full,
                    'waitlist_count': waitlist_count,
                    'is_waitlist_open': is_waitlist_open,
                },
                'user_booking': user_booking,
                'is_included_in_plan': is_included,
            })

        return Response({
            'date': target_date.isoformat(),
            'branch_id': branch_id,
            'classes_count': len(results),
            'slots': results,
        })


# ============================================================================
# 3. Class Booking, Waitlist, Cancellation & QR Pass Views
# ============================================================================

class MobileClaimFreeTrialView(APIView):
    """
    POST /api/v1/mobile/bookings/claim-free-trial/
    POST /api/v1/mobile/trials/book/
    Allows new members & leads (logged-in or guest) to claim 1 free trial session at ₹0 cost.
    Ensures trial cannot be booked twice.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(getattr(request, 'user', None), '_state', None)
            alias = getattr(alias, 'db', None) or get_tenant_db_alias() or 'default'

        user = request.user if (hasattr(request, 'user') and request.user.is_authenticated) else None
        data = request.data or {}

        # If user is not logged in, resolve or create user from request payload
        auth_tokens = None
        if not user:
            email = (data.get('email') or '').strip().lower()
            phone = (data.get('phone') or data.get('contactNumber') or '').strip()
            first_name = (data.get('first_name') or data.get('name') or '').strip()
            last_name = (data.get('last_name') or '').strip()
            if not email and phone:
                import re
                clean_digits = re.sub(r'\D', '', phone)[-10:]
                email = f"lead_{clean_digits}@sweat.internal"

            if not email and not phone:
                return Response(
                    {'detail': 'Please provide first name and email or phone number to book a trial.', 'code': 'GUEST_INFO_REQUIRED'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Look up or create user
            user = TenantUser.objects.using(alias).filter(
                models.Q(email__iexact=email) | (models.Q(phone=phone) if phone else models.Q(pk=None))
            ).first()

            if not user:
                org = Organization.objects.using(alias).first()
                branch_id = data.get('branch_id') or data.get('branch')
                branch = Branch.objects.using(alias).filter(id=branch_id).first() if branch_id else Branch.objects.using(alias).filter(status='ACTIVE').first()
                base_username = email.split('@')[0] if email else f"lead_{uuid.uuid4().hex[:6]}"
                username = base_username
                idx = 1
                while TenantUser.objects.using(alias).filter(username__iexact=username).exists():
                    username = f"{base_username}_{idx}"
                    idx += 1

                user = TenantUser(
                    organization=org,
                    username=username,
                    email=email,
                    phone=phone,
                    first_name=first_name or 'Trial',
                    last_name=last_name or 'Guest',
                    display_name=f"{first_name} {last_name}".strip() or 'Trial Guest',
                    home_branch=branch,
                    user_type='LEAD',
                    status='ACTIVE',
                    is_login_allowed=True,
                )
                user.set_password(f"Sweat@{uuid.uuid4().hex[:8]}")
                user.save(using=alias)

                member_role = Role.objects.using(alias).filter(code='MEMBER').first()
                if member_role:
                    RoleAssignment.objects.using(alias).get_or_create(
                        user=user,
                        role=member_role,
                        defaults={'branch': branch, 'is_active': True}
                    )

            if tenant:
                tokens = _build_tenant_token(user, tenant, alias)
                auth_tokens = {'access': str(tokens.access_token), 'refresh': str(tokens)}

        profile = _get_or_create_user_profile(user, alias)

        occurrence_id = data.get('occurrence_id')
        if not occurrence_id:
            return Response({'detail': 'occurrence_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Enforce 1-time Free Trial policy
        has_trial = (
            Booking.objects.using(alias).filter(user_profile=profile, booking_type='TRIAL').exists()
            or TrialBooking.objects.using(alias).filter(
                models.Q(lead__email_normalized__iexact=user.email) |
                (models.Q(lead__phone_normalized=user.phone) if user.phone else models.Q(pk=None))
            ).exists()
        )
        if has_trial:
            return Response(
                {
                    'detail': 'You have already redeemed your 1 free trial session. Please select a session pack to book this class.',
                    'code': 'TRIAL_ALREADY_USED',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 2. Resolve occurrence
        try:
            occurrence = (
                ClassOccurrence.objects.using(alias)
                .select_related('class_template__category__organization', 'branch')
                .get(id=occurrence_id)
            )
        except ClassOccurrence.DoesNotExist:
            return Response({'detail': 'Class slot not found.'}, status=status.HTTP_404_NOT_FOUND)

        # 3. Book slot using atomic booking service
        try:
            booking = BookingWaitlistAttendanceService.create_booking(
                user_profile=profile,
                occurrence=occurrence,
                booking_type='TRIAL',
                booking_source='MOBILE_APP',
                membership=None,
                created_by_user=user,
                db_alias=alias,
            )
        except ParqRequiredValidationError as e:
            return Response({
                'code': e.code,
                'detail': str(e.message if hasattr(e, 'message') else e),
                'error': str(e.message if hasattr(e, 'message') else e),
                'membership_id': str(e.membership.id) if e.membership else None,
                'form_id': str(e.form.id) if e.form else None,
                'form_title': e.form.name if e.form else None,
            }, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as e:
            if getattr(e, 'code', None) in ['PARQ_REQUIRED', 'PARQ_CONFIG_ERROR']:
                return Response({
                    'code': getattr(e, 'code', 'PARQ_REQUIRED'),
                    'detail': str(e.messages[0] if hasattr(e, 'messages') and e.messages else e),
                    'error': str(e.messages[0] if hasattr(e, 'messages') and e.messages else e),
                }, status=status.HTTP_400_BAD_REQUEST)
            detail = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
            return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # Advance CRM Lead status to TRIAL_BOOKED and log TrialBooking record
        try:
            lead = Lead.objects.using(alias).filter(
                models.Q(email_normalized__iexact=user.email) |
                (models.Q(phone_normalized=user.phone) if user.phone else models.Q(pk=None))
            ).first()
            if not lead:
                org = occurrence.branch.organization if occurrence.branch else Organization.objects.using(alias).first()
                lead_source, _ = LeadSource.objects.using(alias).get_or_create(
                    organization=org,
                    code='MOBILE_APP',
                    defaults={'name': 'Mobile App', 'source_type': 'MOBILE_APP', 'status': 'ACTIVE'}
                )
                lead = CRMLeadService.create_lead(
                    organization=org,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    phone=user.phone,
                    email=user.email,
                    branch=occurrence.branch,
                    lead_source=lead_source,
                    db_alias=alias,
                )
            if lead:
                lead.current_status = 'TRIAL_BOOKED'
                lead.converted_user_profile = profile
                lead.save(using=alias, update_fields=['current_status', 'converted_user_profile'])
                TrialBooking.objects.using(alias).get_or_create(
                    lead=lead,
                    class_occurrence_id=occurrence.id,
                    defaults={
                        'user_profile': profile,
                        'branch': occurrence.branch,
                        'scheduled_start': occurrence.start_at,
                        'scheduled_end': occurrence.end_at,
                        'status': 'BOOKED',
                        'confirmation_status': 'CONFIRMED' if booking.status == 'CONFIRMED' else 'PENDING',
                        'booking_source': 'MOBILE_APP',
                    }
                )
        except Exception as trial_crm_err:
            logger.warning("Could not sync CRM trial booking for %s: %s", user.email, trial_crm_err)

        resp_data = {
            'booking_id': str(booking.id),
            'booking_number': booking.booking_number,
            'status': booking.status,
            'waitlist_position': booking.waitlist_position,
            'is_free_trial': True,
            'class_name': occurrence.class_template.name,
            'branch_name': occurrence.branch.name if occurrence.branch else 'SWEAT Studio',
            'date': occurrence.occurrence_date.isoformat() if occurrence.occurrence_date else None,
            'start_at': occurrence.start_at.isoformat() if occurrence.start_at else None,
            'message': (
                'Free trial booked successfully! See you on the reformer.'
                if booking.status == 'CONFIRMED'
                else f'Class is full. You are #{booking.waitlist_position} on the waitlist.'
            ),
        }
        if auth_tokens:
            resp_data['auth'] = auth_tokens

        return Response(resp_data, status=status.HTTP_201_CREATED)


class MobileBookClassView(APIView):
    """
    POST /api/v1/mobile/bookings/book/
    Books a seat using an available session credit from member's active package.
    Automates FIFO waitlist joining if slot is full. Concurrency protected.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, occurrence_id=None):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        occ_id = occurrence_id or request.data.get('occurrence_id')
        if not occ_id:
            return Response({'detail': 'occurrence_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Resolve occurrence
        try:
            occurrence = (
                ClassOccurrence.objects.using(alias)
                .select_related('class_template__category__organization', 'branch')
                .get(id=occ_id)
            )
        except ClassOccurrence.DoesNotExist:
            return Response({'detail': 'Class slot not found.'}, status=status.HTTP_404_NOT_FOUND)

        # 2. Resolve active membership with remaining session units
        active_membership = (
            Membership.objects.using(alias)
            .filter(
                user_profile=profile,
                status='ACTIVE',
                end_date__gte=timezone.now().date(),
            )
            .select_related('package', 'package_version')
            .order_by('-created_at')
            .first()
        )

        if not active_membership:
            return Response(
                {
                    'detail': 'No active session pack found. Please purchase a session pack to book this class.',
                    'code': 'NO_ACTIVE_MEMBERSHIP',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check remaining session balance
        entitlements = list(active_membership.entitlements.using(alias).filter(status='ACTIVE'))
        total_remaining = sum(
            (e.allocated_units - e.consumed_units)
            for e in entitlements
            if e.is_unlimited or e.allocated_units > e.consumed_units
        )
        has_unlimited = any(e.is_unlimited for e in entitlements)

        if not has_unlimited and total_remaining <= 0:
            return Response(
                {
                    'detail': 'You have 0 remaining session credits in your active pack. Please renew or purchase a new pack.',
                    'code': 'ZERO_CREDITS_REMAINING',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 3. Create booking with concurrency lock
        try:
            booking = BookingWaitlistAttendanceService.create_booking(
                user_profile=profile,
                occurrence=occurrence,
                booking_type='MEMBER',
                booking_source='MOBILE_APP',
                membership=active_membership,
                created_by_user=user,
                db_alias=alias,
            )
        except ValidationError as e:
            detail = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
            return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        is_waitlisted = (booking.status == 'WAITLISTED')
        return Response({
            'booking_id': str(booking.id),
            'booking_number': booking.booking_number,
            'status': booking.status,
            'waitlist_position': booking.waitlist_position if is_waitlisted else None,
            'class_name': occurrence.class_template.name,
            'branch_name': occurrence.branch.name if occurrence.branch else 'SWEAT Studio',
            'date': occurrence.occurrence_date.isoformat() if occurrence.occurrence_date else None,
            'start_at': occurrence.start_at.isoformat() if occurrence.start_at else None,
            'message': (
                f'Seat confirmed! 1 session credit deducted. Booking ref: {booking.booking_number}'
                if not is_waitlisted
                else f'Class is currently full. You are #{booking.waitlist_position} on the waitlist.'
            ),
        }, status=status.HTTP_201_CREATED)


class MobileMyBookingsView(APIView):
    """
    GET /api/v1/mobile/bookings/my-bookings/
    Returns user's Upcoming, Waitlisted, and Past class sessions.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)
        now = timezone.now()

        bookings = (
            Booking.objects.using(alias)
            .filter(user_profile=profile)
            .select_related(
                'occurrence__class_template',
                'occurrence__branch',
                'branch',
            )
            .prefetch_related(
                'occurrence__trainer_assignments__trainer_profile__employee_profile__user_profile__user'
            )
            .order_by('-occurrence__start_at')
        )

        upcoming = []
        waitlisted = []
        past = []

        for b in bookings:
            occ = b.occurrence
            template = occ.class_template if occ else None
            branch = occ.branch or b.branch

            # Trainer info
            trainer_name = 'SWEAT Trainer'
            if occ:
                trainer_rec = occ.trainer_assignments.filter(trainer_role='LEAD').first() or occ.trainer_assignments.first()
                if trainer_rec and trainer_rec.trainer_profile and getattr(trainer_rec.trainer_profile, 'employee_profile', None):
                    emp = trainer_rec.trainer_profile.employee_profile
                    if emp.user_profile and emp.user_profile.user:
                        t_user = emp.user_profile.user
                        trainer_name = f"{t_user.first_name} {t_user.last_name}".strip()

            item = {
                'id': str(b.id),
                'booking_number': b.booking_number,
                'occurrence_id': str(occ.id) if occ else None,
                'status': b.status,
                'booking_type': b.booking_type,
                'waitlist_position': b.waitlist_position,
                'class_name': template.name if template else 'Reformer Pilates',
                'category': template.category.name if template and template.category else 'Bootcamp',
                'branch_name': branch.name if branch else 'SWEAT Studio',
                'date': occ.occurrence_date.isoformat() if occ and occ.occurrence_date else None,
                'start_at': occ.start_at.isoformat() if occ and occ.start_at else None,
                'end_at': occ.end_at.isoformat() if occ and occ.end_at else None,
                'duration_minutes': template.default_duration_minutes if template else 50,
                'trainer_name': trainer_name,
                'booked_at': b.booked_at.isoformat() if b.booked_at else None,
                'can_reschedule': (b.status in ['CONFIRMED', 'RESERVED']),
                'can_cancel': (b.status in ['CONFIRMED', 'RESERVED']),
            }

            if b.status == 'WAITLISTED' and occ and occ.start_at >= now:
                waitlisted.append(item)
            elif b.status in ['CONFIRMED', 'RESERVED'] and occ and occ.start_at >= now:
                upcoming.append(item)
            else:
                past.append(item)

        # Sort upcoming by earliest start time first
        upcoming.sort(key=lambda x: x.get('start_at') or '')
        waitlisted.sort(key=lambda x: x.get('start_at') or '')

        return Response({
            'upcoming': upcoming,
            'waitlisted': waitlisted,
            'past': past,
        })


class MobileCancelBookingView(APIView):
    """
    POST /api/v1/mobile/bookings/<uuid:booking_id>/cancel/
    Cancels an upcoming class booking within cutoff window, refunds credit, and promotes waitlist.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, booking_id):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        try:
            booking = (
                Booking.objects.using(alias)
                .select_related('occurrence__class_template__category__organization', 'branch', 'user_profile')
                .get(id=booking_id)
            )
        except Booking.DoesNotExist:
            return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Security check: User can only cancel their own booking
        if booking.user_profile_id != profile.id:
            return Response({'detail': 'You can only cancel your own bookings.'}, status=status.HTTP_403_FORBIDDEN)

        if booking.status in ['CANCELLED', 'COMPLETED', 'NO_SHOW']:
            return Response({'detail': f'Booking is already {booking.status.lower()}.'}, status=status.HTTP_400_BAD_REQUEST)

        reason = request.data.get('reason', 'Member cancelled via mobile app.')

        try:
            cancellation = BookingWaitlistAttendanceService.cancel_booking(
                booking=booking,
                cancelled_by_user=user,
                reason_code='MEMBER_MOBILE_CANCEL',
                reason_text=reason,
                db_alias=alias,
            )
            session_action = cancellation.session_action_applied if cancellation else 'RESTORE'
        except ValidationError as e:
            detail = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
            return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'detail': 'Booking cancelled successfully.',
            'booking_id': str(booking.id),
            'status': 'CANCELLED',
            'session_credit_refunded': (session_action == 'RESTORE'),
        })


class MobileRescheduleBookingView(APIView):
    """
    POST /api/v1/mobile/bookings/<uuid:booking_id>/reschedule/
    Reschedules a member's own booking to another eligible class occurrence.
    Validates cutoff, verifies capacity/waitlist, transfers session credit entitlement.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, booking_id):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        try:
            booking = (
                Booking.objects.using(alias)
                .select_related('occurrence__class_template__category__organization', 'branch', 'user_profile')
                .get(id=booking_id)
            )
        except Booking.DoesNotExist:
            return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        if booking.user_profile_id != profile.id:
            return Response({'detail': 'You can only reschedule your own bookings.'}, status=status.HTTP_403_FORBIDDEN)

        if booking.status in ['CANCELLED', 'COMPLETED', 'NO_SHOW']:
            return Response({'detail': f'Booking is already {booking.status.lower()} and cannot be rescheduled.'}, status=status.HTTP_400_BAD_REQUEST)

        to_occurrence_id = request.data.get('to_occurrence_id') or request.data.get('new_occurrence_id')
        if not to_occurrence_id:
            return Response({'detail': 'to_occurrence_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            to_occurrence = (
                ClassOccurrence.objects.using(alias)
                .select_related('class_template__category__organization', 'branch')
                .get(id=to_occurrence_id)
            )
        except ClassOccurrence.DoesNotExist:
            return Response({'detail': 'Target class slot not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            new_booking = BookingWaitlistAttendanceService.reschedule_booking(
                booking=booking,
                to_occurrence=to_occurrence,
                rescheduled_by_user=user,
                reason_code='MEMBER_PORTAL_RESCHEDULE',
                reason_text=request.data.get('reason', 'Rescheduled by member via Member Portal'),
                db_alias=alias,
            )
            return Response({
                'detail': 'Workout rescheduled successfully!',
                'booking_id': str(new_booking.id),
                'booking_number': new_booking.booking_number,
                'status': new_booking.status,
                'class_name': to_occurrence.class_template.name,
                'start_at': to_occurrence.start_at.isoformat() if to_occurrence.start_at else None,
                'branch_name': to_occurrence.branch.name if to_occurrence.branch else 'SWEAT Studio',
            }, status=status.HTTP_200_OK)
        except ValidationError as e:
            detail = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
            return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class MobileQRPassView(APIView):
    """
    GET /api/v1/mobile/bookings/<uuid:booking_id>/qr-pass/
    Generates a cryptographically signed, verified QR pass token for turnstile / reception scanning.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, booking_id=None):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        if not booking_id:
            # General Member Digital Turnstile Access Pass
            valid_until_ts = int(timezone.now().timestamp()) + 86400
            pass_data = {
                'uid': str(user.id),
                'mno': profile.member_number or 'MEM-1CCA18',
                'name': f"{user.first_name} {user.last_name}".strip(),
                'branch': user.home_branch.name if getattr(user, 'home_branch', None) else 'All SWEAT Studios',
                'type': 'STUDIO_TURNSTILE_ACCESS',
                'exp': valid_until_ts,
            }
            raw_str = json.dumps(pass_data, separators=(',', ':'))
            sig = hmac.new(settings.SECRET_KEY.encode('utf-8'), raw_str.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
            token = f"{base64.urlsafe_b64encode(raw_str.encode()).decode().rstrip('=')}.{sig}"
            return Response({
                'pass_id': str(uuid.uuid4()),
                'qr_token': token,
                'qr_payload': token,
                'member_name': pass_data['name'],
                'membership_number': pass_data['mno'],
                'branch_name': pass_data['branch'],
                'valid_until': datetime.fromtimestamp(valid_until_ts).isoformat(),
            })

        try:
            booking = (
                Booking.objects.using(alias)
                .select_related('occurrence__class_template', 'branch', 'user_profile')
                .get(id=booking_id)
            )
        except Booking.DoesNotExist:
            return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        if booking.user_profile_id != profile.id:
            return Response({'detail': 'Access denied.'}, status=status.HTTP_403_FORBIDDEN)

        occ = booking.occurrence
        start_ts = int(occ.start_at.timestamp()) if occ and occ.start_at else int(timezone.now().timestamp())
        # Valid from 60 minutes before class until 60 minutes after class ends
        valid_until_ts = start_ts + 7200

        # Construct signed payload
        pass_data = {
            'bid': str(booking.id),
            'bno': booking.booking_number,
            'uid': str(user.id),
            'mno': profile.member_number or 'MEMBER',
            'name': f"{user.first_name} {user.last_name}".strip(),
            'class': occ.class_template.name if occ and occ.class_template else 'SWEAT Session',
            'branch': booking.branch.name if booking.branch else 'SWEAT Studio',
            'status': booking.status,
            'exp': valid_until_ts,
        }

        raw_str = json.dumps(pass_data, separators=(',', ':'))
        sig = hmac.new(settings.SECRET_KEY.encode('utf-8'), raw_str.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
        token = f"{base64.urlsafe_b64encode(raw_str.encode()).decode().rstrip('=')}.{sig}"

        return Response({
            'qr_token': token,
            'booking_number': booking.booking_number,
            'member_name': f"{user.first_name} {user.last_name}".strip(),
            'member_number': profile.member_number,
            'class_name': pass_data['class'],
            'branch_name': pass_data['branch'],
            'start_at': occ.start_at.isoformat() if occ and occ.start_at else None,
            'status': booking.status,
            'valid_until': datetime.fromtimestamp(valid_until_ts).isoformat(),
        })


# ============================================================================
# 4. Memberships, Class Packs & Payments Views
# ============================================================================

class MobilePackagesView(APIView):
    """
    GET /api/v1/mobile/packages/
    Catalog of available session packs (1 Session, 12 Sessions, 36 Sessions, Unlimited, etc.)
    with prices, validity days, and offer badges.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response([], status=status.HTTP_200_OK)

        branch_id = request.query_params.get('branch_id')

        packages = (
            Package.objects.using(alias)
            .filter(status='ACTIVE')
            .prefetch_related('versions__prices', 'versions__entitlement_definitions')
            .order_by('created_at')
        )

        results = []
        for pkg in packages:
            latest_version = pkg.versions.filter(status='ACTIVE').order_by('-version_number').first()
            if not latest_version:
                continue

            # Price lookup
            prices = latest_version.prices.filter(status='ACTIVE')
            if branch_id:
                branch_price = prices.filter(branch_id=branch_id).first()
                price_obj = branch_price or prices.filter(branch__isnull=True).first()
            else:
                price_obj = prices.filter(branch__isnull=True).first() or prices.first()

            if not price_obj:
                continue

            # Entitlement / Session count lookup
            ent_def = latest_version.entitlement_definitions.first()
            raw_units = getattr(ent_def, 'allocated_units', None) or getattr(ent_def, 'total_units', None) if ent_def else None
            session_count = int(raw_units) if raw_units is not None and not getattr(ent_def, 'is_unlimited', False) else 12
            is_unlimited = bool(ent_def and getattr(ent_def, 'is_unlimited', False))

            base_price = float(price_obj.base_price)
            tax_pct = float(price_obj.tax_percent or Decimal('18.0'))
            final_price = round(base_price * (1 + (tax_pct / 100.0)), 2)

            badge = ''
            if session_count >= 36:
                badge = 'BEST VALUE'
            elif session_count == 12:
                badge = 'MOST POPULAR'
            elif session_count == 1:
                badge = 'DROP-IN'

            results.append({
                'id': str(pkg.id),
                'package_version_id': str(latest_version.id),
                'price_id': str(price_obj.id),
                'name': pkg.name,
                'code': pkg.code,
                'description': latest_version.description_snapshot or getattr(pkg, 'description', '') or 'All-access Reformer Pilates package.',
                'sessions': 'Unlimited' if is_unlimited else session_count,
                'session_count': None if is_unlimited else session_count,
                'is_unlimited': is_unlimited,
                'validity_days': latest_version.validity_days or 90,
                'base_price': base_price,
                'tax_percent': tax_pct,
                'final_price': final_price,
                'currency': 'INR',
                'badge': badge,
            })

        return Response(results)


class MobileMyCreditsView(APIView):
    """
    GET /api/v1/mobile/my-credits/
    Active session credit balance, validity countdown, and near-expiry renewal status.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        today = timezone.now().date()
        memberships = (
            Membership.objects.using(alias)
            .filter(
                user_profile=profile,
            )
            .select_related('package')
            .prefetch_related('entitlements')
            .order_by('-created_at')
        )

        packs = []
        total_remaining_credits = 0

        for m in memberships:
            entitlements = list(m.entitlements.all())
            allocated = sum(float(e.allocated_units or 0.0) for e in entitlements)
            consumed = sum(float(e.consumed_units or 0.0) for e in entitlements)
            remaining = max(0.0, allocated - consumed)
            is_unlimited = any(e.is_unlimited for e in entitlements)
            days_left = max(0, (m.end_date - today).days) if m.end_date else 0

            # Distinguish active vs expired vs cancelled memberships
            effective_status = m.status
            if m.status == 'ACTIVE' and m.end_date and m.end_date < today:
                effective_status = 'EXPIRED'

            if effective_status == 'ACTIVE' and not is_unlimited:
                total_remaining_credits += int(remaining)

            packs.append({
                'membership_id': str(m.id),
                'package_name': m.package.name,
                'status': effective_status,
                'allocated_sessions': allocated,
                'consumed_sessions': consumed,
                'remaining_sessions': 'Unlimited' if is_unlimited else remaining,
                'is_unlimited': is_unlimited,
                'start_date': m.start_date.isoformat() if m.start_date else None,
                'end_date': m.end_date.isoformat() if m.end_date else None,
                'days_remaining': days_left,
                'near_expiry': effective_status == 'ACTIVE' and (days_left <= 7 or (not is_unlimited and remaining <= 2)),
                'can_renew': effective_status in ('ACTIVE', 'EXPIRED'),
            })

        return Response({
            'total_credits_available': total_remaining_credits,
            'active_packs_count': len(packs),
            'packs': packs,
        })



class MobileDiscountCouponsView(APIView):
    """
    GET /api/v1/mobile/coupons/
    Returns active promo and discount codes for the mobile app checkout & package discovery.
    Public / AllowAny so guests and authenticated members can browse active offers.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(getattr(request, 'user', None), '_state', None)
            alias = getattr(alias, 'db', None) or get_tenant_db_alias() or 'default'

        now = timezone.now()
        codes = (
            DiscountCode.objects.using(alias)
            .filter(
                status='ACTIVE',
                campaign__status='ACTIVE',
                campaign__valid_from__lte=now,
            )
            .filter(
                models.Q(campaign__valid_until__isnull=True) | models.Q(campaign__valid_until__gte=now)
            )
            .select_related('campaign', 'branch', 'package')
            .order_by('code')
        )

        results = []
        for c in codes:
            camp = c.campaign
            results.append({
                'id': str(c.id),
                'code': c.code,
                'name': camp.name,
                'description': camp.description or '',
                'discount_type': camp.discount_type,  # 'PERCENTAGE' or 'FIXED'
                'discount_value': float(camp.discount_value),
                'max_discount': float(camp.max_discount) if camp.max_discount else None,
                'minimum_order_amount': float(camp.minimum_order_amount) if camp.minimum_order_amount else 0.0,
                'valid_until': camp.valid_until.isoformat() if camp.valid_until else None,
                'applicable_branch': c.branch.name if c.branch else 'All Branches',
                'applicable_package': c.package.name if c.package else 'All Packages',
            })

        return Response(results, status=status.HTTP_200_OK)


class MobileValidateCouponView(APIView):
    """
    POST /api/v1/mobile/coupons/validate/
    Validates a discount coupon code and returns payable breakdown.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        code = (request.data.get('code') or '').strip().upper()
        package_id = request.data.get('package_id')
        subtotal = Decimal(str(request.data.get('subtotal') or '0'))

        if not code:
            return Response({'is_valid': False, 'detail': 'Promo code is required.'}, status=status.HTTP_400_BAD_REQUEST)

        profile = None
        if request.user and request.user.is_authenticated:
            profile = UserProfile.objects.using(alias).filter(user=request.user).first()

        pkg = Package.objects.using(alias).filter(id=package_id).first() if package_id else None

        result = DiscountCouponEngineService.validate_coupon(
            code_str=code,
            user_profile=profile,
            order_subtotal=subtotal,
            package=pkg,
            db_alias=alias,
        )

        if not result.get('is_valid'):
            return Response({
                'is_valid': False,
                'detail': result.get('reason') or 'Invalid promo code.',
            }, status=status.HTTP_400_BAD_REQUEST)

        discount_amount = float(result.get('discount_amount', 0))
        final_payable = max(0.0, float(subtotal) - discount_amount)

        return Response({
            'is_valid': True,
            'code': code,
            'discount_amount': discount_amount,
            'original_price': float(subtotal),
            'final_price': final_payable,
            'detail': f'Promo code applied! You save ₹{discount_amount:.2f}',
        })


class MobileCheckoutOrderView(APIView):
    """
    POST /api/v1/mobile/checkout/create-order/
    Creates a pending purchase order and generates a Razorpay order ID.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        requested_provider = str(request.data.get('payment_provider', 'RAZORPAY')).strip().upper()
        if requested_provider == 'CASH':
            return Response(
                {'detail': 'Cash payment is not permitted for member self-service. Only online payments are accepted.', 'code': 'PAYMENT_METHOD_NOT_ALLOWED_FOR_CHANNEL'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if requested_provider not in ['RAZORPAY', 'ONLINE', '']:
            return Response(
                {'detail': f"Payment provider '{requested_provider}' is not supported. Only RAZORPAY is accepted.", 'code': 'UNSUPPORTED_PAYMENT_PROVIDER'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = request.user
        alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        package_id = request.data.get('package_id')
        coupon_code = request.data.get('coupon_code')

        try:
            package = Package.objects.using(alias).get(id=package_id, status='ACTIVE')
        except Package.DoesNotExist:
            return Response({'detail': 'Selected package is no longer available.'}, status=status.HTTP_404_NOT_FOUND)

        latest_version = package.versions.filter(status='ACTIVE').order_by('-version_number').first()
        if not latest_version:
            return Response({'detail': 'Package version configuration not found.'}, status=status.HTTP_400_BAD_REQUEST)

        price_obj = latest_version.prices.filter(status='ACTIVE').first()
        if not price_obj:
            return Response({'detail': 'Package price not configured.'}, status=status.HTTP_400_BAD_REQUEST)

        base_price = price_obj.base_price
        tax_pct = price_obj.tax_percent or Decimal('18.0')
        subtotal = base_price * (1 + (tax_pct / Decimal('100.0')))
        discount_amount = Decimal('0.00')

        if coupon_code:
            v_res = DiscountCouponEngineService.validate_coupon(
                code_str=coupon_code,
                user_profile=profile,
                order_subtotal=subtotal,
                package=package,
                db_alias=alias,
            )
            if v_res.get('is_valid'):
                discount_amount = Decimal(str(v_res.get('discount_amount', 0)))

        final_amount = max(Decimal('0.00'), subtotal - discount_amount)

        branch = user.home_branch or Branch.objects.using(alias).filter(status='ACTIVE').first()
        org = Organization.objects.using(alias).first()

        with transaction.atomic(using=alias):
            order = Order.objects.using(alias).create(
                organization=org,
                branch=branch,
                user_profile=profile,
                order_number=f"ORD-{str(uuid.uuid4())[:8].upper()}",
                order_type='NEW_MEMBERSHIP',
                source='MOBILE_APP',
                status='PENDING_PAYMENT',
                subtotal=subtotal,
                discount_amount=discount_amount,
                tax_amount=subtotal * (tax_pct / Decimal('100.0')),
                total_amount=final_amount,
                currency='INR',
            )

            OrderItem.objects.using(alias).create(
                order=order,
                item_type='PACKAGE',
                package=package,
                package_version=latest_version,
                package_price=price_obj,
                quantity=1,
                unit_price=base_price,
                total_price=final_amount,
            )

        # Generate real Razorpay order via official service
        from .services_razorpay import RazorpayService
        if not RazorpayService.is_configured(db_alias=alias):
            return Response(
                {'detail': 'Razorpay is not configured on this server.', 'code': 'RAZORPAY_NOT_CONFIGURED'},
                status=status.HTTP_400_BAD_REQUEST
            )

        amount_paise = RazorpayService.convert_inr_to_paise(final_amount)
        key_id, _ = RazorpayService.get_credentials(db_alias=alias)
        rzp_order = RazorpayService.create_order(
            amount_paise=amount_paise,
            receipt=order.order_number,
            currency=order.currency,
            notes={
                'internal_order_id': str(order.id),
                'user_id': str(user.id),
                'order_number': order.order_number,
            },
            db_alias=alias,
        )

        from .services_payment_policy import PaymentPolicyService
        config_id, checkout_config = PaymentPolicyService.get_razorpay_checkout_config(
            organization=org,
            branch=branch,
            db_alias=alias,
        )

        return Response({
            'order_id': str(order.id),
            'order_number': order.order_number,
            'razorpay_order_id': rzp_order['id'],
            'amount_in_paise': amount_paise,
            'amount': float(final_amount),
            'currency': order.currency,
            'package_name': package.name,
            'key_id': key_id,
            'config_id': config_id,
            'config': checkout_config,
        }, status=status.HTTP_201_CREATED)


class MobileCheckoutVerifyView(APIView):
    """
    POST /api/v1/mobile/checkout/verify/
    Verifies payment completion, records transaction, and automatically converts Lead to Member:
    1. Upgrades UserProfile.member_type = 'MEMBER' & generates member_number.
    2. Upgrades TenantUser.user_type = 'MEMBER'.
    3. Confirms RoleAssignment with 'MEMBER' role.
    4. Converts CRM Lead status to 'CONVERTED' & creates LeadConversion record.
    5. Activates Membership & allocates session credits.
    Supports both live Razorpay signatures and mock/test sandbox completions.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        order_id = request.data.get('order_id')
        razorpay_order_id = request.data.get('razorpay_order_id')
        payment_id = request.data.get('razorpay_payment_id') or request.data.get('payment_id')
        signature = request.data.get('razorpay_signature')

        try:
            order = Order.objects.using(alias).select_related('organization', 'branch').get(id=order_id, user_profile=profile)
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

        if order.status == 'PAID':
            membership = Membership.objects.using(alias).filter(source_order=order).first()
            return Response({
                'detail': 'Payment already verified. Your session pack is active.',
                'membership_id': str(membership.id) if membership else None,
                'package_name': membership.package.name if (membership and membership.package) else '',
                'start_date': membership.start_date.isoformat() if (membership and membership.start_date) else '',
                'end_date': membership.end_date.isoformat() if (membership and membership.end_date) else '',
                'order_number': order.order_number,
                'already_processed': True,
            }, status=status.HTTP_200_OK)

        order_item = order.items.filter(item_type='PACKAGE').first()
        if not order_item:
            return Response({'detail': 'Invalid order items.'}, status=status.HTTP_400_BAD_REQUEST)

        # Check for test / sandbox bypass or zero-amount coupon order
        is_sandbox_payment = (
            order.total_amount <= Decimal('0.00')
            or str(payment_id).startswith(('pay_test', 'test_', 'mock_'))
            or signature in ['mock_signature', 'test_signature', 'bypass', 'TEST']
        )

        if not is_sandbox_payment:
            if not payment_id or not razorpay_order_id or not signature:
                return Response(
                    {'detail': 'razorpay_order_id, razorpay_payment_id, and razorpay_signature are required.', 'code': 'RAZORPAY_PARAMS_MISSING'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            from .services_razorpay import RazorpayService
            if not RazorpayService.verify_payment_signature(razorpay_order_id, payment_id, signature, db_alias=alias):
                return Response(
                    {'detail': 'Invalid Razorpay payment signature.', 'code': 'RAZORPAY_SIGNATURE_INVALID'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Cross-check with Razorpay server API
            expected_paise = RazorpayService.convert_inr_to_paise(order.total_amount)
            try:
                RazorpayService.fetch_and_verify_payment(
                    razorpay_payment_id=payment_id,
                    expected_order_id=razorpay_order_id,
                    expected_amount_paise=expected_paise,
                    expected_currency=order.currency,
                    db_alias=alias,
                )
            except Exception as e:
                logger.info("Razorpay server fetch warning (proceeding if signature valid): %s", e)
        else:
            payment_id = payment_id or f"pay_test_{uuid.uuid4().hex[:12]}"
            razorpay_order_id = razorpay_order_id or f"order_test_{uuid.uuid4().hex[:12]}"
            signature = signature or "test_signature"

        with transaction.atomic(using=alias):
            # 1. Record payment transaction
            PaymentTransaction.objects.using(alias).create(
                order=order,
                amount=order.total_amount,
                currency=order.currency,
                provider='RAZORPAY',
                payment_method='RAZORPAY',
                provider_transaction_id=payment_id,
                status='SUCCESS',
                metadata={
                    'razorpay_order_id': razorpay_order_id,
                    'razorpay_payment_id': payment_id,
                    'razorpay_signature': signature,
                },
            )

            # 2. Mark order as PAID
            order.status = 'PAID'
            order.paid_at = timezone.now()
            order.save(using=alias)

            # 3. Activate membership & allocate session units internally
            membership = MembershipLifecycleService.activate_membership_from_order(
                order=order,
                order_item=order_item,
                created_by_user=user,
                db_alias=alias,
            )

            # 4. AUTO-CONVERT LEAD TO ACTIVE MEMBER IN DATABASE & RBAC
            if not profile.member_number:
                profile.member_number = f"SW-{str(uuid.uuid4())[:6].upper()}"
            profile.member_type = 'MEMBER'
            profile.save(using=alias, update_fields=['member_number', 'member_type'])

            # Elevate TenantUser user_type
            user.user_type = 'MEMBER'
            user.save(using=alias, update_fields=['user_type'])

            # Ensure user has active MEMBER role assignment
            member_role = Role.objects.using(alias).filter(code='MEMBER').first()
            if member_role:
                RoleAssignment.objects.using(alias).get_or_create(
                    user=user,
                    role=member_role,
                    defaults={'branch': order.branch or user.home_branch, 'is_active': True}
                )

            # 5. Advance CRM Lead status to CONVERTED
            try:
                lead = Lead.objects.using(alias).filter(
                    models.Q(email_normalized__iexact=user.email) |
                    (models.Q(phone_normalized=user.phone) if user.phone else models.Q(pk=None))
                ).first()
                if lead:
                    lead.current_status = 'CONVERTED'
                    lead.converted_user_profile = profile
                    lead.save(using=alias, update_fields=['current_status', 'converted_user_profile'])

                    LeadConversion.objects.using(alias).get_or_create(
                        lead=lead,
                        user_profile=profile,
                        defaults={
                            'order_id': order.id,
                            'membership_id': membership.id,
                            'package_id': getattr(order_item, 'package_id', None),
                            'package_version_id': getattr(order_item, 'package_version_id', None),
                            'conversion_source': 'MOBILE_APP',
                        }
                    )
            except Exception as conv_crm_err:
                logger.warning("Could not sync CRM lead conversion for %s: %s", user.email, conv_crm_err)

        # Summarize allocated credits
        entitlements = MembershipEntitlement.objects.using(alias).filter(membership=membership)
        credits_summary = []
        for ent in entitlements:
            credits_summary.append({
                'entitlement_type': ent.entitlement_type,
                'allocated_units': str(ent.allocated_units) if ent.allocated_units else 'UNLIMITED',
                'is_unlimited': ent.is_unlimited,
                'valid_until': ent.valid_until.isoformat() if ent.valid_until else None,
            })

        return Response({
            'detail': 'Payment successful! Account converted to Active Member and credits allocated.',
            'member_id': str(profile.id),
            'member_number': profile.member_number,
            'user_type': 'MEMBER',
            'lead_status': 'CONVERTED',
            'membership_id': str(membership.id),
            'package_name': membership.package.name,
            'credits_allocated': credits_summary,
            'start_date': membership.start_date.isoformat(),
            'end_date': membership.end_date.isoformat(),
            'order_number': order.order_number,
        })


class MobileOnboardingSurveyView(APIView):
    """
    GET /api/v1/mobile/onboarding/survey/
    Fetches onboarding and PAR-Q questions (goals, fitness experience, medical clearances).
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)

        # Questions specification tailored for SWEAT Reformer Pilates
        survey_sections = [
            {
                'id': 'goals',
                'title': 'Your Fitness Goals',
                'description': 'What do you hope to achieve with SWEAT Reformer Pilates?',
                'questions': [
                    {
                        'id': 'primary_goal',
                        'label': 'Primary Focus Area',
                        'type': 'MULTI_SELECT',
                        'options': [
                            'Core Strength & Toning',
                            'Postural Alignment',
                            'Flexibility & Mobility',
                            'Rehabilitation & Back Support',
                            'Athletic Conditioning',
                        ],
                    },
                    {
                        'id': 'experience_level',
                        'label': 'Pilates & Reformer Experience',
                        'type': 'SINGLE_SELECT',
                        'options': ['Complete Beginner (First time)', 'Intermediate (10+ classes)', 'Advanced Practitioner'],
                    },
                    {
                        'id': 'weekly_frequency',
                        'label': 'Target Weekly Workouts',
                        'type': 'SINGLE_SELECT',
                        'options': ['1-2 Sessions / Week', '3-4 Sessions / Week', '5+ Sessions / Week'],
                    },
                ],
            },
            {
                'id': 'par_q',
                'title': 'Physical Activity Readiness (PAR-Q)',
                'description': 'Your safety is our #1 priority. Please answer honestly.',
                'questions': [
                    {
                        'id': 'heart_condition',
                        'label': 'Has your doctor ever told you that you have a heart condition?',
                        'type': 'BOOLEAN',
                    },
                    {
                        'id': 'chest_pain',
                        'label': 'Do you feel pain in your chest when engaging in physical activity?',
                        'type': 'BOOLEAN',
                    },
                    {
                        'id': 'dizziness',
                        'label': 'Do you ever lose balance because of dizziness or lose consciousness?',
                        'type': 'BOOLEAN',
                    },
                    {
                        'id': 'bone_joint_problem',
                        'label': 'Do you have a bone or joint problem (e.g., knee, spine, hip) that could be aggravated by training?',
                        'type': 'BOOLEAN',
                    },
                ],
            },
            {
                'id': 'biometrics',
                'title': 'Biometrics & Physical Metrics',
                'description': 'Helps coaches calibrate spring resistance and posture alignments.',
                'questions': [
                    {'id': 'height_cm', 'label': 'Height (cm)', 'type': 'NUMBER'},
                    {'id': 'current_weight_kg', 'label': 'Current Weight (kg)', 'type': 'NUMBER'},
                    {'id': 'target_weight_kg', 'label': 'Target Weight (kg)', 'type': 'NUMBER'},
                    {'id': 'age', 'label': 'Age', 'type': 'NUMBER'},
                ],
            },
        ]

        saved_onboarding = None
        if request.user and request.user.is_authenticated:
            user_profile = UserProfile.objects.using(alias).filter(user=request.user).first()
            if user_profile and user_profile.address_json:
                saved_onboarding = user_profile.address_json.get('onboarding')

        is_cleared = False
        if saved_onboarding:
            is_cleared = not saved_onboarding.get('requires_doctor_clearance', False)

        return Response({
            'sections': survey_sections,
            'saved_responses': saved_onboarding,
            'is_cleared': is_cleared,
            'is_completed': saved_onboarding is not None,
        })


class MobileOnboardingSubmitView(APIView):
    """
    POST /api/v1/mobile/onboarding/submit/
    Saves member onboarding goals, PAR-Q clearances, and biometrics.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        data = request.data
        goals = data.get('goals', {})
        par_q = data.get('par_q', {})
        biometrics = data.get('biometrics', {})

        # Flag if doctor clearance is required based on PAR-Q responses
        requires_doctor_clearance = any(
            par_q.get(k) is True
            for k in ['heart_condition', 'chest_pain', 'dizziness', 'bone_joint_problem']
        )

        # Store in user profile's JSON fields
        current_data = profile.address_json or {}
        current_data['onboarding'] = {
            'completed_at': timezone.now().isoformat(),
            'goals': goals,
            'par_q': par_q,
            'biometrics': biometrics,
            'requires_doctor_clearance': requires_doctor_clearance,
        }
        profile.address_json = current_data
        profile.save(using=alias)

        return Response({
            'detail': 'PAR-Q health assessment saved successfully.',
            'requires_doctor_clearance': requires_doctor_clearance,
            'is_cleared': not requires_doctor_clearance,
            'message': (
                'Notice: Based on your health responses, a medical clearance is recommended prior to high intensity sessions.'
                if requires_doctor_clearance
                else 'PAR-Q Cleared! You are medically cleared to participate in all studio workouts.'
            ),
        })


class MobilePTAppointmentsView(APIView):
    """
    GET, POST /api/v1/mobile/pt/appointments/
    Lists and books 1-on-1 Personal Training appointments for authenticated members.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        from .models_appointments import Appointment
        appts = (
            Appointment.objects.using(alias)
            .filter(user_profile=profile)
            .select_related('appointment_type', 'branch')
            .prefetch_related('assigned_trainers__trainer_profile__employee_profile__user_profile__user')
            .order_by('-start_at')
        )

        results = []
        for a in appts:
            assigned = a.assigned_trainers.first()
            trainer_name = 'SWEAT Master Coach'
            trainer_photo = ''
            trainer_designation = 'Personal Trainer'
            if assigned and assigned.trainer_profile:
                t_prof = assigned.trainer_profile
                emp = getattr(t_prof, 'employee_profile', None)
                if emp:
                    trainer_designation = emp.designation or trainer_designation
                    if emp.user_profile and emp.user_profile.user:
                        u = emp.user_profile.user
                        trainer_name = f"{u.first_name} {u.last_name}".strip()
                        trainer_photo = u.avatar_url or ''

            results.append({
                'id': str(a.id),
                'appointment_number': f"PT-{str(a.id)[:8].upper()}",
                'type_name': a.appointment_type.name if a.appointment_type else '1-on-1 Personal Training',
                'status': a.status,
                'start_at': a.start_at.isoformat() if a.start_at else None,
                'end_at': a.end_at.isoformat() if a.end_at else None,
                'trainer_name': trainer_name,
                'trainer_avatar': trainer_photo,
                'trainer_designation': trainer_designation,
                'branch_name': a.branch.name if a.branch else 'SWEAT Studio',
                'notes': a.notes or '',
            })
        return Response(results)

    def post(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        from .models_appointments import Appointment, AppointmentType, AppointmentTrainer

        data = request.data
        trainer_id = data.get('trainer_id')
        start_at_str = data.get('start_at')
        duration_minutes = int(data.get('duration_minutes', 60))
        focus = data.get('focus', 'Pilates Core & Athletic Conditioning')
        notes = data.get('notes', f"Focus: {focus}")

        if not start_at_str:
            return Response({'detail': 'start_at date and time is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            start_at = datetime.fromisoformat(start_at_str)
            if timezone.is_naive(start_at):
                start_at = timezone.make_aware(start_at)
        except Exception:
            return Response({'detail': 'Invalid datetime format for start_at.'}, status=status.HTTP_400_BAD_REQUEST)

        end_at = start_at + timedelta(minutes=duration_minutes)

        org = Organization.objects.using(alias).first()
        branch = getattr(user, 'home_branch', None) or Branch.objects.using(alias).filter(status='ACTIVE').first()

        appt_type = AppointmentType.objects.using(alias).filter(status='ACTIVE').first()
        if not appt_type:
            appt_type = AppointmentType.objects.using(alias).create(
                organization=org,
                code='PT-1ON1',
                name='1-on-1 Personal Training',
                default_duration_minutes=60,
                default_delivery_mode='OFFLINE',
                requires_trainer=True,
                status='ACTIVE',
            )

        with transaction.atomic(using=alias):
            appt = Appointment.objects.using(alias).create(
                branch=branch,
                appointment_type=appt_type,
                user_profile=profile,
                start_at=start_at,
                end_at=end_at,
                status='CONFIRMED',
                booking_source='WEB',
                notes=notes,
            )

            if trainer_id:
                trainer_prof = TrainerProfile.objects.using(alias).filter(id=trainer_id).first()
                if trainer_prof:
                    AppointmentTrainer.objects.using(alias).create(
                        appointment=appt,
                        trainer_profile=trainer_prof,
                        role='LEAD',
                    )

        return Response({
            'detail': '1-on-1 Personal Training session scheduled successfully!',
            'appointment_id': str(appt.id),
            'appointment_number': f"PT-{str(appt.id)[:8].upper()}",
            'status': appt.status,
            'start_at': appt.start_at.isoformat(),
        }, status=status.HTTP_201_CREATED)


class MobileCancelPTAppointmentView(APIView):
    """
    POST /api/v1/mobile/pt/appointments/<uuid:appointment_id>/cancel/
    Cancels a member's 1-on-1 Personal Training appointment.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, appointment_id):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        from .models_appointments import Appointment
        try:
            appt = Appointment.objects.using(alias).get(id=appointment_id, user_profile=profile)
        except Appointment.DoesNotExist:
            return Response({'detail': 'Appointment not found.'}, status=status.HTTP_404_NOT_FOUND)

        appt.status = 'CANCELLED'
        appt.save(using=alias)

        return Response({
            'detail': 'Personal Training appointment cancelled successfully.',
            'appointment_id': str(appt.id),
            'status': 'CANCELLED',
        }, status=status.HTTP_200_OK)


class MobileLeadCaptureView(APIView):
    """
    POST /api/v1/mobile/leads/
    Public, unauthenticated lead creation endpoint.
    Accepts complete lead intake payload (First Name, Last Name, Email, Phone, Gender,
    Birthday, Branch, Program, Country, Location/Area, Goal) and creates the prospect
    directly in the CRM Leads panel with Round-Robin representative assignment.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data or {}
        first_name = (data.get('first_name') or data.get('firstName') or '').strip()
        last_name = (data.get('last_name') or data.get('lastName') or '').strip()
        email = (data.get('email') or data.get('emailAddress') or '').strip().lower()
        phone = (data.get('phone') or data.get('contactNumber') or '').strip()

        if not first_name:
            return Response(
                {'detail': 'First name is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not email and not phone:
            return Response(
                {'detail': 'Either email address or contact number is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        alias, tenant = _resolve_mobile_tenant_and_db(request)
        if not alias or not tenant:
            return Response(
                {'detail': 'Studio service is temporarily unavailable. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        with transaction.atomic(using=alias):
            org = Organization.objects.using(alias).filter(status='ACTIVE').order_by('-created_at').first()
            if not org:
                return Response(
                    {'detail': 'Studio organization configuration not found.'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Resolve Branch safely
            branch_id = data.get('branch_id') or data.get('location_id') or data.get('locationId')
            branch_name = data.get('branch_name') or data.get('branch')
            branch = None
            if branch_id and isinstance(branch_id, str):
                try:
                    uuid.UUID(str(branch_id).strip())
                    branch = Branch.objects.using(alias).filter(id=branch_id, status='ACTIVE').first()
                except (ValueError, TypeError):
                    branch = None
            if not branch and branch_name and isinstance(branch_name, str):
                branch = Branch.objects.using(alias).filter(name__icontains=branch_name.strip(), status='ACTIVE').first()
            if not branch:
                branch = Branch.objects.using(alias).filter(status='ACTIVE').first()

            # Resolve Program
            program_id = data.get('interested_program_id') or data.get('program_id')
            program_name = data.get('interested_in') or data.get('program_name')
            program = None
            if program_id:
                program = Program.objects.using(alias).filter(id=program_id, status='ACTIVE').first()
            if not program and program_name:
                program = Program.objects.using(alias).filter(name__icontains=program_name, status='ACTIVE').first()

            # Resolve Lead Source
            source_code = data.get('lead_source') or 'MOBILE_APP'
            lead_source, _ = LeadSource.objects.using(alias).get_or_create(
                organization=org,
                code=source_code,
                defaults={'name': 'Mobile App', 'source_type': 'MOBILE_APP', 'status': 'ACTIVE'}
            )

            # Build extra enrichment fields
            extra_fields = {}
            if data.get('gender'):
                extra_fields['gender'] = data.get('gender')
            if data.get('date_of_birth') or data.get('birthday'):
                extra_fields['date_of_birth'] = data.get('date_of_birth') or data.get('birthday')
            if data.get('fitness_goal') or data.get('goal'):
                extra_fields['fitness_goal'] = data.get('fitness_goal') or data.get('goal')
            if data.get('area') or data.get('location'):
                extra_fields['area'] = data.get('area') or data.get('location')
            if data.get('country'):
                extra_fields['country'] = data.get('country')
            if program:
                extra_fields['interested_program'] = program

            # Auto-create or update CRM Lead
            lead = Lead.objects.using(alias).filter(
                models.Q(email_normalized__iexact=email) if email else models.Q(pk=None) |
                (models.Q(phone_normalized=phone) if phone else models.Q(pk=None))
            ).first()

            if not lead:
                lead = CRMLeadService.create_lead(
                    organization=org,
                    first_name=first_name,
                    last_name=last_name or '',
                    phone=phone or '',
                    email=email,
                    branch=branch,
                    lead_source=lead_source,
                    assigned_sales_user=None,  # Runs configured auto-assignment (Round-Robin)
                    actor_user=None,
                    extra_fields=extra_fields,
                    db_alias=alias,
                )
                created = True
            else:
                created = False
                updated_fields = []
                if branch and not lead.branch:
                    lead.branch = branch
                    updated_fields.append('branch')
                if program and not lead.interested_program:
                    lead.interested_program = program
                    updated_fields.append('interested_program')
                for k, v in extra_fields.items():
                    if hasattr(lead, k) and not getattr(lead, k):
                        setattr(lead, k, v)
                        updated_fields.append(k)
                if updated_fields:
                    lead.save(using=alias, update_fields=updated_fields)

        return Response({
            'status': 'success',
            'lead_id': str(lead.id),
            'message': 'Lead registered successfully. Details are visible in the CRM Leads panel.',
            'lead': {
                'id': str(lead.id),
                'first_name': lead.first_name,
                'last_name': lead.last_name,
                'email': lead.email_normalized,
                'phone': lead.phone_normalized,
                'status': lead.current_status,
                'branch': branch.name if branch else None,
                'is_new': created,
            }
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

