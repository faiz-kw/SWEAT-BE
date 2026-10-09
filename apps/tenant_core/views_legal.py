"""
Public Legal & Compliance Views.

Three permanently public, unauthenticated endpoints required for Meta App Review
and general platform compliance:
  /privacy-policy/
  /terms-of-service/
  /data-deletion/
"""

import os
from django.conf import settings
from django.shortcuts import render
from django.views.decorators.http import require_GET


@require_GET
def privacy_policy_view(request):
    """
    Public, unauthenticated Privacy Policy page.
    Required for Meta Lead Ads Instant Forms verification and compliance.
    """
    contact_email = getattr(settings, 'PRIVACY_CONTACT_EMAIL', '') or os.getenv('PRIVACY_CONTACT_EMAIL', '')
    return render(
        request,
        'legal/privacy_policy.html',
        {
            'contact_email': contact_email.strip(),
        },
    )


@require_GET
def terms_of_service_view(request):
    """
    Public, unauthenticated Terms of Service page.
    Required for Meta App Review and general SaaS platform compliance.
    """
    contact_email = getattr(settings, 'PRIVACY_CONTACT_EMAIL', '') or os.getenv('PRIVACY_CONTACT_EMAIL', '')
    return render(
        request,
        'legal/terms_of_service.html',
        {
            'contact_email': contact_email.strip(),
        },
    )


@require_GET
def data_deletion_view(request):
    """
    Public, unauthenticated Data Deletion Instructions page.
    Required by Meta for apps that handle user data via Facebook Login / Lead Ads.
    Explains how users can request removal of connection data and personal information.
    """
    contact_email = getattr(settings, 'PRIVACY_CONTACT_EMAIL', '') or os.getenv('PRIVACY_CONTACT_EMAIL', '')
    return render(
        request,
        'legal/data_deletion.html',
        {
            'contact_email': contact_email.strip(),
        },
    )
