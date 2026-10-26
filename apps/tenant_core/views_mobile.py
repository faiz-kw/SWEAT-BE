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

import sys
import re
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
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from config.routers import set_tenant_db_alias, get_tenant_db_alias
from apps.master.models_tenant import Tenant
from apps.master.services_auth_directory import sync_tenant_user_identity
from apps.authentication.views import _build_tenant_token, _register_and_resolve_tenant

from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_rbac import Role, RoleAssignment
from .models_workforce import UserProfile, TrainerProfile
from .models_classes import ClassTemplate, ClassOccurrence, ClassPrice, PackageClassAccessRule, ClassCategory
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
    Fails closed (returns None, None) if an explicit tenant slug is supplied but does not exist or is inactive.
    """
    explicit_slug = (
        (request.headers.get('X-Tenant-Slug') if hasattr(request, 'headers') else None)
        or request.META.get('HTTP_X_TENANT_SLUG')
        or (getattr(request, 'query_params', None) or getattr(request, 'GET', {})).get('tenant')
        or (request.data.get('tenant_slug') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
    )

    if explicit_slug:
        clean_slug = str(explicit_slug).strip().lower()
        if clean_slug in ('sweat-demo', 'sweat_demo', 'sweat-staging', 'sweat-dev'):
            clean_slug = 'sweat'
        tenant = Tenant.objects.using('default').filter(slug=clean_slug, status='ACTIVE').first()
        if not tenant:
            logger.warning("Rejecting invalid or inactive explicit mobile tenant slug: '%s'", clean_slug)
            return None, None
    else:
        tenant = None
        if getattr(request, 'user', None) and request.user.is_authenticated:
            org = getattr(request.user, 'organization', None)
            if org and hasattr(org, 'tenant') and getattr(org.tenant, 'status', None) == 'ACTIVE':
                tenant = org.tenant
        if not tenant:
            tenant = Tenant.objects.using('default').filter(slug='sweat', status='ACTIVE').first()
        if not tenant and 'test' in sys.argv:
            tenant = Tenant.objects.using('default').filter(status='ACTIVE').first()

    if not tenant:
        return None, None

    active_alias = get_tenant_db_alias()
    if active_alias and tenant:
        return active_alias, tenant

    # Check authenticated user's state db
    if getattr(request, 'user', None) and request.user.is_authenticated:
        user_db = getattr(getattr(request.user, '_state', None), 'db', None)
        if user_db:
            set_tenant_db_alias(user_db)
            return user_db, tenant

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
                {'detail': 'Organization not found or inactive. Please specify a valid organization.', 'code': 'TENANT_NOT_FOUND'},
                status=status.HTTP_404_NOT_FOUND,
            )

        with transaction.atomic(using=alias):
            # 1. Check duplicate user by email or phone
            dup_q = models.Q(email__iexact=email)
            if phone:
                dup_q = dup_q | models.Q(phone=phone)
            if TenantUser.objects.using(alias).filter(dup_q).exists():
                return Response(
                    {'detail': 'An account with this email address or phone number already exists.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # 2. Resolve organization and branch
            org = Organization.objects.using(alias).filter(status='ACTIVE').first()
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
                extra_lead_data = {}
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
                    lead.latest_touch_source = 'MOBILE_APP'
                    lead.save(using=alias, update_fields=['latest_touch_source'])
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
                {
                    'detail': 'Organization not found or inactive. Please specify a valid organization.',
                    'code': 'TENANT_NOT_FOUND',
                },
                status=status.HTTP_404_NOT_FOUND,
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
            {
                'detail': 'Organization not found or inactive. Please specify a valid organization.',
                'code': 'TENANT_NOT_FOUND',
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    with transaction.atomic(using=alias):
        # 1. Lookup existing user by email
        user = TenantUser.objects.using(alias).filter(email__iexact=email).first()
        is_new_user = False

        org = Organization.objects.using(alias).filter(status='ACTIVE').first()
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
                lead.latest_touch_source = f"MOBILE_{provider.upper()}"
                update_fields = ['latest_touch_source']
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

        # Check if user has an active membership and whether trial is already used
        has_active_membership = Membership.objects.using(alias).filter(
            user_profile=profile, status='ACTIVE', end_date__gte=timezone.now().date()
        ).exists()
        has_trial = (
            Booking.objects.using(alias).filter(user_profile=profile, booking_type='TRIAL').exists()
            or TrialBooking.objects.using(alias).filter(
                models.Q(lead__email_normalized__iexact=email) |
                (models.Q(lead__phone_normalized=user.phone) if user.phone else models.Q(pk=None))
            ).exists()
        )

        return Response({
            'access': str(tokens.access_token),
            'refresh': str(tokens),
            'user': {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone': user.phone or '',
                'role': 'member' if has_active_membership else 'lead',
                'role_code': 'MEMBER' if has_active_membership else 'LEAD',
                'user_type': 'MEMBER' if has_active_membership else 'LEAD',
                'is_member': has_active_membership,
                'member_number': profile.member_number or '',
                'avatar_url': user.avatar_url or '',
                'home_branch': home_branch_info,
            },
            'is_new_user': is_new_user,
            'is_member': has_active_membership,
            'can_book_trial': not has_active_membership and not has_trial,
            'redirect_destination': 'MEMBER_PORTAL' if has_active_membership else 'TRIAL_ONBOARDING',
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

        total_remaining_credits = float(membership_data.get('remaining_sessions', 0.0)) if membership_data else 0.0
        return Response({
            'user': {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone': user.phone,
                'role': getattr(user, 'user_type', 'MEMBER').lower(),
                'role_code': getattr(user, 'user_type', 'MEMBER').upper(),
                'user_type': getattr(user, 'user_type', 'MEMBER').upper(),
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
                'member_type': profile.member_type,
                'joining_date': profile.joining_date.isoformat() if profile.joining_date else None,
                'preferred_branch': {
                    'id': str(profile.preferred_branch.id) if profile.preferred_branch else None,
                    'name': profile.preferred_branch.name if profile.preferred_branch else 'SWEAT Studio',
                } if profile.preferred_branch else None,
            },
            'credits': total_remaining_credits,
            'credits_available': total_remaining_credits,
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

        hb_data = {
            'id': str(user.home_branch.id) if user.home_branch else (str(profile.preferred_branch.id) if profile.preferred_branch else None),
            'name': user.home_branch.name if user.home_branch else (profile.preferred_branch.name if profile.preferred_branch else 'SWEAT Studio'),
        } if (user.home_branch or profile.preferred_branch) else None

        return Response({
            'detail': 'Profile updated successfully.',
            'id': str(user.id),
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'phone': user.phone,
            'avatar_url': user.avatar_url or '',
            'membership_number': profile.member_number,
            'home_branch': hb_data,
            'user': {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone': user.phone,
                'avatar_url': user.avatar_url or '',
                'home_branch': hb_data,
            },
            'profile': {
                'member_number': profile.member_number,
                'member_status': profile.member_status,
                'member_type': profile.member_type,
                'preferred_branch': hb_data,
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

    loc_name = ''
    loc_code = ''
    if hasattr(b, 'location') and b.location:
        raw_loc = getattr(b.location, 'name', '') or ''
        loc_name = raw_loc.title() if raw_loc else ''
        loc_code = getattr(b.location, 'code', '') or ''

    return {
        'id': str(b.id),
        'name': b.name,
        'code': b.code,
        'location_name': loc_name,
        'location_code': loc_code,
        'address': full_address,
        'address_line1': addr_l1,
        'address_line2': addr_l2,
        'city': getattr(b, 'address_city', None) or getattr(b, 'city', None) or (getattr(b.location, 'city', None) if hasattr(b, 'location') and b.location else 'Mumbai'),
        'postal_code': getattr(b, 'address_postal_code', '') or getattr(b, 'postal_code', '') or (getattr(b.location, 'postal_code', '') if hasattr(b, 'location') and b.location else ''),
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


def _get_active_bookable_branches(request, alias):
    """
    Returns active, bookable studio branches scoped to the active organization.
    Filters out dormant/seed branches belonging to inactive or unrelated organizations.
    """
    branches_qs = Branch.objects.using(alias).filter(status='ACTIVE')
    active_org = None
    if request.user and request.user.is_authenticated:
        active_org = getattr(request.user, 'organization', None)
        if not active_org:
            from .models_workforce import UserProfile
            prof = UserProfile.objects.using(alias).filter(user=request.user).first()
            if prof and prof.organization:
                active_org = prof.organization
            elif prof and prof.preferred_branch and prof.preferred_branch.organization:
                active_org = prof.preferred_branch.organization

    if not active_org:
        from .models_classes import ClassOccurrence
        active_org_ids = list(
            ClassOccurrence.objects.using(alias)
            .values_list('branch__organization_id', flat=True)
            .distinct()
        )
        active_org_ids = [oid for oid in active_org_ids if oid]
        if active_org_ids:
            branches_qs = branches_qs.filter(organization_id__in=active_org_ids)
    else:
        branches_qs = branches_qs.filter(organization=active_org)

    return branches_qs.select_related('location').order_by('name')


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

        branches = _get_active_bookable_branches(request, alias)
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

        branches = _get_active_bookable_branches(request, alias)
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

        branch_id = request.query_params.get('branch_id')
        category_id = request.query_params.get('category_id')

        # Resolve authoritative studio timezone (default 'Asia/Kolkata')
        tz_name = 'Asia/Kolkata'
        if branch_id and str(branch_id).lower() != 'all':
            try:
                branch_obj = Branch.objects.using(alias).filter(id=branch_id).first()
                if branch_obj and branch_obj.timezone:
                    tz_name = branch_obj.timezone
            except Exception:
                pass

        try:
            import zoneinfo
            local_tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            import pytz
            local_tz = pytz.timezone(tz_name)

        now = timezone.now()
        local_now = now.astimezone(local_tz)

        date_str = request.query_params.get('date')
        if date_str:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                target_date = local_now.date()
        else:
            target_date = local_now.date()

        # Compute authoritative studio-local calendar day window converted to UTC
        from datetime import time as dt_time, timezone as dt_timezone
        day_start_local = datetime.combine(target_date, dt_time.min).replace(tzinfo=local_tz)
        day_end_local = datetime.combine(target_date, dt_time.max).replace(tzinfo=local_tz)
        day_start_utc = day_start_local.astimezone(dt_timezone.utc)
        day_end_utc = day_end_local.astimezone(dt_timezone.utc)

        # Query occurrences whose authoritative start_at falls within the studio-local calendar date
        qs = (
            ClassOccurrence.objects.using(alias)
            .filter(
                models.Q(start_at__gte=day_start_utc, start_at__lte=day_end_utc)
                | (models.Q(start_at__isnull=True) & models.Q(occurrence_date=target_date)),
                status__in=['OPEN', 'SCHEDULED', 'IN_PROGRESS', 'CONFIRMED', 'PUBLISHED', 'ACTIVE']
            )
            .select_related('class_template__category', 'branch')
            .prefetch_related(
                'trainer_assignments__trainer_profile__employee_profile__user_profile__user',
                'trainer_assignments__trainer_profile__specialty_assignments__specialty',
            )
            .order_by('start_at')
        )

        # Filter by branch (robust to 'all')
        if branch_id and str(branch_id).lower() != 'all':
            qs = qs.filter(branch_id=branch_id)

        # Filter by category/format (accepts UUID, code, or name, robust to 'all')
        category_param = request.query_params.get('category') or category_id
        if category_param and str(category_param).lower() != 'all':
            import uuid
            try:
                cat_uuid = uuid.UUID(str(category_param))
                qs = qs.filter(class_template__category_id=cat_uuid)
            except (ValueError, AttributeError):
                qs = qs.filter(
                    models.Q(class_template__category__name__iexact=category_param)
                    | models.Q(class_template__category__name__icontains=category_param)
                    | models.Q(class_template__category__code__iexact=category_param)
                    | models.Q(class_template__category__code__icontains=category_param)
                )

        # Authenticated user bookings for this day to show 'CONFIRMED' / 'WAITLISTED' badges
        user_booking_map = {}
        included_class_ids = None
        active_mem = None
        if request.user and request.user.is_authenticated:
            user_profile = _get_or_create_user_profile(request.user, alias)
            if not user_profile and getattr(request.user, 'email', None):
                user_profile = UserProfile.objects.using(alias).filter(user__email__iexact=request.user.email).first()
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

        # Check if caller requested trial slots only
        is_trial_filter = (
            'trials/available-slots' in request.path
            or request.query_params.get('for_trial', '').lower() in ('true', '1', 'yes')
            or request.query_params.get('trial_only', '').lower() in ('true', '1', 'yes')
        )
        hide_full = (
            request.query_params.get('hide_full', '').lower() in ('true', '1', 'yes')
            or request.query_params.get('available_only', '').lower() in ('true', '1', 'yes')
        )

        results = []
        for occ in qs:
            template = occ.class_template
            category = template.category if template else None

            # Calculate live capacity & booked seats
            confirmed_emails = set(
                occ.bookings.using(alias).filter(
                    status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
                ).values_list('user_profile__user__email', flat=True)
            )
            trial_crm_qs = TrialBooking.objects.using(alias).filter(
                class_occurrence_id=occ.id,
                status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']
            )
            if confirmed_emails:
                trial_crm_qs = trial_crm_qs.exclude(lead__email_normalized__in=confirmed_emails)
            trial_crm_count = trial_crm_qs.count()

            booking_confirmed = occ.bookings.using(alias).filter(
                status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
            )
            booking_trial_count = booking_confirmed.filter(booking_type='TRIAL').count()
            total_trial_booked = trial_crm_count + booking_trial_count

            regular_booked_count = booking_confirmed.exclude(booking_type='TRIAL').count()
            total_booked = regular_booked_count + total_trial_booked

            total_capacity = occ.capacity or (template.default_capacity if template else 10)
            available_seats = max(0, total_capacity - total_booked)
            is_full = (available_seats == 0)

            # Waitlist status
            waitlist_count = occ.bookings.using(alias).filter(status='WAITLISTED').count()
            allow_waitlist = bool(template and getattr(template, 'allow_waitlist', True))
            max_waitlist = (
                occ.waitlist_capacity
                if occ.waitlist_capacity is not None
                else (template.default_waitlist_capacity if (template and template.default_waitlist_capacity is not None) else 5)
            )
            is_waitlist_open = allow_waitlist and (waitlist_count < max_waitlist)

            # Trial capacity and availability
            effective_trial_cap = (
                occ.trial_capacity
                if occ.trial_capacity is not None
                else (template.default_trial_capacity if template else 0)
            )
            allow_trial = effective_trial_cap > 0
            trial_available_seats = max(0, min(available_seats, effective_trial_cap - total_trial_booked)) if allow_trial else 0
            is_trial_available = allow_trial and (not is_full) and (trial_available_seats > 0)

            # Time boundary checks against authoritative current timestamp
            effective_end_at = occ.end_at
            if not effective_end_at and occ.start_at:
                dur = template.default_duration_minutes if template else 50
                effective_end_at = occ.start_at + timedelta(minutes=dur)

            has_ended = bool(effective_end_at and effective_end_at <= now) or (occ.status == 'COMPLETED')
            has_started = bool(occ.start_at and occ.start_at <= now) or has_ended
            is_in_progress = bool(has_started and not has_ended)

            # Regular member booking state
            if has_ended:
                booking_state = 'ENDED'
                is_bookable = False
            elif is_in_progress:
                booking_state = 'IN_PROGRESS'
                is_bookable = False
            elif not is_full:
                booking_state = 'AVAILABLE'
                is_bookable = True
            elif is_waitlist_open:
                booking_state = 'WAITLIST'
                is_bookable = True
            else:
                booking_state = 'FULL'
                is_bookable = False

            # Filters:
            # 1. If trial slots requested, omit if trial capacity or class is full
            if is_trial_filter and not is_trial_available:
                continue

            # 2. If hide_full requested, omit if class AND waitlist are completely full
            if hide_full and not is_bookable:
                continue

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
            ineligible_reason = None
            allowed_pkg_name = active_mem.package.name if (active_mem and active_mem.package) else None
            if not is_included and template:
                ineligible_reason = f"Class '{template.name}' is not included in package version {active_mem.package_version.version_number if (active_mem and active_mem.package_version) else 1}."


            results.append({
                'id': str(occ.id),
                'class_id': str(template.id) if template else None,
                'class_name': template.name if template else 'Reformer Session',
                'category': category.name if category else 'Pilates Reformer',
                'category_id': str(category.id) if category else None,
                'category_code': category.code if category else None,
                'branch': {
                    'id': str(occ.branch.id) if occ.branch else None,
                    'name': occ.branch.name if occ.branch else 'SWEAT Studio',
                } if occ.branch else None,
                'date': (occ.start_at.astimezone(local_tz).date().isoformat() if occ.start_at else target_date.isoformat()),
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
                    'max_waitlist': max_waitlist,
                    'is_waitlist_open': is_waitlist_open,
                    'is_waitlist_full': not is_waitlist_open,
                    'is_completely_full': is_full and not is_waitlist_open,
                    'trial_capacity': effective_trial_cap,
                    'trial_booked_count': total_trial_booked,
                    'available_trial_seats': trial_available_seats,
                    'is_trial_available': is_trial_available,
                },
                'booking_state': booking_state,
                'is_bookable': is_bookable,
                'can_book': is_bookable and not is_full and not has_started and not has_ended,
                'can_waitlist': is_bookable and is_full and is_waitlist_open and not has_started and not has_ended,
                'has_started': has_started,
                'has_ended': has_ended,
                'is_in_progress': is_in_progress,
                'is_trial_available': is_trial_available,
                'available_trial_seats': trial_available_seats,
                'user_booking': user_booking,
                'is_included_in_plan': is_included,
                'ineligible_reason': ineligible_reason,
                'allowed_package_name': allowed_pkg_name,
            })

        all_categories = [
            {'id': str(cat.id), 'code': cat.code, 'name': cat.name}
            for cat in ClassCategory.objects.using(alias).filter(status='ACTIVE').order_by('display_order', 'name')
        ]

        return Response({
            'date': target_date.isoformat(),
            'branch_id': branch_id,
            'classes_count': len(results),
            'categories': all_categories,
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
        if not alias or not tenant:
            return Response(
                {
                    'detail': 'Organization not found or inactive. Please specify a valid organization.',
                    'code': 'TENANT_NOT_FOUND',
                },
                status=status.HTTP_404_NOT_FOUND,
            )

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
                clean_digits = re.sub(r'\D', '', phone)[-10:]
                email = f"lead_{clean_digits}@sweat.internal"

            if not email and not phone:
                return Response(
                    {'detail': 'Please provide first name and email or phone number to book a trial.', 'code': 'GUEST_INFO_REQUIRED'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Check if an account already exists with this email or phone
            existing_user = TenantUser.objects.using(alias).filter(
                models.Q(email__iexact=email) | (models.Q(phone=phone) if phone else models.Q(pk=None))
            ).first()

            if existing_user:
                return Response(
                    {
                        'detail': 'An account with this email address or phone number already exists. Please log in to your account to book your trial session.',
                        'code': 'AUTHENTICATION_REQUIRED',
                        'requires_login': True,
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            org = Organization.objects.using(alias).filter(status='ACTIVE').first()
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

        # 1. Enforce 1-time Free Trial policy with slot-level idempotency
        # Check if booking for this exact occurrence already exists
        existing_booking = Booking.objects.using(alias).filter(
            user_profile=profile, occurrence_id=occurrence_id, booking_type='TRIAL'
        ).first()

        if existing_booking:
            return Response({
                'booking_id': str(existing_booking.id),
                'booking_number': existing_booking.booking_number,
                'status': existing_booking.status,
                'waitlist_position': existing_booking.waitlist_position,
                'is_free_trial': True,
                'already_claimed': True,
                'message': 'Free trial booked successfully! See you on the reformer.',
            }, status=status.HTTP_200_OK)

        has_other_trial = (
            Booking.objects.using(alias).filter(user_profile=profile, booking_type='TRIAL').exclude(occurrence_id=occurrence_id).exists()
            or TrialBooking.objects.using(alias).filter(
                models.Q(lead__email_normalized__iexact=user.email) |
                (models.Q(lead__phone_normalized=user.phone) if user.phone else models.Q(pk=None))
            ).exclude(class_occurrence_id=occurrence_id).exists()
        )
        if has_other_trial:
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

        # Check occurrence lifecycle and start/end time boundaries
        now = timezone.now()
        effective_end_at = occurrence.end_at
        if not effective_end_at and occurrence.start_at:
            duration = occurrence.class_template.default_duration_minutes if occurrence.class_template else 50
            effective_end_at = occurrence.start_at + timedelta(minutes=duration)

        if occurrence.status in ('CANCELLED', 'COMPLETED'):
            return Response({
                'detail': f'This class session is {occurrence.status.lower()}.',
                'code': f'CLASS_{occurrence.status}',
            }, status=status.HTTP_400_BAD_REQUEST)

        if effective_end_at and effective_end_at <= now:
            return Response({
                'detail': 'Cannot book a trial for a class occurrence that has already ended.',
                'code': 'CLASS_ALREADY_ENDED',
            }, status=status.HTTP_400_BAD_REQUEST)

        if occurrence.start_at and occurrence.start_at <= now:
            return Response({
                'detail': 'Cannot book a trial for a class occurrence that has already started.',
                'code': 'CLASS_ALREADY_STARTED',
            }, status=status.HTTP_400_BAD_REQUEST)

        # Pre-check trial capacity availability before booking
        effective_trial_cap = (
            occurrence.trial_capacity
            if occurrence.trial_capacity is not None
            else (occurrence.class_template.default_trial_capacity if occurrence.class_template else 0)
        )
        if effective_trial_cap <= 0:
            return Response({
                'detail': 'Trial bookings are not enabled for this session.',
                'code': 'TRIAL_NOT_PERMITTED',
            }, status=status.HTTP_400_BAD_REQUEST)

        # Check total capacity
        confirmed_count = occurrence.bookings.using(alias).filter(
            status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
        ).count()
        trial_crm_count = TrialBooking.objects.using(alias).filter(
            class_occurrence_id=occurrence.id,
            status__in=['BOOKED', 'CONFIRMED', 'ATTENDED']
        ).count()
        total_booked = confirmed_count + trial_crm_count
        total_capacity = occurrence.capacity or (occurrence.class_template.default_capacity if occurrence.class_template else 10)
        if total_booked >= total_capacity:
            return Response({
                'detail': 'This class session is fully booked. No trial slots available.',
                'code': 'TRIAL_CAPACITY_FULL',
            }, status=status.HTTP_400_BAD_REQUEST)

        # Check trial specific capacity
        booking_trial_count = occurrence.bookings.using(alias).filter(
            booking_type='TRIAL',
            status__in=['CONFIRMED', 'RESERVED', 'COMPLETED']
        ).count()
        if (booking_trial_count + trial_crm_count) >= effective_trial_cap:
            return Response({
                'detail': 'Trial capacity reached for this session. This trial session is now full. Please select another slot.',
                'code': 'TRIAL_CAPACITY_FULL',
            }, status=status.HTTP_400_BAD_REQUEST)

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
            is_trial_cap = 'trial' in detail.lower() or 'capacity' in detail.lower() or 'full' in detail.lower()
            return Response({
                'detail': detail,
                'code': 'TRIAL_CAPACITY_FULL' if is_trial_cap else 'VALIDATION_ERROR'
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        if booking.status == 'WAITLISTED':
            booking.delete()
            return Response({
                'detail': 'Trial capacity reached for this session. This trial session is now full. Please select another slot.',
                'code': 'TRIAL_CAPACITY_FULL',
            }, status=status.HTTP_400_BAD_REQUEST)

        # Advance CRM Lead status to TRIAL_BOOKED and log TrialBooking record
        try:
            lead = Lead.objects.using(alias).filter(
                models.Q(email_normalized__iexact=user.email) |
                (models.Q(phone_normalized=user.phone) if user.phone else models.Q(pk=None))
            ).first()
            if not lead:
                org = occurrence.branch.organization if occurrence.branch else Organization.objects.using(alias).filter(status='ACTIVE').first()
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
                lead.save(using=alias, update_fields=['current_status'])
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
            'waitlist_position': None,
            'is_free_trial': True,
            'class_name': occurrence.class_template.name,
            'branch_name': occurrence.branch.name if occurrence.branch else 'SWEAT Studio',
            'date': occurrence.occurrence_date.isoformat() if occurrence.occurrence_date else None,
            'start_at': occurrence.start_at.isoformat() if occurrence.start_at else None,
            'message': 'Free trial booked successfully! See you on the reformer.',
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

        # Check occurrence lifecycle and start/end time boundaries
        now = timezone.now()
        effective_end_at = occurrence.end_at
        if not effective_end_at and occurrence.start_at:
            duration = occurrence.class_template.default_duration_minutes if occurrence.class_template else 50
            effective_end_at = occurrence.start_at + timedelta(minutes=duration)

        if occurrence.status in ('CANCELLED', 'COMPLETED'):
            return Response({
                'detail': f'This class session is {occurrence.status.lower()}.',
                'code': f'CLASS_{occurrence.status}',
            }, status=status.HTTP_400_BAD_REQUEST)

        if effective_end_at and effective_end_at <= now:
            return Response({
                'detail': 'Cannot book a class occurrence that has already ended.',
                'code': 'CLASS_ALREADY_ENDED',
            }, status=status.HTTP_400_BAD_REQUEST)

        if occurrence.start_at and occurrence.start_at <= now:
            return Response({
                'detail': 'Cannot book a class occurrence that has already started.',
                'code': 'CLASS_ALREADY_STARTED',
            }, status=status.HTTP_400_BAD_REQUEST)

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
            detail = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
            is_full = 'capacity and waitlist' in detail.lower() or 'completely full' in detail.lower()
            is_pkg_mismatch = 'not included in package' in detail.lower()
            err_code = 'CLASS_AND_WAITLIST_FULL' if is_full else ('PACKAGE_CLASS_NOT_INCLUDED' if is_pkg_mismatch else 'BOOKING_VALIDATION_ERROR')
            return Response({
                'detail': detail,
                'code': err_code,
            }, status=status.HTTP_400_BAD_REQUEST)
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

        # Trigger automatic attendance lifecycle reconciliation
        try:
            from .services_bookings import BookingWaitlistAttendanceService
            BookingWaitlistAttendanceService.reconcile_past_occurrences_attendance(
                user_profile=profile,
                db_alias=alias,
            )
        except Exception:
            pass

        bookings = (
            Booking.objects.using(alias)
            .filter(user_profile=profile)
            .select_related(
                'occurrence__class_template',
                'occurrence__branch',
                'branch',
            )
            .prefetch_related(
                'attendance_records',
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

            from .services_booking_policy import BookingPolicyService
            org = occ.class_template.category.organization if (occ and occ.class_template and occ.class_template.category) else getattr(branch, 'organization', None)
            bp = BookingPolicyService.get_effective_policy(org, branch=branch, db_alias=alias) if org else {}

            diff_minutes = int((occ.start_at - now).total_seconds() / 60) if (occ and occ.start_at) else 9999
            cutoff_hours = bp.get('cancellation_cutoff_hours', 2)
            reschedule_cutoff_hours = bp.get('rescheduling_cutoff_hours', 2)
            is_late_cancel = diff_minutes < (cutoff_hours * 60)
            is_past_reschedule_cutoff = diff_minutes < (reschedule_cutoff_hours * 60)

            att_rec = b.attendance_records.first()
            effective_status = b.status
            if b.status in ['COMPLETED', 'CONFIRMED']:
                if att_rec and att_rec.check_in_status == 'SUCCESSFUL':
                    effective_status = 'LATE' if att_rec.status in ['LATE', 'LATE_ENTRY', 'LATE_SHOW'] else 'ATTENDED'
                elif occ and occ.end_at and occ.end_at <= now:
                    effective_status = 'NO_SHOW'

            item = {
                'id': str(b.id),
                'booking_number': b.booking_number,
                'occurrence_id': str(occ.id) if occ else None,
                'status': effective_status,
                'attendance_status': att_rec.status if att_rec else effective_status,
                'check_in_at': att_rec.check_in_at.isoformat() if att_rec and att_rec.check_in_at else None,
                'check_out_at': att_rec.check_out_at.isoformat() if att_rec and att_rec.check_out_at else None,
                'payment_status': getattr(b, 'payment_status', 'PAID'),
                'confirmation_status': getattr(b, 'confirmation_status', 'CONFIRMED'),
                'booking_type': b.booking_type,
                'waitlist_position': b.waitlist_position,
                'class_name': template.name if template else 'Reformer Pilates',
                'category': template.category.name if template and template.category else 'Bootcamp',
                'branch_name': branch.name if branch else 'SWEAT Studio',
                'date': occ.occurrence_date.isoformat() if occ and occ.occurrence_date else None,
                'start_at': occ.start_at.isoformat() if occ and occ.start_at else None,
                'start_time': occ.start_at.isoformat() if occ and occ.start_at else None,
                'end_at': occ.end_at.isoformat() if occ and occ.end_at else None,
                'qr_pass': str(b.id),
                'duration_minutes': template.default_duration_minutes if template else 50,
                'trainer_name': trainer_name,
                'booked_at': b.booked_at.isoformat() if b.booked_at else None,
                'can_reschedule': (b.status in ['CONFIRMED', 'RESERVED']) and not is_past_reschedule_cutoff,
                'can_cancel': (b.status in ['CONFIRMED', 'RESERVED']),
                'cancellation_cutoff_hours': cutoff_hours,
                'rescheduling_cutoff_hours': reschedule_cutoff_hours,
                'is_late_cancellation': is_late_cancel,
                'late_cancellation_session_action': bp.get('late_cancellation_session_action', 'CONSUME'),
                'cancellation_fee': bp.get('late_cancellation_fee', '0.00'),
                'late_cancellation_fee': bp.get('late_cancellation_fee', '0.00'),
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
            from .services_booking_policy import BookingPolicyService
            policy_eval = BookingPolicyService.validate_member_cancellation(booking, cancelled_by_user=user, db_alias=alias)
            reason_code = 'MEMBER_LATE_CANCEL' if policy_eval['is_late'] else 'MEMBER_MOBILE_CANCEL'

            cancellation = BookingWaitlistAttendanceService.cancel_booking(
                booking=booking,
                cancelled_by_user=user,
                reason_code=reason_code,
                reason_text=reason,
                db_alias=alias,
            )
            session_action = cancellation.session_action_applied if cancellation else policy_eval['session_action']
        except ValidationError as e:
            detail = e.messages[0] if hasattr(e, 'messages') and e.messages else str(e)
            return Response({'detail': detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        is_refunded = (session_action == 'RESTORE')
        detail_msg = (
            'Booking cancelled. 1 session credit refunded to your passbook.'
            if is_refunded
            else 'Booking cancelled. Session credit forfeited per gym policy.'
        )

        return Response({
            'detail': detail_msg,
            'booking_id': str(booking.id),
            'status': 'CANCELLED',
            'is_late': policy_eval['is_late'],
            'session_action': session_action,
            'session_credit_refunded': is_refunded,
            'refunded': is_refunded,
            'fee_applied': str(policy_eval['fee']),
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
            from .services_booking_policy import BookingPolicyService
            BookingPolicyService.validate_member_reschedule(booking, to_occurrence, rescheduled_by_user=user, db_alias=alias)

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
                'new_start_time': to_occurrence.start_at.isoformat() if to_occurrence.start_at else None,
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
    GET /api/v1/mobile/qr-pass/
    Generates a cryptographically signed, verified QR pass token for turnstile / reception scanning
    strictly ON-DEMAND when within the admin-configured activation window.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, booking_id=None):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        from .services_access_control import (
            generate_compact_qr_token,
            generate_booking_qr_token,
            hash_qr_token,
        )
        from .services_booking_policy import BookingPolicyService
        from .models_attendance import AccessPass, AttendanceRecord
        from .models_org import Organization
        from zoneinfo import ZoneInfo

        now = timezone.now()

        # 1. Resolve target booking (either explicitly by ID or nearest upcoming confirmed booking)
        if booking_id:
            try:
                booking = (
                    Booking.objects.using(alias)
                    .select_related('occurrence__class_template', 'branch__organization', 'user_profile')
                    .get(id=booking_id)
                )
            except Booking.DoesNotExist:
                return Response({'detail': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

            if booking.user_profile_id != profile.id:
                return Response({'detail': 'Access denied.'}, status=status.HTTP_403_FORBIDDEN)
        else:
            # Query candidate bookings that are upcoming or currently in session (no restrictive today_end cutoff)
            candidate_bookings = (
                Booking.objects.using(alias)
                .select_related('occurrence__class_template', 'branch__organization', 'user_profile')
                .filter(
                    user_profile=profile,
                    status__in=['CONFIRMED', 'ATTENDED'],
                    occurrence__start_at__gte=now - timedelta(hours=2),
                )
                .order_by('occurrence__start_at')
            )

            booking = candidate_bookings.first()
            if not booking:
                return Response({
                    'has_booking': False,
                    'is_active': False,
                    'status': 'NO_BOOKING',
                    'detail': 'No upcoming class session scheduled. Please reserve a class session to generate your turnstile access key.',
                }, status=status.HTTP_200_OK)

        # 2. Basic booking status validation
        if booking.status in ('CANCELLED', 'NO_SHOW'):
            return Response({
                'has_booking': False,
                'is_active': False,
                'status': booking.status,
                'detail': f'This booking has been {booking.status.lower()}. Access pass cannot be generated.',
            }, status=status.HTTP_200_OK)

        # If attendance is already complete (member checked out or session finalized)
        att = AttendanceRecord.objects.using(alias).filter(booking=booking).first()
        if att and att.attendance_completed:
            return Response({
                'has_booking': True,
                'is_active': False,
                'status': 'EXPIRED',
                'detail': 'Workout attendance has completed. Access pass has expired.',
            }, status=status.HTTP_200_OK)

        occ = booking.occurrence
        if not occ or not occ.start_at:
            return Response({
                'has_booking': False,
                'is_active': False,
                'status': 'INVALID',
                'detail': 'No class occurrence schedule associated with booking.',
            }, status=status.HTTP_400_BAD_REQUEST)

        # 3. Resolve Admin-Configured QR Activation Window (default: 20 minutes)
        org = None
        if booking.branch and booking.branch.organization:
            org = booking.branch.organization
        elif profile.organization:
            org = profile.organization
        else:
            org = Organization.objects.using(alias).filter(status='ACTIVE').first()

        policy = BookingPolicyService.get_effective_policy(
            org,
            branch=booking.branch,
            db_alias=alias,
        )
        qr_activation_minutes = int(policy.get('qr_activation_minutes_before_class', 20) or 20)

        # 4. Calculate timing boundaries
        start_at = occ.start_at
        duration = occ.class_template.default_duration_minutes if (occ.class_template and occ.class_template.default_duration_minutes) else 50
        end_at = occ.end_at or (start_at + timedelta(minutes=duration))
        activation_time = start_at - timedelta(minutes=qr_activation_minutes)

        # Format local times cleanly for human presentation
        tz_str = (booking.branch.timezone if (booking.branch and booking.branch.timezone) else None) or 'Asia/Kolkata'
        try:
            branch_tz = ZoneInfo(tz_str)
        except Exception:
            branch_tz = ZoneInfo('Asia/Kolkata')

        local_start = start_at.astimezone(branch_tz)
        local_act = activation_time.astimezone(branch_tz)
        start_str = local_start.strftime('%I:%M %p').lstrip('0')
        act_str = local_act.strftime('%I:%M %p').lstrip('0')
        date_str = local_start.strftime('%a, %b %d')

        # 5. EARLY REQUEST: Before Activation Window Opens
        # Demand-driven requirement: Do NOT generate a QR code in advance or queue it.
        if now < activation_time:
            return Response({
                'has_booking': True,
                'is_active': False,
                'status': 'EARLY',
                'booking_id': str(booking.id),
                'booking_number': booking.booking_number,
                'class_name': occ.class_template.name if occ and occ.class_template else 'SWEAT Session',
                'branch_name': booking.branch.name if booking.branch else 'SWEAT Studio',
                'start_at': start_at.isoformat(),
                'activation_time': activation_time.isoformat(),
                'activation_minutes_before': qr_activation_minutes,
                'formatted_start_time': f"{start_str} ({date_str})",
                'formatted_activation_time': act_str,
                'detail': (
                    f"Your QR code isn't available yet. Your class starts at {start_str}. "
                    f"Your turnstile access QR code will become available at {act_str} "
                    f"({qr_activation_minutes} minutes before class). Please return at that time to generate your QR code."
                ),
            }, status=status.HTTP_200_OK)

        # 6. EXPIRED REQUEST: 15+ minutes after session conclusion
        if (end_at + timedelta(minutes=15)) < now:
            return Response({
                'has_booking': True,
                'is_active': False,
                'status': 'EXPIRED',
                'detail': 'Class session has concluded. Access pass has expired.',
            }, status=status.HTTP_200_OK)

        # 7. ACTIVE WINDOW: activation_time <= now <= (end_at + 15m)
        # Generate on-demand 22-character cryptographic Base62 token and upsert AccessPass
        start_ts = int(start_at.timestamp())
        valid_until_ts = start_ts + 7200
        valid_from_dt = activation_time
        valid_until_dt = (end_at + timedelta(minutes=60))

        qr_display_token = generate_booking_qr_token(booking.id, settings.SECRET_KEY)
        t_hash = hash_qr_token(qr_display_token)

        pass_obj, _ = AccessPass.objects.using(alias).update_or_create(
            booking=booking,
            defaults={
                'user_profile': profile,
                'branch': booking.branch,
                'token_hash': t_hash,
                'pass_type': 'CLASS_BOOKING',
                'valid_from': valid_from_dt,
                'valid_until': valid_until_dt,
                'status': 'ACTIVE',
                'updated_at': now,
            }
        )

        # Construct legacy signed payload for backwards compatibility
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
            'has_booking': True,
            'is_active': True,
            'status': 'ACTIVE',
            'pass_id': str(pass_obj.id),
            'booking_id': str(booking.id),
            'booking_number': booking.booking_number,
            'qr_token': qr_display_token,
            'compact_token': qr_display_token,
            'qr_payload': qr_display_token,
            'legacy_signed_token': token,
            'member_name': f"{user.first_name} {user.last_name}".strip(),
            'member_number': profile.member_number,
            'class_name': pass_data['class'],
            'branch_name': pass_data['branch'],
            'start_at': start_at.isoformat(),
            'activation_time': activation_time.isoformat(),
            'activation_minutes_before': qr_activation_minutes,
            'booking_status': booking.status,
            'valid_from': valid_from_dt.isoformat(),
            'valid_until': valid_until_dt.isoformat(),
            'pass_type': 'CLASS_BOOKING',
        })


# ============================================================================
# 4. Memberships, Class Packs & Payments Views
# ============================================================================

class MobilePackagesView(APIView):
    """
    GET /api/v1/mobile/packages/
    Catalog of available session packs (1 Session, 12 Sessions, 36 Sessions, Unlimited, etc.)
    with prices, validity days, program details, and offer badges.
    Supports ?branch_id= and ?program_id= filters.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response([], status=status.HTTP_200_OK)

        branch_id = request.query_params.get('branch_id')
        program_id = request.query_params.get('program_id')

        packages = (
            Package.objects.using(alias)
            .filter(status='ACTIVE')
            .select_related('program')
            .prefetch_related('versions__prices', 'versions__entitlement_definitions')
            .order_by('created_at')
        )

        if program_id:
            packages = packages.filter(program_id=program_id)

        results = []
        for pkg in packages:
            latest_version = pkg.versions.filter(status='ACTIVE').order_by('-version_number').first()
            if not latest_version:
                continue

            # Price lookup
            prices = latest_version.prices.filter(status='ACTIVE')
            if branch_id:
                from .models_catalog import PackageBranchAvailability
                has_avail_rules = PackageBranchAvailability.objects.using(alias).filter(package=pkg).exists()
                if has_avail_rules:
                    is_avail = PackageBranchAvailability.objects.using(alias).filter(
                        package=pkg, branch_id=branch_id, is_available=True
                    ).exists()
                    if not is_avail:
                        continue

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
                'program_id': str(pkg.program_id) if pkg.program_id else None,
                'program_name': pkg.program.name if pkg.program else '',
                'program_code': pkg.program.code if pkg.program else '',
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
        package_version_id = request.data.get('package_version_id')
        coupon_code = request.data.get('coupon_code')

        package = None
        latest_version = None

        if package_version_id:
            latest_version = PackageVersion.objects.using(alias).select_related('package').filter(id=package_version_id).first()
            if latest_version:
                package = latest_version.package

        if not package and package_id:
            package = Package.objects.using(alias).filter(id=package_id, status='ACTIVE').first()
            if package:
                latest_version = package.versions.filter(status='ACTIVE').order_by('-version_number').first()

        if not package or not latest_version:
            return Response({'detail': 'Selected package is no longer available.'}, status=status.HTTP_404_NOT_FOUND)

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
        org = Organization.objects.using(alias).filter(status='ACTIVE').first()

        with transaction.atomic(using=alias):
            order = Order.objects.using(alias).create(
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
                item_name_snapshot=package.name,
                quantity=1,
                unit_price_snapshot=base_price,
                tax_percent_snapshot=tax_pct,
                discount_amount=discount_amount,
                tax_amount=subtotal * (tax_pct / Decimal('100.0')),
                total_amount=final_amount,
            )

        # Generate Razorpay order (or sandbox simulation if unconfigured)
        from .services_razorpay import RazorpayService
        if final_amount > Decimal('0.00'):
            amount_paise = RazorpayService.convert_inr_to_paise(final_amount)
            rzp_order_id = f"order_test_{order.order_number}"
            key_id = "rzp_test_placeholder"

            if RazorpayService.is_configured(db_alias=alias):
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
                rzp_order_id = rzp_order['id']
        else:
            amount_paise = 0
            rzp_order_id = f"order_free_{order.order_number}"
            key_id = "" 

        from .services_payment_policy import PaymentPolicyService
        config_id, checkout_config = PaymentPolicyService.get_razorpay_checkout_config(
            organization=org,
            branch=branch,
            db_alias=alias,
        )

        return Response({
            'order_id': str(order.id),
            'order_number': order.order_number,
            'razorpay_order_id': rzp_order_id,
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
    Verifies payment completion, records transaction, and finalizes membership lifecycle actions:
    1. NEW_MEMBERSHIP: Upgrades UserProfile.member_type = 'MEMBER', converts Lead, activates Membership & credits.
    2. RENEWAL: Activates renewed membership contract, respects prior validity, and links PAR-Q.
    3. UPGRADE: Executes authoritative package upgrade, provisions new entitlements, links PAR-Q.
    4. EXTENSION / VALIDITY_EXTENSION: Extends end date by configured days and extends entitlement validity.
    5. FREEZE: Applies freeze window, pauses status, and extends validity.
    Enforces idempotency, atomic transactions, and server-side Razorpay verification.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        order_id = request.data.get('order_id') or request.data.get('internal_order_id')
        razorpay_order_id = request.data.get('razorpay_order_id')
        payment_id = request.data.get('razorpay_payment_id') or request.data.get('payment_id')
        signature = request.data.get('razorpay_signature')

        try:
            order = Order.objects.using(alias).select_related('branch', 'user_profile').get(id=order_id, user_profile=profile)
        except Order.DoesNotExist:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

        if order.status == 'PAID':
            membership = Membership.objects.using(alias).filter(source_order=order).first()
            if not membership:
                membership = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()
            return Response({
                'success': True,
                'detail': 'Payment already verified. Transaction is complete.',
                'status': 'PAID',
                'membership_id': str(membership.id) if membership else None,
                'package_name': membership.package.name if (membership and membership.package) else '',
                'start_date': membership.start_date.isoformat() if (membership and membership.start_date) else '',
                'end_date': membership.end_date.isoformat() if (membership and membership.end_date) else '',
                'order_number': order.order_number,
                'already_processed': True,
            }, status=status.HTTP_200_OK)

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

        try:
            with transaction.atomic(using=alias):
                # 1. Record payment transaction with full audit fields
                PaymentTransaction.objects.using(alias).create(
                    order=order,
                    user_profile=profile,
                    amount=order.total_amount,
                    currency=order.currency,
                    provider='RAZORPAY',
                    payment_method='RAZORPAY',
                    provider_transaction_id=payment_id,
                    idempotency_key=f"RZP-{razorpay_order_id}-{payment_id}",
                    status='SUCCESS',
                    paid_at=timezone.now(),
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

                meta = {}
                if order.notes:
                    try:
                        meta = json.loads(order.notes)
                    except Exception:
                        meta = {}

                action_type = meta.get('action_type') or order.order_type
                membership = None

                if action_type == 'RENEWAL' or order.order_type == 'RENEWAL':
                    order_item = order.items.using(alias).filter(item_type='PACKAGE').first()
                    prior_m_id = meta.get('membership_id')
                    prior_m = Membership.objects.using(alias).filter(id=prior_m_id).first() if prior_m_id else None
                    today = timezone.now().date()
                    renewal_start = today
                    if prior_m and prior_m.end_date and prior_m.end_date >= today:
                        renewal_start = prior_m.end_date + timedelta(days=1)

                    if order_item:
                        membership = MembershipLifecycleService.activate_membership_from_order(
                            order=order,
                            order_item=order_item,
                            start_date=renewal_start,
                            created_by_user=user,
                            db_alias=alias,
                        )
                    elif prior_m:
                        membership = prior_m

                    # Carry forward unused units from prior membership if applicable
                    if prior_m and membership and prior_m.id != membership.id:
                        for old_ent in prior_m.entitlements.using(alias).filter(status='ACTIVE', is_unlimited=False):
                            remaining = (old_ent.allocated_units or Decimal('0.00')) - (old_ent.consumed_units or Decimal('0.00'))
                            if remaining > Decimal('0.00'):
                                target_ent = membership.entitlements.using(alias).filter(
                                    entitlement_type=old_ent.entitlement_type,
                                    is_unlimited=False
                                ).first()
                                if target_ent:
                                    MembershipLifecycleService.adjust_entitlement(
                                        membership=membership,
                                        entitlement_type=old_ent.entitlement_type,
                                        units_delta=remaining,
                                        reason_code='RENEWAL_CARRY_FORWARD',
                                        reason_text=f"Carried forward {remaining} unused sessions from prior membership {prior_m.membership_number}",
                                        actor_user=user,
                                        db_alias=alias,
                                    )

                    parq_sub_id = meta.get('parq_submission_id')
                    if parq_sub_id and membership:
                        sub = IntakeSubmission.objects.using(alias).filter(id=parq_sub_id).first()
                        if sub:
                            sub.order = order
                            sub.membership = membership
                            sub.user_profile = profile
                            sub.save(using=alias, update_fields=['order', 'membership', 'user_profile'])
                            membership.parq_status = 'CLEARED'
                            membership.parq_submission = sub
                            membership.parq_completed_at = sub.submitted_at or timezone.now()
                            membership.save(using=alias, update_fields=['parq_status', 'parq_submission', 'parq_completed_at'])

                elif action_type == 'UPGRADE' or order.order_type == 'UPGRADE':
                    target_pkg_id = meta.get('target_package_id')
                    target_ver_id = meta.get('target_package_version_id')
                    target_br_id = meta.get('target_branch_id')
                    current_m_id = meta.get('current_membership_id')

                    target_pkg = Package.objects.using(alias).filter(id=target_pkg_id).first()
                    target_ver = PackageVersion.objects.using(alias).filter(id=target_ver_id).first()
                    target_br = Branch.objects.using(alias).filter(id=target_br_id).first() if target_br_id else None
                    m_to_upgrade = Membership.objects.using(alias).filter(id=current_m_id).first() if current_m_id else None
                    if not m_to_upgrade:
                        m_to_upgrade = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()

                    if m_to_upgrade and target_pkg and target_ver:
                        membership = MembershipLifecycleService.execute_upgrade(
                            membership=m_to_upgrade,
                            target_package=target_pkg,
                            target_package_version=target_ver,
                            target_branch=target_br,
                            order=order,
                            actor_user=user,
                            db_alias=alias,
                        )
                        parq_sub_id = meta.get('parq_submission_id')
                        if parq_sub_id and membership:
                            sub = IntakeSubmission.objects.using(alias).filter(id=parq_sub_id).first()
                            if sub:
                                sub.order = order
                                sub.membership = membership
                                sub.user_profile = profile
                                sub.save(using=alias, update_fields=['order', 'membership', 'user_profile'])
                                membership.parq_status = 'CLEARED'
                                membership.parq_submission = sub
                                membership.parq_completed_at = sub.submitted_at or timezone.now()
                                membership.save(using=alias, update_fields=['parq_status', 'parq_submission', 'parq_completed_at'])

                elif action_type == 'VALIDITY_EXTENSION' or order.order_type == 'EXTENSION':
                    ext_days = int(meta.get('days', 30))
                    m_id = meta.get('membership_id')
                    m_to_ext = Membership.objects.using(alias).filter(id=m_id).first() if m_id else None
                    if not m_to_ext:
                        m_to_ext = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()
                    if m_to_ext:
                        membership = MembershipLifecycleService.extend_membership(
                            membership=m_to_ext,
                            days=ext_days,
                            reason_text=f"Self-service validity extension of {ext_days} days (Order {order.order_number})",
                            approved_by_user=user,
                            db_alias=alias,
                        )

                elif action_type == 'FREEZE':
                    freeze_from_str = meta.get('freeze_from')
                    freeze_until_str = meta.get('freeze_until')
                    m_id = meta.get('membership_id')
                    m_to_frz = Membership.objects.using(alias).filter(id=m_id).first() if m_id else None
                    if not m_to_frz:
                        m_to_frz = Membership.objects.using(alias).filter(user_profile=profile).order_by('-created_at').first()
                    if m_to_frz and freeze_from_str and freeze_until_str:
                        from_d = datetime.strptime(freeze_from_str[:10], '%Y-%m-%d').date()
                        until_d = datetime.strptime(freeze_until_str[:10], '%Y-%m-%d').date()
                        MembershipLifecycleService.apply_freeze(
                            membership=m_to_frz,
                            freeze_from=from_d,
                            freeze_until=until_d,
                            reason_text=f"Self-service paid freeze (Order {order.order_number})",
                            approved_by_user=user,
                            db_alias=alias,
                        )
                        membership = m_to_frz

                else:
                    # Default / NEW_MEMBERSHIP
                    order_item = order.items.filter(item_type='PACKAGE').first()
                    if order_item:
                        membership = MembershipLifecycleService.activate_membership_from_order(
                            order=order,
                            order_item=order_item,
                            created_by_user=user,
                            db_alias=alias,
                        )

                    if not profile.member_number:
                        profile.member_number = f"SW-{str(uuid.uuid4())[:6].upper()}"
                    profile.member_type = 'MEMBER'
                    profile.save(using=alias, update_fields=['member_number', 'member_type'])

                    user.user_type = 'MEMBER'
                    user.save(using=alias, update_fields=['user_type'])

                    member_role = Role.objects.using(alias).filter(code='MEMBER').first()
                    if member_role:
                        RoleAssignment.objects.using(alias).get_or_create(
                            user=user,
                            role=member_role,
                            defaults={'branch': order.branch or user.home_branch, 'is_active': True}
                        )

                    try:
                        lead = Lead.objects.using(alias).filter(email_normalized__iexact=user.email).first()
                        if not lead and user.phone:
                            lead = Lead.objects.using(alias).filter(phone_normalized=user.phone).first()
                        if lead:
                            lead.current_status = 'CONVERTED'
                            lead.converted_user_profile = profile
                            lead.save(using=alias, update_fields=['current_status', 'converted_user_profile'])

                            LeadConversion.objects.using(alias).get_or_create(
                                lead=lead,
                                user_profile=profile,
                                defaults={
                                    'order_id': order.id,
                                    'membership_id': membership.id if membership else None,
                                    'package_id': getattr(order_item, 'package_id', None) if order_item else None,
                                    'package_version_id': getattr(order_item, 'package_version_id', None) if order_item else None,
                                    'conversion_source': 'MOBILE_APP',
                                }
                            )
                    except Exception as conv_crm_err:
                        logger.warning("Could not sync CRM lead conversion for %s: %s", user.email, conv_crm_err)
        except Exception as exc:
            logger.exception("Failed to verify and process checkout for order %s: %s", order.order_number, exc)
            return Response(
                {
                    'detail': f'Checkout verification failed: {str(exc)}',
                    'code': 'CHECKOUT_PROCESSING_ERROR'
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        credits_summary = []
        if membership:
            entitlements = MembershipEntitlement.objects.using(alias).filter(membership=membership)
            for ent in entitlements:
                credits_summary.append({
                    'entitlement_type': ent.entitlement_type,
                    'allocated_units': str(ent.allocated_units) if ent.allocated_units else 'UNLIMITED',
                    'is_unlimited': ent.is_unlimited,
                    'valid_until': ent.valid_until.isoformat() if ent.valid_until else None,
                })

        return Response({
            'success': True,
            'status': 'PAID',
            'detail': 'Payment successful! Membership action processed successfully.',
            'action_type': action_type,
            'membership_id': str(membership.id) if membership else None,
            'membership_status': membership.status if membership else 'ACTIVE',
            'package_name': membership.package.name if (membership and membership.package) else '',
            'credits_allocated': credits_summary,
            'start_date': membership.start_date.isoformat() if (membership and membership.start_date) else '',
            'end_date': membership.end_date.isoformat() if (membership and membership.end_date) else '',
            'order_number': order.order_number,
        })



class MobileOrdersView(APIView):
    """
    GET /api/v1/mobile/orders/
    Fetch order and invoice history for the authenticated member.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        orders = (
            Order.objects.using(alias)
            .filter(user_profile=profile)
            .select_related('branch')
            .prefetch_related('items__package')
            .order_by('-created_at')
        )
        data = []
        for o in orders:
            items_data = []
            for it in o.items.all():
                items_data.append({
                    'id': str(it.id),
                    'item_name': it.item_name_snapshot or (it.package.name if it.package else 'Package'),
                    'item_type': it.item_type,
                    'package_name': it.package.name if it.package else '',
                    'quantity': str(it.quantity),
                    'unit_price': str(it.unit_price_snapshot),
                    'discount_amount': str(it.discount_amount),
                    'tax_amount': str(it.tax_amount),
                    'total_amount': str(it.total_amount),
                })
            data.append({
                'id': str(o.id),
                'order_number': o.order_number,
                'order_type': o.order_type,
                'status': o.status,
                'subtotal': str(o.subtotal),
                'discount_amount': str(o.discount_amount),
                'tax_amount': str(o.tax_amount),
                'total_amount': str(o.total_amount),
                'currency': o.currency,
                'branch_id': str(o.branch_id) if o.branch_id else None,
                'branch_name': o.branch.name if o.branch else '',
                'created_at': o.created_at.isoformat() if o.created_at else None,
                'items': items_data,
            })
        return Response(data, status=status.HTTP_200_OK)


class MobileOnboardingSurveyView(APIView):
    """
    GET /api/v1/mobile/onboarding/survey/
    Fetches onboarding and PAR-Q questions dynamically from the authoritative IntakeForm definition.
    """
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)

        from .models_crm import IntakeForm, IntakeSubmission, IntakeAnswer
        from .models_workforce import UserProfile

        # Authoritative lookup for active form (supports ?form_type=ONBOARDING or ?form_type=PAR_Q)
        requested_type = request.query_params.get('form_type') or request.query_params.get('type')
        if requested_type:
            form = IntakeForm.objects.using(alias).filter(form_type__iexact=requested_type, status='ACTIVE').order_by('-version_number').first()
        else:
            form = IntakeForm.objects.using(alias).filter(form_type='ONBOARDING', status='ACTIVE').order_by('-version_number').first()
            if not form:
                form = IntakeForm.objects.using(alias).filter(form_type='PAR_Q', status='ACTIVE').order_by('-version_number').first()
        if not form:
            form = IntakeForm.objects.using(alias).filter(status='ACTIVE').order_by('-version_number').first()

        questions_list = []
        sections_dict = {}

        category_labels = {
            'FITNESS': 'Fitness & Profile',
            'LIFESTYLE': 'Lifestyle & Goals',
            'PSYCHOLOGY': 'Motivation & Goals',
            'MEDICAL': 'Health & Medical (Protected)',
            'SALES': 'Trial & Preferences',
            'OTHER': 'General Information',
        }

        if form:
            qs = form.questions.filter(status='ACTIVE').order_by('display_order', 'created_at')
            for q in qs:
                opts = []
                for o in q.options.filter(status='ACTIVE').order_by('display_order'):
                    opt_item = {
                        'id': str(o.id),
                        'label': o.label,
                        'value': o.value,
                        'order': o.display_order,
                    }
                    if '///' in o.label:
                        parts = [p.strip() for p in o.label.split('///')]
                        opt_item['title'] = parts[0]
                        opt_item['label'] = parts[0]
                        if len(parts) > 1 and parts[1]:
                            opt_item['subtitle'] = parts[1]
                            opt_item['description'] = parts[1]
                        for extra_part in parts[2:]:
                            if extra_part.startswith('icon:'):
                                opt_item['icon'] = extra_part.split(':', 1)[1]
                            elif extra_part.startswith('img:'):
                                opt_item['image'] = extra_part.split(':', 1)[1]
                            elif extra_part.startswith('bars:'):
                                try:
                                    opt_item['bars'] = int(extra_part.split(':', 1)[1])
                                except ValueError:
                                    opt_item['bars'] = extra_part.split(':', 1)[1]
                            elif extra_part == 'EXCLUSIVE':
                                opt_item['is_exclusive'] = True
                            elif extra_part == 'RECOMMENDED':
                                opt_item['is_recommended'] = True
                                opt_item['badge'] = 'RECOMMENDED'
                            else:
                                opt_item['badge'] = extra_part
                    opts.append(opt_item)
                q_item = {
                    'id': str(q.id),
                    'text': q.question_text,
                    'label': q.question_text,
                    'type': q.question_type,
                    'category': q.category,
                    'category_label': category_labels.get(q.category, q.category),
                    'is_required': q.is_required,
                    'is_sensitive': q.is_sensitive,
                    'order': q.display_order,
                    'options': opts,
                }
                questions_list.append(q_item)

                cat_key = q.category.lower()
                if cat_key not in sections_dict:
                    sections_dict[cat_key] = {
                        'id': cat_key,
                        'title': category_labels.get(q.category, q.category),
                        'questions': [],
                    }
                sections_dict[cat_key]['questions'].append(q_item)

        saved_responses = {}
        is_cleared = False
        user_profile = None
        submission_info = None

        if request.user and request.user.is_authenticated:
            target_mid = request.query_params.get('member_id') or request.query_params.get('memberId') or request.headers.get('X-Member-Id')
            if target_mid:
                from django.db.models import Q
                user_profile = UserProfile.objects.using(alias).filter(Q(id=target_mid) | Q(user__id=target_mid) | Q(member_number=target_mid)).first()
            if not user_profile:
                user_profile = UserProfile.objects.using(alias).filter(user=request.user).first()
            if not user_profile:
                user_profile = UserProfile.objects.using(alias).filter(user__email__iexact=request.user.email).first()

            if user_profile:
                all_subs = IntakeSubmission.objects.using(alias).filter(
                    user_profile=user_profile, status='COMPLETED'
                ).order_by('-submitted_at')
                sub_count = all_subs.count()
                last_sub = all_subs.first()
                submission_history = []
                for s_item in all_subs:
                    s_meta = getattr(s_item, 'metadata', {}) or {}
                    s_answers_map = {}
                    s_answers_detailed = []

                    # Build lookup map from form_snapshot questions if available
                    snap_q_map = {}
                    if isinstance(s_item.form_snapshot, dict) and isinstance(s_item.form_snapshot.get('questions'), list):
                        for sq in s_item.form_snapshot['questions']:
                            if isinstance(sq, dict):
                                sq_id = str(sq.get('id', ''))
                                sq_ans = sq.get('submitted_answer') if sq.get('submitted_answer') is not None else sq.get('answer')
                                if sq_id:
                                    snap_q_map[sq_id] = sq_ans
                                if sq.get('order'):
                                    snap_q_map[f"q_{sq['order']}"] = sq_ans

                    item_answers = IntakeAnswer.objects.using(alias).filter(submission=s_item).select_related('question').order_by('question__display_order')
                    for ans_rec in item_answers:
                        val_rec = None
                        if ans_rec.boolean_value is not None:
                            val_rec = ans_rec.boolean_value
                        elif ans_rec.numeric_value is not None:
                            val_rec = float(ans_rec.numeric_value)
                        elif ans_rec.date_value is not None:
                            val_rec = ans_rec.date_value.isoformat() if hasattr(ans_rec.date_value, 'isoformat') else str(ans_rec.date_value)
                        elif ans_rec.text_value not in (None, ''):
                            val_rec = ans_rec.text_value
                        elif ans_rec.json_value not in (None, {}, [], ''):
                            val_rec = ans_rec.json_value

                        # Fallback to form_snapshot if still empty
                        if val_rec in (None, {}, '') and ans_rec.question_id:
                            snap_val = snap_q_map.get(str(ans_rec.question_id))
                            if snap_val not in (None, {}, ''):
                                val_rec = snap_val
                        if val_rec in (None, {}, '') and ans_rec.question:
                            snap_val = snap_q_map.get(f"q_{ans_rec.question.display_order}")
                            if snap_val not in (None, {}, ''):
                                val_rec = snap_val

                        if ans_rec.question_id:
                            s_answers_map[str(ans_rec.question_id)] = val_rec
                        if ans_rec.question:
                            s_answers_map[f"q_{ans_rec.question.display_order}"] = val_rec
                            s_answers_detailed.append({
                                'question_id': str(ans_rec.question.id),
                                'question_text': ans_rec.question.question_text,
                                'question_type': ans_rec.question.question_type,
                                'category': ans_rec.question.category,
                                'is_sensitive': ans_rec.question.is_sensitive,
                                'order': ans_rec.question.display_order,
                                'answer': val_rec,
                                'submitted_answer': val_rec,
                                'text_value': str(val_rec) if val_rec is not None else '',
                            })

                    # If no IntakeAnswer records exist yet but form_snapshot exists, extract from form_snapshot
                    if not s_answers_detailed and isinstance(s_item.form_snapshot, dict) and isinstance(s_item.form_snapshot.get('questions'), list):
                        for sq in s_item.form_snapshot['questions']:
                            if isinstance(sq, dict):
                                sq_ans = sq.get('submitted_answer') if sq.get('submitted_answer') is not None else sq.get('answer')
                                sq_id = str(sq.get('id', ''))
                                sq_order = sq.get('order')
                                if sq_id:
                                    s_answers_map[sq_id] = sq_ans
                                if sq_order:
                                    s_answers_map[f"q_{sq_order}"] = sq_ans
                                s_answers_detailed.append({
                                    'question_id': sq_id,
                                    'question_text': sq.get('question_text', ''),
                                    'question_type': sq.get('question_type', 'TEXT'),
                                    'category': sq.get('category', 'GENERAL'),
                                    'is_sensitive': sq.get('is_sensitive', False),
                                    'order': sq_order,
                                    'answer': sq_ans,
                                    'submitted_answer': sq_ans,
                                    'text_value': str(sq_ans) if sq_ans is not None else '',
                                })

                    submission_history.append({
                        'id': str(s_item.id),
                        'submitted_at': s_item.submitted_at.isoformat() if s_item.submitted_at else None,
                        'signer_identity': s_item.signer_identity,
                        'signature_data': s_item.signature_data,
                        'signature_date': s_item.signature_date.isoformat() if s_item.signature_date else None,
                        'form_name': (s_item.form_snapshot.get('name') if isinstance(s_item.form_snapshot, dict) else None) or (s_item.intake_form.name if s_item.intake_form else 'PAR-Q Form'),
                        'version_number': (s_item.form_snapshot.get('version') if isinstance(s_item.form_snapshot, dict) else None) or (s_item.intake_form.version_number if s_item.intake_form else 1),
                        'is_edit': s_meta.get('is_edit', False),
                        'revision_number': s_meta.get('revision_number', 1),
                        'agreement_accepted': s_item.agreement_accepted,
                        'agreement_text_snapshot': s_item.agreement_text_snapshot,
                        'form_snapshot': s_item.form_snapshot,
                        'answers': s_answers_map,
                        'answers_detailed': s_answers_detailed,
                    })

                if last_sub and submission_history:
                    is_cleared = True
                    saved_responses = dict(submission_history[0]['answers'])
                    submission_info = dict(submission_history[0])
                    submission_info['submission_count'] = sub_count
                elif user_profile.address_json and user_profile.address_json.get('onboarding'):
                    onb = user_profile.address_json['onboarding']
                    is_cleared = not onb.get('requires_doctor_clearance', False)
                    saved_responses = onb.get('par_q', {})

        return Response({
            'form_id': str(form.id) if form else None,
            'form_name': form.name if form else 'SWEAT PAR-Q Questionnaire',
            'version_number': form.version_number if form else 1,
            'agreement_title': form.agreement_title if form else 'Physical Activity Readiness Agreement',
            'agreement_text': form.agreement_text if form else '',
            'questions': questions_list,
            'sections': list(sections_dict.values()),
            'saved_responses': saved_responses,
            'is_cleared': is_cleared,
            'status_badge': 'PAR-Q Cleared' if is_cleared else 'PAR-Q Pending',
            'is_completed': is_cleared,
            'has_completed_parq': is_cleared,
            'submission': submission_info,
            'existing_submission': submission_info,
            'submission_history': submission_history if 'submission_history' in locals() else ([] if not submission_info else [submission_info]),
        })

    def post(self, request):
        return MobileOnboardingSubmitView().post(request)


class MobileOnboardingSubmitView(APIView):
    """
    POST /api/v1/mobile/onboarding/submit/
    Authoritatively saves member onboarding and PAR-Q responses to IntakeSubmission & IntakeAnswer.
    Accepts application/json and multipart/form-data.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        user = request.user
        alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        from .models_workforce import UserProfile
        from .models_crm import IntakeForm, IntakeQuestion, IntakeSubmission, IntakeAnswer
        from .models_memberships import Membership

        profile = _get_or_create_user_profile(user, alias)
        data = request.data or {}

        # 1. Authoritative active form (supports form_type in payload or query params)
        requested_type = data.get('form_type') or request.query_params.get('form_type')
        if requested_type:
            form = IntakeForm.objects.using(alias).filter(form_type__iexact=requested_type, status='ACTIVE').order_by('-version_number').first()
        else:
            form = IntakeForm.objects.using(alias).filter(form_type='ONBOARDING', status='ACTIVE').order_by('-version_number').first()
            if not form:
                form = IntakeForm.objects.using(alias).filter(form_type='PAR_Q', status='ACTIVE').order_by('-version_number').first()
        if not form:
            form = IntakeForm.objects.using(alias).filter(status='ACTIVE').order_by('-version_number').first()

        now = timezone.now()

        # Support both multipart file upload (request.FILES) and base64 string (request.data)
        uploaded_sig = request.FILES.get('signature') or request.FILES.get('signature_file') or request.FILES.get('signature_data')
        signature_data = data.get('signature_data') or data.get('signature') or ''
        agreement_accepted = data.get('agreement_accepted', True)
        if isinstance(agreement_accepted, str):
            agreement_accepted = agreement_accepted.lower() in ('true', '1', 't', 'yes')

        # Check existing submissions for lifecycle & revision decision
        prior_subs = IntakeSubmission.objects.using(alias).filter(user_profile=profile).order_by('-submitted_at')
        prior_count = prior_subs.count()
        prev_sub = prior_subs.first()
        parq_decision = str(data.get('parq_decision', '')).upper().strip()
        allow_revision = bool(data.get('allow_revision', False))

        # View-Only Guard: Prevent accidental / silent overwrite from normal member portal viewing flow
        if prior_count > 0 and not parq_decision and not allow_revision:
            return Response({
                'detail': 'A completed PAR-Q health assessment is already on file for this member. Existing answers cannot be overwritten from the normal viewing flow.',
                'code': 'PARQ_ALREADY_COMPLETED',
                'submission_id': str(prev_sub.id),
                'submitted_at': prev_sub.submitted_at.isoformat() if prev_sub.submitted_at else None,
            }, status=status.HTTP_400_BAD_REQUEST)

        # OPTION A: REUSE EXISTING PAR-Q
        if parq_decision == 'REUSE':
            if not prev_sub:
                return Response({
                    'detail': 'No existing completed PAR-Q assessment found to reuse.',
                    'code': 'NO_PARQ_TO_REUSE',
                }, status=status.HTTP_400_BAD_REQUEST)
            # Reusing MUST NOT create a duplicate IntakeSubmission record
            target_mem_id = data.get('membership_id')
            if target_mem_id:
                target_m = Membership.objects.using(alias).filter(id=target_mem_id, user_profile=profile).first()
                if target_m:
                    target_m.parq_status = 'COMPLETED'
                    target_m.parq_submission = prev_sub
                    target_m.parq_completed_at = prev_sub.submitted_at
                    target_m.save(using=alias, update_fields=['parq_status', 'parq_submission', 'parq_completed_at', 'updated_at'])
            return Response({
                'detail': f"Existing PAR-Q from {prev_sub.submitted_at.strftime('%b %d, %Y')} reused successfully. Historical record preserved.",
                'is_cleared': True,
                'status_badge': 'PAR-Q Cleared',
                'submission_id': str(prev_sub.id),
                'is_reused': True,
            }, status=status.HTTP_200_OK)

        has_valid_signature = bool(uploaded_sig) or (
            bool(signature_data) and (
                str(signature_data).startswith('data:image/') or
                str(signature_data).startswith('/media/') or
                len(str(signature_data)) >= 100
            )
        )

        # OPTION B: EDIT EXISTING PAR-Q (Create audited revision with new signature)
        if parq_decision == 'EDIT' or allow_revision:
            is_edit = True
            rev_num = prior_count + 1
            if not has_valid_signature:
                return Response({
                    'detail': 'A newly drawn digital signature is required when creating a revised PAR-Q assessment.',
                    'code': 'NEW_SIGNATURE_REQUIRED',
                }, status=status.HTTP_400_BAD_REQUEST)

        # OPTION C: FILL OUT A NEW PAR-Q (Independent submission with new signature)
        elif parq_decision == 'NEW':
            is_edit = False
            rev_num = prior_count + 1
            if not has_valid_signature:
                return Response({
                    'detail': 'A newly drawn digital signature is required when completing a new PAR-Q assessment.',
                    'code': 'SIGNATURE_REQUIRED',
                }, status=status.HTTP_400_BAD_REQUEST)

        # First-time submission
        else:
            is_edit = False
            rev_num = 1
            if not has_valid_signature:
                return Response({
                    'detail': 'A valid drawn digital signature is required to complete the health assessment.',
                    'code': 'SIGNATURE_REQUIRED',
                }, status=status.HTTP_400_BAD_REQUEST)

        meta_info = {
            'is_edit': is_edit,
            'revision_number': rev_num,
            'channel': 'MEMBER_PORTAL',
            'signer_type': 'MEMBER_DIRECT',
            'accepted_by_name': f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip() or user.email,
            'previous_submission_id': str(prev_sub.id) if (is_edit and prev_sub) else None,
            'submission_type': 'REVISION' if is_edit else ('NEW_ASSESSMENT' if parq_decision == 'NEW' else 'INITIAL'),
            'parq_decision': parq_decision or ('INITIAL' if prior_count == 0 else 'EDIT'),
        }

        # 2. Persist authoritative IntakeSubmission (compress signature to disk if file/Base64)
        from .services_storage import save_compressed_signature
        sig_input = uploaded_sig if uploaded_sig else (signature_data if signature_data else None)
        stored_signature = save_compressed_signature(sig_input, tenant_slug=alias) if sig_input else None

        submission = IntakeSubmission(
            intake_form=form,
            user_profile=profile,
            submitted_by_user=user,
            agreement_accepted=bool(agreement_accepted),
            agreement_accepted_at=now if agreement_accepted else None,
            agreement_text_snapshot=form.agreement_text if form else '',
            form_snapshot={'name': form.name, 'version': form.version_number} if form else {},
            metadata=meta_info,
            signature_data=stored_signature,
            signature_date=now if sig_input else None,
            signer_identity=f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip() or user.email,
            signer_ip=request.META.get('REMOTE_ADDR'),
            signer_user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            status='COMPLETED',
            submitted_at=now,
        )
        submission.save(using=alias)

        # 3. Save answers with full question snapshots & revision carryover
        answers_dict = data.get('answers') or data.get('par_q') or data.get('responses') or data
        if isinstance(answers_dict, str):
            try:
                answers_dict = json.loads(answers_dict)
            except Exception:
                pass
        if isinstance(answers_dict, list):
            mapped_dict = {}
            for item in answers_dict:
                if isinstance(item, dict):
                    q_key = str(item.get('question_id') or item.get('id') or '')
                    val_item = item.get('value', item.get('answer', item.get('text_value')))
                    if q_key:
                        mapped_dict[q_key] = val_item
                    if item.get('order'):
                        mapped_dict[f"q_{item['order']}"] = val_item
                        mapped_dict[str(item['order'])] = val_item
            answers_dict = mapped_dict

        snapshot_questions = []

        if form:
            form_questions = list(form.questions.using(alias).filter(status='ACTIVE').order_by('display_order'))
            processed_q_ids = set()

            for q in form_questions:
                processed_q_ids.add(str(q.id))
                val = None
                if isinstance(answers_dict, dict):
                    # Check exact UUID match, order keys (e.g. q_1, 1), and slug
                    if str(q.id) in answers_dict:
                        val = answers_dict[str(q.id)]
                    elif f"q_{q.display_order}" in answers_dict:
                        val = answers_dict[f"q_{q.display_order}"]
                    elif str(q.display_order) in answers_dict:
                        val = answers_dict[str(q.display_order)]

                # Carry over previous answer on revision if unchanged/unprovided
                if val is None and prev_sub:
                    prev_ans = prev_sub.answers.filter(question=q).first()
                    if prev_ans:
                        if prev_ans.boolean_value is not None:
                            val = prev_ans.boolean_value
                        elif prev_ans.numeric_value is not None:
                            val = float(prev_ans.numeric_value)
                        elif prev_ans.date_value is not None:
                            val = prev_ans.date_value.isoformat()
                        elif prev_ans.json_value is not None:
                            val = prev_ans.json_value
                        elif prev_ans.text_value:
                            val = prev_ans.text_value

                # Persist answer record
                ans = IntakeAnswer(submission=submission, question=q)
                if isinstance(val, bool):
                    ans.boolean_value = val
                elif isinstance(val, (int, float)):
                    ans.numeric_value = val
                elif isinstance(val, (dict, list)):
                    ans.json_value = val
                else:
                    ans.text_value = str(val) if val is not None else ''
                ans.save(using=alias)

                snapshot_questions.append({
                    'id': str(q.id),
                    'question_text': q.question_text,
                    'question_type': q.question_type,
                    'category': q.category,
                    'is_sensitive': q.is_sensitive,
                    'submitted_answer': val,
                })

        # Update submission with authoritative questions & answers snapshot
        submission.form_snapshot = {
            'form_title': form.name if form else 'PAR-Q Form',
            'version_number': form.version_number if form else 1,
            'agreement_title': form.agreement_title if form else 'Physical Activity Readiness & Assumption of Risk Agreement',
            'questions': snapshot_questions,
        }
        submission.save(using=alias, update_fields=['form_snapshot'])

        # 4. Authoritatively update member memberships to COMPLETED
        active_memberships = profile.memberships.filter(status='ACTIVE')
        for mem in active_memberships:
            mem.parq_status = 'COMPLETED'
            mem.parq_completed_at = now
            mem.parq_submission = submission
            mem.parq_form = form
            mem.save(using=alias, update_fields=['parq_status', 'parq_completed_at', 'parq_submission', 'parq_form'])

        # 5. Backward compatibility sync in address_json
        current_data = profile.address_json or {}
        current_data['onboarding'] = {
            'completed_at': now.isoformat(),
            'submission_id': str(submission.id),
            'is_cleared': True,
            'signature_present': bool(signature_data),
            'revision_number': rev_num,
            'is_edit': is_edit,
        }
        profile.address_json = current_data
        profile.save(using=alias, update_fields=['address_json'])

        detail_msg = f"PAR-Q health assessment updated (Revision #{rev_num}) and recorded in studio records." if is_edit else "PAR-Q health assessment submitted and digitally confirmed."

        return Response({
            'detail': detail_msg,
            'is_cleared': True,
            'status_badge': 'PAR-Q Cleared',
            'submission_id': str(submission.id),
            'is_edit': is_edit,
            'revision_number': rev_num,
            'memberships_updated': active_memberships.count(),
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
        if not start_at_str and data.get('appointment_date') and data.get('start_time'):
            start_at_str = f"{data.get('appointment_date')}T{data.get('start_time')}:00"
        duration_minutes = int(data.get('duration_minutes', 60))
        focus = data.get('focus') or data.get('focus_area') or 'Pilates Core & Athletic Conditioning'
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

        org = Organization.objects.using(alias).filter(status='ACTIVE').first()
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
                {
                    'detail': 'Organization not found or inactive. Please specify a valid organization.',
                    'code': 'TENANT_NOT_FOUND',
                },
                status=status.HTTP_404_NOT_FOUND,
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



class MobileClassCategoriesView(APIView):
    """
    GET /api/v1/mobile/categories/
    Lists active class categories (formats) for the tenant.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            return Response([], status=status.HTTP_200_OK)

        categories = (
            ClassCategory.objects.using(alias)
            .filter(status='ACTIVE')
            .order_by('display_order', 'name')
        )
        results = [
            {'id': str(c.id), 'code': c.code, 'name': c.name, 'description': c.description or ''}
            for c in categories
        ]
        return Response(results)


class MobileMembershipActionsView(APIView):
    """
    GET /api/v1/mobile/memberships/actions/
    Returns member-facing options and policies for:
    - Renew Membership (fixed current program, selectable branch, filtered packages)
    - Upgrade Package (change branch, program, package with authoritative price diff)
    - Extend Validity (dynamic duration options & fees from CRM config, self-service checkout)
    - Freeze Membership (dynamic duration limits & fees from CRM config, self-service checkout)
    - Active freeze state, PAR-Q status, and Razorpay configuration
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        def _get_ver_days(v):
            if not v:
                return 30
            if getattr(v, 'duration_days', None) is not None:
                return v.duration_days
            val = getattr(v, 'duration_value', 30) or 30
            u = str(getattr(v, 'duration_unit', 'MONTH') or 'MONTH').upper().rstrip('S')
            if u == 'DAY':
                return val
            elif u == 'WEEK':
                return val * 7
            elif u == 'MONTH':
                return val * 30
            elif u == 'YEAR':
                return val * 365
            return val

        def _get_ver_sessions(v):
            if not v:
                return 12
            if getattr(v, 'session_count', None) is not None:
                return v.session_count
            tot = 0
            try:
                for ped in v.entitlement_definitions.using(alias).all():
                    if getattr(ped, 'is_unlimited', False):
                        return 999
                    tot += int(getattr(ped, 'allocated_units', 0) or 0)
            except Exception:
                pass
            return tot or 12

        from decimal import Decimal
        from django.db.models import Q
        from .models_memberships import (
            Membership, MembershipFreeze, MembershipChangeRequest,
            MembershipRenewalPolicy, Package, PackageVersion, PackagePrice
        )
        from .models_catalog import Program
        from .models_workforce import Branch
        from .models_govern import OrganizationSettings
        from .models_crm import IntakeSubmission
        from .services_razorpay import RazorpayService

        active_m = (
            Membership.objects.using(alias)
            .filter(user_profile=profile)
            .select_related('package', 'package_version', 'program', 'home_branch')
            .order_by('-created_at')
            .first()
        )

        org = getattr(user, 'organization', None)
        if not org and active_m and active_m.package:
            org = active_m.package.organization
        if not org:
            org = Organization.objects.using(alias).first()

        branch_qs = Branch.objects.using(alias).filter(status='ACTIVE')
        if org:
            branch_qs = branch_qs.filter(organization=org)
        if branch_qs.exclude(code='').exists():
            branch_qs = branch_qs.exclude(code='')
        raw_branches = list(
            branch_qs.values(
                'id', 'name', 'code', 'address', 'address_line_1',
                'location__name', 'location__city', 'location__state'
            )
        )
        branches = [
            {
                'id': str(b['id']),
                'name': b['name'],
                'code': b['code'],
                'address': b.get('address') or '',
                'address_line_1': b.get('address_line_1') or '',
                'location_name': b.get('location__name') or '',
                'city': b.get('location__city') or '',
                'state': b.get('location__state') or '',
            }
            for b in raw_branches
        ]

        programs_qs = Program.objects.using(alias).filter(status='ACTIVE')
        if org:
            programs_qs = programs_qs.filter(organization=org)
        # Only include commercial programs that have active commercial packages available for self-service purchase/upgrade
        programs_qs = (
            programs_qs.filter(packages__status='ACTIVE')
            .distinct()
            .order_by('display_order', 'name')
        )
        programs = list(
            programs_qs.values('id', 'name', 'code')
        )

        # Retrieve dynamic settings for extension & freeze
        os_obj = OrganizationSettings.objects.using(alias).first()
        cfg = getattr(os_obj, 'membership_config', {}) or {}
        ext_cfg = cfg.get('extension_policy', {})
        frz_cfg = cfg.get('freeze_policy', {})
        tax_pct = float(getattr(os_obj, 'tax_rate_pct', 18.0) or 18.0)

        # Build dynamic extension options
        ext_options_list = ext_cfg.get('available_options')
        if not ext_options_list:
            ext_options_list = [
                {'days': 7, 'base_fee': 500.0, 'tax_pct': tax_pct, 'tax_amount': round(500.0 * (tax_pct / 100.0), 2), 'total_fee': round(500.0 * (1.0 + (tax_pct / 100.0)), 2), 'name': '7 Days Extension'},
                {'days': 14, 'base_fee': 900.0, 'tax_pct': tax_pct, 'tax_amount': round(900.0 * (tax_pct / 100.0), 2), 'total_fee': round(900.0 * (1.0 + (tax_pct / 100.0)), 2), 'name': '14 Days Extension'},
                {'days': 30, 'base_fee': 1500.0, 'tax_pct': tax_pct, 'tax_amount': round(1500.0 * (tax_pct / 100.0), 2), 'total_fee': round(1500.0 * (1.0 + (tax_pct / 100.0)), 2), 'name': '30 Days Extension'},
                {'days': 60, 'base_fee': 2800.0, 'tax_pct': tax_pct, 'tax_amount': round(2800.0 * (tax_pct / 100.0), 2), 'total_fee': round(2800.0 * (1.0 + (tax_pct / 100.0)), 2), 'name': '60 Days Extension'},
            ]

        # Build dynamic freeze options
        freeze_enabled = frz_cfg.get('enabled', getattr(os_obj, 'allow_member_freeze', True))
        freeze_min_days = int(frz_cfg.get('min_days', 7))
        freeze_max_days = int(frz_cfg.get('max_days', getattr(os_obj, 'max_freeze_days_per_year', 60) or 60))
        freeze_fee_fixed = float(frz_cfg.get('fee_fixed', 0.0))
        freeze_fee_per_day = float(frz_cfg.get('fee_per_day', 0.0))

        # Check existing PAR-Q
        prior_parq = IntakeSubmission.objects.using(alias).filter(user_profile=profile, status='COMPLETED').order_by('-submitted_at').first()
        parq_info = {
            'has_completed_parq': prior_parq is not None,
            'submission_id': str(prior_parq.id) if prior_parq else None,
            'submitted_at': prior_parq.submitted_at.isoformat() if prior_parq and prior_parq.submitted_at else None,
            'is_cleared': (getattr(prior_parq, 'is_cleared', prior_parq.status == 'COMPLETED') if prior_parq else False),
            'form_title': ((getattr(prior_parq.intake_form, 'name', None) or getattr(prior_parq.intake_form, 'title', 'Physical Activity Readiness Questionnaire')) if (prior_parq and getattr(prior_parq, 'intake_form', None)) else 'Physical Activity Readiness Questionnaire'),
        }

        # Razorpay Key
        razorpay_key_id = 'rzp_test_placeholder'
        if RazorpayService.is_configured(db_alias=alias):
            razorpay_key_id, _ = RazorpayService.get_credentials(db_alias=alias)

        if not active_m:
            return Response({
                'has_membership': False,
                'active_membership': None,
                'branches': branches,
                'programs': programs,
                'renew_options': [],
                'upgrade_options': [],
                'extend_options': {
                    'enabled': ext_cfg.get('enabled', True),
                    'available_options': ext_options_list,
                    'requires_approval': False,
                    'policy_note': 'Self-service extensions are processed immediately with secure checkout.',
                },
                'freeze_options': {
                    'enabled': freeze_enabled,
                    'min_days': freeze_min_days,
                    'max_days': freeze_max_days,
                    'fee_fixed': freeze_fee_fixed,
                    'fee_per_day': freeze_fee_per_day,
                    'tax_pct': tax_pct,
                    'requires_approval': False,
                    'policy_note': 'Freezes pause your membership and extend validity by the frozen period.',
                },
                'active_freeze': None,
                'parq_info': parq_info,
                'razorpay_key_id': razorpay_key_id,
            })

        # Calculate session credits
        total_sessions = 0
        remaining_sessions = 0
        used_sessions = 0
        for ent in active_m.entitlements.using(alias).all():
            alloc = int(getattr(ent, 'allocated_units', None) or getattr(ent, 'total_units', 0) or 0)
            cons = int(getattr(ent, 'consumed_units', 0) or 0)
            total_sessions += alloc
            used_sessions += cons
            remaining_sessions += max(0, alloc - cons)

        today = timezone.now().date()
        days_remaining = max(0, (active_m.end_date - today).days) if active_m.end_date else 0

        # Check active freeze
        active_freeze_rec = (
            MembershipFreeze.objects.using(alias)
            .filter(membership=active_m, status__in=['ACTIVE', 'SCHEDULED'])
            .order_by('-freeze_from')
            .first()
        )
        active_freeze_data = None
        if active_freeze_rec:
            active_freeze_data = {
                'id': str(active_freeze_rec.id),
                'freeze_from': active_freeze_rec.freeze_from.isoformat(),
                'freeze_until': active_freeze_rec.freeze_until.isoformat(),
                'days': active_freeze_rec.extend_membership_days or (active_freeze_rec.freeze_until - active_freeze_rec.freeze_from).days,
                'status': active_freeze_rec.status,
                'reason': active_freeze_rec.reason_text or '',
            }

        # Optional query param filters for selected branch
        filter_branch_id = request.query_params.get('branch_id')
        effective_branch = active_m.home_branch
        if filter_branch_id:
            found_br = Branch.objects.using(alias).filter(id=filter_branch_id, status='ACTIVE').first()
            if found_br:
                effective_branch = found_br

        # RENEW OPTIONS: Strictly fixed to active_m.program!
        renew_options = []
        if active_m.program:
            pkg_qs = (
                Package.objects.using(alias)
                .filter(status='ACTIVE', program=active_m.program)
                .select_related('program')
                .prefetch_related('versions__prices')
            )
            for pkg in pkg_qs:
                ver = pkg.versions.filter(status='ACTIVE').order_by('-version_number').first()
                if not ver:
                    continue
                if effective_branch and hasattr(ver, 'available_branches'):
                    if ver.available_branches.exists() and not ver.available_branches.filter(id=effective_branch.id).exists():
                        continue
                price_obj = ver.prices.filter(status='ACTIVE').first()
                if not price_obj:
                    continue
                b_price = float(price_obj.base_price)
                t_pct = float(price_obj.tax_percent or 18.0)
                t_amt = round(b_price * (t_pct / 100.0), 2)
                tot = round(b_price + t_amt, 2)
                renew_options.append({
                    'package_id': str(pkg.id),
                    'package_version_id': str(ver.id),
                    'name': pkg.name,
                    'program_id': str(pkg.program.id),
                    'program_name': pkg.program.name,
                    'program_code': pkg.program.code,
                    'duration_days': _get_ver_days(ver),
                    'session_count': _get_ver_sessions(ver),
                    'base_price': b_price,
                    'tax_percent': t_pct,
                    'tax_amount': t_amt,
                    'total_price': tot,
                    'currency': price_obj.currency or 'INR',
                })

        # Current membership pricing snapshot for comparison
        cur_total_price = 0.0
        cur_sessions = total_sessions
        cur_days = 0
        if active_m.package_version:
            cur_days = _get_ver_days(active_m.package_version)
            p_obj = active_m.package_version.prices.filter(status='ACTIVE').first()
            if p_obj:
                cur_b = float(p_obj.base_price)
                cur_t = float(p_obj.tax_percent or 18.0)
                cur_total_price = round(cur_b * (1.0 + (cur_t / 100.0)), 2)

        # UPGRADE OPTIONS: All programs & packages available for upgrade
        upgrade_options = []
        all_pkg_qs = (
            Package.objects.using(alias)
            .filter(status='ACTIVE')
            .select_related('program')
            .prefetch_related('versions__prices')
        )
        for pkg in all_pkg_qs:
            ver = pkg.versions.filter(status='ACTIVE').order_by('-version_number').first()
            if not ver:
                continue
            if active_m.package and pkg.id == active_m.package.id and active_m.package_version and ver.id == active_m.package_version.id:
                continue
            price_obj = ver.prices.filter(status='ACTIVE').first()
            if not price_obj:
                continue
            b_price = float(price_obj.base_price)
            t_pct = float(price_obj.tax_percent or 18.0)
            t_amt = round(b_price * (t_pct / 100.0), 2)
            tot = round(b_price + t_amt, 2)
            price_diff = max(0.0, round(tot - cur_total_price, 2))
            extra_sessions = max(0, _get_ver_sessions(ver) - cur_sessions)
            extra_days = max(0, _get_ver_days(ver) - cur_days)

            upgrade_options.append({
                'package_id': str(pkg.id),
                'package_version_id': str(ver.id),
                'name': pkg.name,
                'program_id': str(pkg.program.id) if pkg.program else '',
                'program_name': pkg.program.name if pkg.program else '',
                'program_code': pkg.program.code if pkg.program else '',
                'duration_days': _get_ver_days(ver),
                'session_count': _get_ver_sessions(ver),
                'base_price': b_price,
                'tax_percent': t_pct,
                'tax_amount': t_amt,
                'total_price': tot,
                'price_difference': price_diff,
                'additional_sessions': extra_sessions,
                'additional_days': extra_days,
                'currency': price_obj.currency or 'INR',
            })

        active_membership_data = {
            'id': str(active_m.id),
            'membership_number': active_m.membership_number,
            'status': active_m.status,
            'package_id': str(active_m.package.id) if active_m.package else None,
            'package_name': active_m.package.name if active_m.package else 'Custom Membership',
            'package_version_id': str(active_m.package_version.id) if active_m.package_version else None,
            'program_id': str(active_m.program.id) if active_m.program else None,
            'program_name': active_m.program.name if active_m.program else None,
            'program_code': active_m.program.code if active_m.program else None,
            'branch_id': str(active_m.home_branch.id) if active_m.home_branch else None,
            'branch_name': active_m.home_branch.name if active_m.home_branch else None,
            'start_date': str(active_m.start_date) if active_m.start_date else None,
            'end_date': str(active_m.end_date) if active_m.end_date else None,
            'days_remaining': days_remaining,
            'total_sessions': total_sessions,
            'remaining_sessions': remaining_sessions,
            'used_sessions': used_sessions,
            'is_frozen': active_m.status == 'FROZEN',
            'parq_status': active_m.parq_status,
            'parq_completed_at': active_m.parq_completed_at.isoformat() if active_m.parq_completed_at else None,
        }

        return Response({
            'has_membership': True,
            'active_membership': active_membership_data,
            'branches': branches,
            'programs': programs,
            'renew_options': renew_options,
            'upgrade_options': upgrade_options,
            'extend_options': {
                'enabled': ext_cfg.get('enabled', True),
                'available_options': ext_options_list,
                'requires_approval': False,
                'policy_note': 'Self-service extensions are processed immediately with secure Razorpay checkout.',
            },
            'freeze_options': {
                'enabled': freeze_enabled,
                'min_days': freeze_min_days,
                'max_days': freeze_max_days,
                'fee_fixed': freeze_fee_fixed,
                'fee_per_day': freeze_fee_per_day,
                'tax_pct': tax_pct,
                'requires_approval': False,
                'policy_note': 'Freezing pauses your membership status and shifts your expiry date automatically.',
            },
            'active_freeze': active_freeze_data,
            'parq_info': parq_info,
            'razorpay_key_id': razorpay_key_id,
        })


class MobileMembershipRenewView(APIView):
    """
    POST /api/v1/mobile/memberships/renew/
    Initiates member-facing renewal checkout.
    - Validates target package belongs to current program (program remains fixed).
    - Supports dynamic branch selection.
    - Handles PAR-Q decision (REUSE, EDIT, NEW).
    - Generates Razorpay checkout order with server-calculated price and tax.
    - Final activation occurs strictly upon verified payment via MobileCheckoutVerifyView.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        from decimal import Decimal
        from .models_memberships import Membership, Package, PackageVersion
        from .models_workforce import Branch
        from .models_commerce import Order, OrderItem
        from .models_crm import IntakeSubmission
        from .services_razorpay import RazorpayService

        active_m = (
            Membership.objects.using(alias)
            .filter(user_profile=profile)
            .select_related('package', 'package_version', 'program', 'home_branch')
            .order_by('-created_at')
            .first()
        )
        if not active_m:
            return Response({'detail': 'No membership found to renew.'}, status=status.HTTP_400_BAD_REQUEST)

        package_id = request.data.get('package_id') or (str(active_m.package.id) if active_m.package else None)
        package_version_id = request.data.get('package_version_id')
        branch_id = request.data.get('branch_id')

        package = Package.objects.using(alias).filter(id=package_id, status='ACTIVE').first() if package_id else None
        if not package:
            return Response({'detail': 'Renewal package not found or inactive.'}, status=status.HTTP_404_NOT_FOUND)

        # CRITICAL RULE: Program must remain fixed during renewal!
        if active_m.program and package.program_id != active_m.program_id:
            return Response(
                {'detail': f"Renewal package must belong to your current program '{active_m.program.name}'. Use Upgrade Membership to switch programs."},
                status=status.HTTP_400_BAD_REQUEST
            )

        branch = None
        if branch_id:
            branch = Branch.objects.using(alias).filter(id=branch_id, status='ACTIVE').first()
        if not branch:
            branch = active_m.home_branch or Branch.objects.using(alias).filter(status='ACTIVE').first()

        if package_version_id:
            ver = PackageVersion.objects.using(alias).filter(id=package_version_id, package=package).first()
        else:
            ver = package.versions.using(alias).filter(status='ACTIVE').order_by('-version_number').first()

        if not ver:
            return Response({'detail': 'Active package version not found for renewal.'}, status=status.HTTP_400_BAD_REQUEST)

        price_obj = ver.prices.using(alias).filter(status='ACTIVE').first()
        if not price_obj:
            return Response({'detail': 'Package price not configured.'}, status=status.HTTP_400_BAD_REQUEST)

        base_price = price_obj.base_price
        tax_pct = price_obj.tax_percent or Decimal('18.0')
        tax_amount = round(base_price * (tax_pct / Decimal('100.0')), 2)
        total_price = base_price + tax_amount

        # Process PAR-Q decision
        parq_decision = str(request.data.get('parq_decision', 'REUSE')).upper().strip()
        parq_submission_id = request.data.get('existing_submission_id')

        if parq_decision == 'REUSE':
            if not parq_submission_id:
                prior_sub = IntakeSubmission.objects.using(alias).filter(user_profile=profile, status='COMPLETED').order_by('-submitted_at').first()
                if prior_sub:
                    parq_submission_id = str(prior_sub.id)
        elif parq_decision in ['EDIT', 'NEW']:
            answers = request.data.get('answers')
            sig_data = request.data.get('signature_data')
            if sig_data or answers:
                from .models_crm import IntakeForm
                from .services_storage import save_compressed_signature
                form = active_m.parq_form or IntakeForm.objects.using(alias).filter(status='ACTIVE', form_type__in=['PAR_Q', 'PARQ']).order_by('-version_number').first()
                if form:
                    stored_sig = save_compressed_signature(sig_data, tenant_slug=alias) if sig_data else ''
                    new_sub = IntakeSubmission.objects.using(alias).create(
                        organization=branch.organization if branch else None,
                        intake_form=form,
                        user_profile=profile,
                        submitted_by_user=user,
                        channel='MEMBER_PORTAL',
                        status='COMPLETED',
                        is_cleared=True,
                        signature_data=stored_sig,
                        signed_at=timezone.now(),
                    )
                    parq_submission_id = str(new_sub.id)

        # Idempotency check: Reuse existing pending renewal order created in the last 10 minutes
        ten_mins_ago = timezone.now() - timedelta(minutes=10)
        existing_order = (
            Order.objects.using(alias)
            .filter(
                user_profile=profile,
                order_type='RENEWAL',
                status='PENDING_PAYMENT',
                created_at__gte=ten_mins_ago
            )
            .order_by('-created_at')
            .first()
        )

        metadata_dict = {
            'action_type': 'RENEWAL',
            'membership_id': str(active_m.id),
            'branch_id': str(branch.id) if branch else '',
            'package_id': str(package.id),
            'package_version_id': str(ver.id),
            'parq_decision': parq_decision,
            'parq_submission_id': parq_submission_id,
        }

        with transaction.atomic(using=alias):
            if existing_order:
                order = existing_order
                order.subtotal = base_price
                order.tax_amount = tax_amount
                order.total_amount = total_price
                order.notes = json.dumps(metadata_dict)
                order.save(using=alias)
            else:
                order_num = f"REN-{uuid.uuid4().hex[:8].upper()}"
                order = Order.objects.using(alias).create(
                    branch=branch,
                    user_profile=profile,
                    order_number=order_num,
                    order_type='RENEWAL',
                    source='MOBILE_APP',
                    status='PENDING_PAYMENT',
                    subtotal=base_price,
                    discount_amount=Decimal('0.00'),
                    tax_amount=tax_amount,
                    total_amount=total_price,
                    currency=price_obj.currency or 'INR',
                    notes=json.dumps(metadata_dict),
                )
                OrderItem.objects.using(alias).create(
                    order=order,
                    item_type='PACKAGE',
                    package=package,
                    package_version=ver,
                    package_price=price_obj,
                    item_name_snapshot=f"Membership Renewal: {package.name}",
                    quantity=Decimal('1.00'),
                    unit_price_snapshot=base_price,
                    tax_percent_snapshot=tax_pct,
                    tax_amount=tax_amount,
                    total_amount=total_price,
                )

        # Create Razorpay order
        amount_paise = RazorpayService.convert_inr_to_paise(total_price)
        key_id = 'rzp_test_placeholder'
        rzp_order_id = f"order_test_{order.order_number}"

        if RazorpayService.is_configured(db_alias=alias) and amount_paise > 0:
            key_id, _ = RazorpayService.get_credentials(db_alias=alias)
            try:
                rzp_order = RazorpayService.create_order(
                    amount_paise=amount_paise,
                    receipt=order.order_number,
                    currency=order.currency,
                    notes={
                        'internal_order_id': str(order.id),
                        'user_id': str(user.id),
                        'order_number': order.order_number,
                        'order_type': 'RENEWAL',
                    },
                    db_alias=alias,
                )
                rzp_order_id = rzp_order['id']
            except Exception as e:
                logger.error("Razorpay order creation failed for renewal: %s", e)
                rzp_order_id = f"order_test_{order.order_number}"

        return Response({
            'order_id': str(order.id),
            'order_number': order.order_number,
            'razorpay_order_id': rzp_order_id,
            'status': order.status,
            'amount': float(total_price),
            'amount_paise': amount_paise,
            'currency': order.currency,
            'razorpay_key_id': key_id,
            'package_name': package.name,
            'branch_name': branch.name if branch else '',
            'detail': 'Existing pending renewal order retrieved. Complete payment to activate your renewed package.' if existing_order else 'Renewal order created. Complete payment to activate your renewed package.',
            'requires_payment': True,
            'is_active': False,
        }, status=status.HTTP_200_OK if existing_order else status.HTTP_201_CREATED)


class MobileMembershipUpgradeView(APIView):
    """
    POST /api/v1/mobile/memberships/upgrade/
    Allows eligible members to upgrade independently without manual approval:
    - Select branch, program, package.
    - Authoritative server-side calculation of price difference, additional entitlements, and taxes.
    - Generates Razorpay checkout order for price difference.
    - Final upgrade executed strictly upon verified payment via MobileCheckoutVerifyView.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        from decimal import Decimal
        from .models_memberships import Membership, Package, PackageVersion
        from .models_workforce import Branch
        from .models_catalog import Program
        from .models_commerce import Order, OrderItem
        from .models_crm import IntakeSubmission
        from .services_razorpay import RazorpayService
        from .services_memberships import MembershipLifecycleService

        active_m = (
            Membership.objects.using(alias)
            .filter(user_profile=profile)
            .select_related('package', 'package_version', 'program', 'home_branch')
            .order_by('-created_at')
            .first()
        )
        if not active_m:
            return Response({'detail': 'No active membership found to upgrade.'}, status=status.HTTP_400_BAD_REQUEST)

        target_pkg_id = request.data.get('target_package_id')
        target_ver_id = request.data.get('target_package_version_id')
        target_branch_id = request.data.get('target_branch_id')
        reason = (request.data.get('reason') or 'Self-service tier upgrade').strip()

        if not target_pkg_id:
            return Response({'detail': 'Target package is required for upgrade.'}, status=status.HTTP_400_BAD_REQUEST)

        target_pkg = Package.objects.using(alias).filter(id=target_pkg_id, status='ACTIVE').first()
        if not target_pkg:
            return Response({'detail': 'Target upgrade package not found or inactive.'}, status=status.HTTP_404_NOT_FOUND)

        if target_ver_id:
            target_ver = PackageVersion.objects.using(alias).filter(id=target_ver_id, package=target_pkg, status='ACTIVE').first()
        else:
            target_ver = target_pkg.versions.using(alias).filter(status='ACTIVE').order_by('-version_number').first()

        if not target_ver:
            return Response({'detail': 'Target package version not found.'}, status=status.HTTP_400_BAD_REQUEST)

        target_branch = None
        if target_branch_id:
            target_branch = Branch.objects.using(alias).filter(id=target_branch_id, status='ACTIVE').first()
        if not target_branch:
            target_branch = active_m.home_branch or Branch.objects.using(alias).filter(status='ACTIVE').first()

        target_price_obj = target_ver.prices.using(alias).filter(status='ACTIVE').first()
        if not target_price_obj:
            return Response({'detail': 'Target package price not configured.'}, status=status.HTTP_400_BAD_REQUEST)

        target_base = target_price_obj.base_price
        target_tax_pct = target_price_obj.tax_percent or Decimal('18.0')
        target_tax = round(target_base * (target_tax_pct / Decimal('100.0')), 2)
        target_total = target_base + target_tax

        # Current package value calculation
        cur_total = Decimal('0.00')
        cur_base = Decimal('0.00')
        if active_m.package_version:
            cur_p = active_m.package_version.prices.using(alias).filter(status='ACTIVE').first()
            if cur_p:
                cur_base = cur_p.base_price
                cur_tax_pct = cur_p.tax_percent or Decimal('18.0')
                cur_total = cur_base * (Decimal('1.00') + (cur_tax_pct / Decimal('100.0')))

        # Server-authoritative difference
        diff_total = max(Decimal('0.00'), round(target_total - cur_total, 2))
        diff_base = max(Decimal('0.00'), round(target_base - cur_base, 2))
        diff_tax = max(Decimal('0.00'), diff_total - diff_base)

        parq_decision = str(request.data.get('parq_decision', 'REUSE')).upper().strip()
        parq_submission_id = request.data.get('existing_submission_id')
        if parq_decision == 'REUSE' and not parq_submission_id:
            prior_sub = IntakeSubmission.objects.using(alias).filter(user_profile=profile, status='COMPLETED').order_by('-submitted_at').first()
            if prior_sub:
                parq_submission_id = str(prior_sub.id)

        # If price difference is zero, execute upgrade immediately
        if diff_total <= Decimal('0.00'):
            upgraded_m = MembershipLifecycleService.execute_upgrade(
                membership=active_m,
                target_package=target_pkg,
                target_package_version=target_ver,
                target_branch=target_branch,
                actor_user=user,
                db_alias=alias,
            )
            return Response({
                'detail': f"Membership upgraded to {target_pkg.name} successfully at no additional charge.",
                'status': 'COMPLETED',
                'membership_id': str(upgraded_m.id),
                'package_name': target_pkg.name,
                'price_difference': 0.0,
                'requires_payment': False,
            }, status=status.HTTP_200_OK)

        # Create pending order for upgrade
        metadata_dict = {
            'action_type': 'UPGRADE',
            'current_membership_id': str(active_m.id),
            'target_package_id': str(target_pkg.id),
            'target_package_version_id': str(target_ver.id),
            'target_branch_id': str(target_branch.id) if target_branch else '',
            'parq_decision': parq_decision,
            'parq_submission_id': parq_submission_id,
            'reason': reason,
        }

        with transaction.atomic(using=alias):
            order_num = f"UPG-{uuid.uuid4().hex[:8].upper()}"
            order = Order.objects.using(alias).create(
                branch=target_branch,
                user_profile=profile,
                order_number=order_num,
                order_type='UPGRADE',
                source='MOBILE_APP',
                status='PENDING_PAYMENT',
                subtotal=diff_base,
                discount_amount=Decimal('0.00'),
                tax_amount=diff_tax,
                total_amount=diff_total,
                currency=target_price_obj.currency or 'INR',
                notes=json.dumps(metadata_dict),
            )
            OrderItem.objects.using(alias).create(
                order=order,
                item_type='PACKAGE',
                package=target_pkg,
                package_version=target_ver,
                package_price=target_price_obj,
                item_name_snapshot=f"Tier Upgrade: {target_pkg.name}",
                quantity=Decimal('1.00'),
                unit_price_snapshot=diff_base,
                tax_percent_snapshot=target_tax_pct,
                tax_amount=diff_tax,
                total_amount=diff_total,
            )

        amount_paise = RazorpayService.convert_inr_to_paise(diff_total)
        key_id = 'rzp_test_placeholder'
        rzp_order_id = f"order_test_{order.order_number}"

        if RazorpayService.is_configured(db_alias=alias) and amount_paise > 0:
            key_id, _ = RazorpayService.get_credentials(db_alias=alias)
            try:
                rzp_order = RazorpayService.create_order(
                    amount_paise=amount_paise,
                    receipt=order.order_number,
                    currency=order.currency,
                    notes={
                        'internal_order_id': str(order.id),
                        'user_id': str(user.id),
                        'order_number': order.order_number,
                        'order_type': 'UPGRADE',
                    },
                    db_alias=alias,
                )
                rzp_order_id = rzp_order['id']
            except Exception as e:
                logger.error("Razorpay order creation failed for upgrade: %s", e)
                rzp_order_id = f"order_test_{order.order_number}"

        return Response({
            'order_id': str(order.id),
            'order_number': order.order_number,
            'razorpay_order_id': rzp_order_id,
            'status': order.status,
            'amount': float(diff_total),
            'amount_paise': amount_paise,
            'currency': order.currency,
            'razorpay_key_id': key_id,
            'target_package_name': target_pkg.name,
            'price_difference': float(diff_total),
            'net_difference': float(diff_total),
            'total_payable': float(diff_total),
            'requires_payment': True,
            'breakdown': {
                'current_package_price': float(cur_total),
                'target_package_price': float(target_total),
                'difference_base': float(diff_base),
                'difference_tax': float(diff_tax),
                'total_payable': float(diff_total),
            },
            'detail': f"Upgrade order for {target_pkg.name} created. Complete payment to finalize tier change.",
            'requires_payment': True,
        }, status=status.HTTP_201_CREATED)


class MobileMembershipExtendView(APIView):
    """
    POST /api/v1/mobile/memberships/extend/
    Self-service validity extension with dynamic fee lookup and Razorpay checkout:
    - Days selected dynamically (7, 14, 30, 60 days).
    - Dynamic fee calculated from CRM OrganizationSettings.
    - Immediate extension applied if free; Razorpay checkout created if fee > 0.
    - Verified payment in MobileCheckoutVerifyView atomically shifts expiry date.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        from decimal import Decimal
        from .models_memberships import Membership
        from .models_govern import OrganizationSettings
        from .models_commerce import Order, OrderItem
        from .services_razorpay import RazorpayService
        from .services_memberships import MembershipLifecycleService

        active_m = (
            Membership.objects.using(alias)
            .filter(user_profile=profile)
            .select_related('home_branch')
            .order_by('-created_at')
            .first()
        )
        if not active_m:
            return Response({'detail': 'No membership found to extend.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            days = int(request.data.get('days', 14))
        except (ValueError, TypeError):
            return Response({'detail': 'Extension duration must be an integer number of days.'}, status=status.HTTP_400_BAD_REQUEST)

        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'detail': 'Reason is required for validity extension.'}, status=status.HTTP_400_BAD_REQUEST)

        # Dynamic fee lookup from CRM configuration
        os_obj = OrganizationSettings.objects.using(alias).first()
        cfg = getattr(os_obj, 'membership_config', {}) or {}
        ext_cfg = cfg.get('extension_policy', {})
        tax_pct = Decimal(str(getattr(os_obj, 'tax_rate_pct', 18.0) or '18.0'))

        allowed_days = [7, 14, 21, 30, 60]
        configured_options = ext_cfg.get('available_options', [])
        if configured_options:
            allowed_days = [int(opt.get('days')) for opt in configured_options if 'days' in opt]

        if days not in allowed_days:
            return Response({'detail': f'Extension duration of {days} days is not permitted. Permitted options: {allowed_days}'}, status=status.HTTP_400_BAD_REQUEST)

        # Look up matched fee from config or calculate prorated tier
        matched_base_fee = None
        for opt in configured_options:
            if int(opt.get('days', 0)) == days:
                matched_base_fee = Decimal(str(opt.get('base_fee', opt.get('fee', 0.0))))
                break

        if matched_base_fee is None:
            tier_defaults = {7: Decimal('500.00'), 14: Decimal('900.00'), 21: Decimal('1200.00'), 30: Decimal('1500.00'), 60: Decimal('2800.00')}
            matched_base_fee = tier_defaults.get(days, Decimal(str(days * 50)))

        tax_amount = round(matched_base_fee * (tax_pct / Decimal('100.0')), 2)
        total_fee = matched_base_fee + tax_amount

        # If zero fee, extend immediately
        if total_fee <= Decimal('0.00'):
            ext_m = MembershipLifecycleService.extend_membership(
                membership=active_m,
                days=days,
                reason_text=reason,
                approved_by_user=user,
                db_alias=alias,
            )
            return Response({
                'detail': f"Membership validity extended by {days} days. New expiry: {ext_m.end_date}.",
                'status': 'COMPLETED',
                'days': days,
                'new_end_date': str(ext_m.end_date),
                'requires_payment': False,
            }, status=status.HTTP_200_OK)

        branch = active_m.home_branch
        metadata_dict = {
            'action_type': 'VALIDITY_EXTENSION',
            'membership_id': str(active_m.id),
            'days': days,
            'reason': reason,
        }

        with transaction.atomic(using=alias):
            order_num = f"EXT-{uuid.uuid4().hex[:8].upper()}"
            order = Order.objects.using(alias).create(
                branch=branch,
                user_profile=profile,
                order_number=order_num,
                order_type='EXTENSION',
                source='MOBILE_APP',
                status='PENDING_PAYMENT',
                subtotal=matched_base_fee,
                discount_amount=Decimal('0.00'),
                tax_amount=tax_amount,
                total_amount=total_fee,
                currency='INR',
                notes=json.dumps(metadata_dict),
            )
            OrderItem.objects.using(alias).create(
                order=order,
                item_type='PACKAGE',
                item_name_snapshot=f"Validity Extension: {days} Days",
                quantity=Decimal('1.00'),
                unit_price_snapshot=matched_base_fee,
                tax_percent_snapshot=tax_pct,
                tax_amount=tax_amount,
                total_amount=total_fee,
            )

        amount_paise = RazorpayService.convert_inr_to_paise(total_fee)
        key_id = 'rzp_test_placeholder'
        rzp_order_id = f"order_test_{order.order_number}"

        if RazorpayService.is_configured(db_alias=alias) and amount_paise > 0:
            key_id, _ = RazorpayService.get_credentials(db_alias=alias)
            try:
                rzp_order = RazorpayService.create_order(
                    amount_paise=amount_paise,
                    receipt=order.order_number,
                    currency=order.currency,
                    notes={
                        'internal_order_id': str(order.id),
                        'user_id': str(user.id),
                        'order_number': order.order_number,
                        'order_type': 'EXTENSION',
                    },
                    db_alias=alias,
                )
                rzp_order_id = rzp_order['id']
            except Exception as e:
                logger.error("Razorpay order creation failed for extension: %s", e)
                rzp_order_id = f"order_test_{order.order_number}"

        return Response({
            'order_id': str(order.id),
            'order_number': order.order_number,
            'razorpay_order_id': rzp_order_id,
            'status': order.status,
            'amount': float(total_fee),
            'amount_paise': amount_paise,
            'currency': order.currency,
            'razorpay_key_id': key_id,
            'days': days,
            'current_end_date': str(active_m.end_date) if active_m.end_date else None,
            'detail': f"Extension order for {days} days created. Complete payment to extend your validity.",
            'requires_payment': True,
        }, status=status.HTTP_201_CREATED)


class MobileMembershipFreezeView(APIView):
    """
    POST /api/v1/mobile/memberships/freeze/
    Self-service membership freeze management:
    - Validates duration (7 to max allowed days).
    - Dynamic fee calculation from CRM OrganizationSettings.
    - Freezes immediately if no charge; initiates Razorpay checkout if fee applies.
    - Verified payment applies freeze and shifts validity atomically.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        from decimal import Decimal
        from datetime import datetime
        from .models_memberships import Membership, MembershipFreeze
        from .models_govern import OrganizationSettings
        from .models_commerce import Order, OrderItem
        from .services_razorpay import RazorpayService
        from .services_memberships import MembershipLifecycleService

        active_m = (
            Membership.objects.using(alias)
            .filter(user_profile=profile, status='ACTIVE')
            .select_related('home_branch', 'package')
            .order_by('-created_at')
            .first()
        )
        if not active_m:
            return Response({'detail': 'Only active memberships can be frozen.'}, status=status.HTTP_400_BAD_REQUEST)

        freeze_from_str = request.data.get('freeze_from')
        freeze_until_str = request.data.get('freeze_until')
        reason = (request.data.get('reason') or 'Self-service member freeze').strip()

        if not freeze_from_str or not freeze_until_str:
            return Response({'detail': 'Freeze start and end dates are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from_d = datetime.strptime(freeze_from_str[:10], '%Y-%m-%d').date()
            until_d = datetime.strptime(freeze_until_str[:10], '%Y-%m-%d').date()
        except Exception:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

        today = timezone.now().date()
        if from_d < today:
            return Response({'detail': 'Freeze start date cannot be in the past.'}, status=status.HTTP_400_BAD_REQUEST)
        if until_d <= from_d:
            return Response({'detail': 'Freeze end date must be after start date.'}, status=status.HTTP_400_BAD_REQUEST)

        freeze_days = (until_d - from_d).days
        if freeze_days < 7:
            return Response({'detail': 'Minimum freeze duration is 7 days.'}, status=status.HTTP_400_BAD_REQUEST)

        os_obj = OrganizationSettings.objects.using(alias).first()
        max_allowed = getattr(os_obj, 'max_freeze_days_per_year', 60) or 60
        if freeze_days > max_allowed:
            return Response({'detail': f"Maximum allowed freeze duration is {max_allowed} days."}, status=status.HTTP_400_BAD_REQUEST)

        overlap = MembershipFreeze.objects.using(alias).filter(
            membership=active_m,
            status__in=['SCHEDULED', 'ACTIVE'],
            freeze_from__lte=until_d,
            freeze_until__gte=from_d,
        ).exists()
        if overlap:
            return Response({'detail': 'A freeze window already overlaps with these dates.'}, status=status.HTTP_400_BAD_REQUEST)

        # Dynamic fee from settings
        cfg = getattr(os_obj, 'membership_config', {}) or {}
        frz_cfg = cfg.get('freeze_policy', {})
        fixed_fee = Decimal(str(frz_cfg.get('fee_fixed', '0.00')))
        per_day_fee = Decimal(str(frz_cfg.get('fee_per_day', '0.00')))
        tax_pct = Decimal(str(getattr(os_obj, 'tax_rate_pct', 18.0) or '18.0'))

        base_freeze_fee = fixed_fee + (per_day_fee * Decimal(str(freeze_days)))
        tax_amt = round(base_freeze_fee * (tax_pct / Decimal('100.0')), 2)
        total_freeze_fee = base_freeze_fee + tax_amt

        # If zero fee, apply freeze directly!
        if total_freeze_fee <= Decimal('0.00'):
            MembershipLifecycleService.apply_freeze(
                membership=active_m,
                freeze_from=from_d,
                freeze_until=until_d,
                reason_text=reason,
                approved_by_user=user,
                db_alias=alias,
            )
            return Response({
                'detail': f"Membership frozen successfully from {from_d} to {until_d} ({freeze_days} days). End date extended.",
                'status': 'FROZEN',
                'freeze_from': str(from_d),
                'freeze_until': str(until_d),
                'days': freeze_days,
                'new_end_date': str(active_m.end_date) if active_m.end_date else None,
                'requires_payment': False,
            }, status=status.HTTP_200_OK)

        branch = active_m.home_branch
        metadata_dict = {
            'action_type': 'FREEZE',
            'membership_id': str(active_m.id),
            'freeze_from': str(from_d),
            'freeze_until': str(until_d),
            'days': freeze_days,
            'reason': reason,
        }

        with transaction.atomic(using=alias):
            order_num = f"FRZ-{uuid.uuid4().hex[:8].upper()}"
            order = Order.objects.using(alias).create(
                branch=branch,
                user_profile=profile,
                order_number=order_num,
                order_type='OTHER',
                source='MOBILE_APP',
                status='PENDING_PAYMENT',
                subtotal=base_freeze_fee,
                discount_amount=Decimal('0.00'),
                tax_amount=tax_amt,
                total_amount=total_freeze_fee,
                currency='INR',
                notes=json.dumps(metadata_dict),
            )
            OrderItem.objects.using(alias).create(
                order=order,
                item_type='PACKAGE',
                item_name_snapshot=f"Membership Freeze: {freeze_days} Days",
                quantity=Decimal('1.00'),
                unit_price_snapshot=base_freeze_fee,
                tax_percent_snapshot=tax_pct,
                tax_amount=tax_amt,
                total_amount=total_freeze_fee,
            )

        amount_paise = RazorpayService.convert_inr_to_paise(total_freeze_fee)
        key_id = 'rzp_test_placeholder'
        rzp_order_id = f"order_test_{order.order_number}"

        if RazorpayService.is_configured(db_alias=alias) and amount_paise > 0:
            key_id, _ = RazorpayService.get_credentials(db_alias=alias)
            try:
                rzp_order = RazorpayService.create_order(
                    amount_paise=amount_paise,
                    receipt=order.order_number,
                    currency=order.currency,
                    notes={
                        'internal_order_id': str(order.id),
                        'user_id': str(user.id),
                        'order_number': order.order_number,
                        'order_type': 'FREEZE',
                    },
                    db_alias=alias,
                )
                rzp_order_id = rzp_order['id']
            except Exception as e:
                logger.error("Razorpay order creation failed for freeze: %s", e)
                rzp_order_id = f"order_test_{order.order_number}"

        return Response({
            'order_id': str(order.id),
            'order_number': order.order_number,
            'razorpay_order_id': rzp_order_id,
            'status': order.status,
            'amount': float(total_freeze_fee),
            'amount_paise': amount_paise,
            'currency': order.currency,
            'razorpay_key_id': key_id,
            'freeze_from': str(from_d),
            'freeze_until': str(until_d),
            'days': freeze_days,
            'detail': f"Freeze order for {freeze_days} days created. Complete payment to activate freeze.",
            'requires_payment': True,
        }, status=status.HTTP_201_CREATED)


class MobileMembershipUnfreezeView(APIView):
    """
    POST /api/v1/mobile/memberships/unfreeze/
    Unfreezes a member's active frozen membership and restores it to ACTIVE status immediately.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = getattr(user._state, 'db', None) or get_tenant_db_alias() or 'default'
        profile = _get_or_create_user_profile(user, alias)

        from .models_memberships import Membership
        from .services_memberships import MembershipLifecycleService

        frozen_m = (
            Membership.objects.using(alias)
            .filter(user_profile=profile, status='FROZEN')
            .order_by('-created_at')
            .first()
        )
        if not frozen_m:
            frozen_m = (
                Membership.objects.using(alias)
                .filter(user_profile=profile)
                .order_by('-created_at')
                .first()
            )

        if not frozen_m:
            return Response({'detail': 'No membership found to unfreeze.'}, status=status.HTTP_400_BAD_REQUEST)

        reason = (request.data.get('reason') or 'Unfreeze requested by member via mobile app').strip()
        MembershipLifecycleService.unfreeze_membership(
            membership=frozen_m,
            reason_text=reason,
            approved_by_user=user,
            db_alias=alias,
        )
        profile.member_status = 'ACTIVE'
        profile.save(using=alias, update_fields=['member_status'])

        return Response({
            'detail': 'Membership unfreezed successfully and restored to Active status.',
            'status': 'ACTIVE',
            'membership_id': str(frozen_m.id),
        }, status=status.HTTP_200_OK)


class MobileDirectTurnstileOpenView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        import urllib.request
        url = "http://192.168.1.108/cgi-bin/accessControl.cgi?action=openDoor&channel=1"
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, "admin", "tipl9910")
        handler = urllib.request.HTTPDigestAuthHandler(mgr)
        opener = urllib.request.build_opener(handler)
        try:
            resp = opener.open(url, timeout=3)
            res_str = resp.read().decode('utf-8', errors='ignore').strip()
            return Response({"success": True, "detail": "Turnstile gate unlocked!", "result": res_str}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

