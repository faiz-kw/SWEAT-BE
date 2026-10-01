from django.urls import path
from .views_mobile import (
    MobileRegisterView,
    MobileLoginView,
    MobileMeView,
    MobileBranchesView,
    MobileClassesView,
    MobileTrainersView,
    MobileScheduleView,
    MobileClaimFreeTrialView,
    MobileBookClassView,
    MobileMyBookingsView,
    MobileCancelBookingView,
    MobileQRPassView,
    MobilePackagesView,
    MobileMyCreditsView,
    MobileValidateCouponView,
    MobileCheckoutOrderView,
    MobileCheckoutVerifyView,
    MobileOnboardingSurveyView,
    MobileOnboardingSubmitView,
)

app_name = 'mobile_api'

urlpatterns = [
    # 1. Auth & Profile
    path('auth/register/', MobileRegisterView.as_view(), name='mobile-register'),
    path('auth/login/', MobileLoginView.as_view(), name='mobile-login'),
    path('auth/me/', MobileMeView.as_view(), name='mobile-me'),

    # 2. Studio Catalog & Schedule
    path('branches/', MobileBranchesView.as_view(), name='mobile-branches'),
    path('classes/', MobileClassesView.as_view(), name='mobile-classes'),
    path('trainers/', MobileTrainersView.as_view(), name='mobile-trainers'),
    path('schedule/', MobileScheduleView.as_view(), name='mobile-schedule'),

    # 3. Bookings, Free Trial, Cancellations & QR Pass
    path('bookings/claim-free-trial/', MobileClaimFreeTrialView.as_view(), name='mobile-claim-free-trial'),
    path('bookings/book/', MobileBookClassView.as_view(), name='mobile-book-class'),
    path('bookings/my-bookings/', MobileMyBookingsView.as_view(), name='mobile-my-bookings'),
    path('bookings/<uuid:booking_id>/cancel/', MobileCancelBookingView.as_view(), name='mobile-cancel-booking'),
    path('bookings/<uuid:booking_id>/qr-pass/', MobileQRPassView.as_view(), name='mobile-qr-pass'),

    # 4. Memberships, Packs, Credits & Checkout
    path('packages/', MobilePackagesView.as_view(), name='mobile-packages'),
    path('my-credits/', MobileMyCreditsView.as_view(), name='mobile-my-credits'),
    path('coupons/validate/', MobileValidateCouponView.as_view(), name='mobile-validate-coupon'),
    path('checkout/create-order/', MobileCheckoutOrderView.as_view(), name='mobile-checkout-create-order'),
    path('checkout/verify/', MobileCheckoutVerifyView.as_view(), name='mobile-checkout-verify'),

    # 5. Onboarding & Health Assessments
    path('onboarding/survey/', MobileOnboardingSurveyView.as_view(), name='mobile-onboarding-survey'),
    path('onboarding/submit/', MobileOnboardingSubmitView.as_view(), name='mobile-onboarding-submit'),
]
