from django.urls import path
from .views_mobile import (
    MobileRegisterView,
    MobileLeadCaptureView,
    MobileLoginView,
    MobileGoogleAuthView,
    MobileFacebookAuthView,
    MobileMeView,
    MobileBranchesView,
    MobileNearestBranchView,
    MobileClassesView,
    MobileTrainersView,
    MobileScheduleView,
    MobileClaimFreeTrialView,
    MobileBookClassView,
    MobileMyBookingsView,
    MobileCancelBookingView,
    MobileRescheduleBookingView,
    MobileQRPassView,
    MobilePackagesView,
    MobileMyCreditsView,
    MobileDiscountCouponsView,
    MobileValidateCouponView,
    MobileCheckoutOrderView,
    MobileCheckoutVerifyView,
    MobileOnboardingSurveyView,
    MobileOnboardingSubmitView,
    MobilePTAppointmentsView,
    MobileCancelPTAppointmentView,
)

app_name = 'mobile_api'

urlpatterns = [
    # 1. Auth & Profile
    path('auth/register/', MobileRegisterView.as_view(), name='mobile-register'),
    path('leads/', MobileLeadCaptureView.as_view(), name='mobile-leads'),
    path('leads/register/', MobileLeadCaptureView.as_view(), name='mobile-leads-register'),
    path('lead/', MobileLeadCaptureView.as_view(), name='mobile-lead-direct'),
    path('auth/login/', MobileLoginView.as_view(), name='mobile-login'),
    path('auth/google/', MobileGoogleAuthView.as_view(), name='mobile-auth-google'),
    path('auth/facebook/', MobileFacebookAuthView.as_view(), name='mobile-auth-facebook'),
    path('auth/me/', MobileMeView.as_view(), name='mobile-me'),
    path('me/', MobileMeView.as_view(), name='mobile-me-direct'),

    # 2. Studio Catalog & Schedule
    path('branches/nearest/', MobileNearestBranchView.as_view(), name='mobile-branches-nearest'),
    path('branches/', MobileBranchesView.as_view(), name='mobile-branches'),
    path('classes/', MobileClassesView.as_view(), name='mobile-classes'),
    path('trainers/', MobileTrainersView.as_view(), name='mobile-trainers'),
    path('schedule/', MobileScheduleView.as_view(), name='mobile-schedule'),
    path('schedule/<uuid:occurrence_id>/book/', MobileBookClassView.as_view(), name='mobile-schedule-book-direct'),

    # 3. Bookings, Reschedule, Free Trial, Cancellations & QR Pass
    path('bookings/claim-free-trial/', MobileClaimFreeTrialView.as_view(), name='mobile-claim-free-trial'),
    path('trials/book/', MobileClaimFreeTrialView.as_view(), name='mobile-trials-book'),
    path('trials/claim/', MobileClaimFreeTrialView.as_view(), name='mobile-trials-claim'),
    path('trials/available-slots/', MobileScheduleView.as_view(), name='mobile-trials-slots'),
    path('trial-bookings/', MobileClaimFreeTrialView.as_view(), name='mobile-trial-bookings'),
    path('bookings/book/', MobileBookClassView.as_view(), name='mobile-book-class'),
    path('bookings/my-bookings/', MobileMyBookingsView.as_view(), name='mobile-my-bookings'),
    path('bookings/', MobileMyBookingsView.as_view(), name='mobile-bookings-direct'),
    path('bookings/<uuid:booking_id>/cancel/', MobileCancelBookingView.as_view(), name='mobile-cancel-booking'),
    path('bookings/<uuid:booking_id>/reschedule/', MobileRescheduleBookingView.as_view(), name='mobile-reschedule-booking'),
    path('bookings/<uuid:booking_id>/qr-pass/', MobileQRPassView.as_view(), name='mobile-qr-pass'),
    path('qr-pass/', MobileQRPassView.as_view(), name='mobile-qr-pass-direct'),

    # 4. Personal Training (PT)
    path('pt/appointments/', MobilePTAppointmentsView.as_view(), name='mobile-pt-appointments'),
    path('pt/appointments/<uuid:appointment_id>/cancel/', MobileCancelPTAppointmentView.as_view(), name='mobile-pt-cancel'),

    # 5. Memberships, Packs, Credits & Checkout
    path('packages/', MobilePackagesView.as_view(), name='mobile-packages'),
    path('my-credits/', MobileMyCreditsView.as_view(), name='mobile-my-credits'),
    path('credits/', MobileMyCreditsView.as_view(), name='mobile-credits-direct'),
    path('coupons/', MobileDiscountCouponsView.as_view(), name='mobile-coupons'),
    path('discounts/', MobileDiscountCouponsView.as_view(), name='mobile-discounts'),
    path('coupons/validate/', MobileValidateCouponView.as_view(), name='mobile-validate-coupon'),
    path('checkout/create-order/', MobileCheckoutOrderView.as_view(), name='mobile-checkout-create-order'),
    path('checkout/verify/', MobileCheckoutVerifyView.as_view(), name='mobile-checkout-verify'),

    # 6. Onboarding & Health Assessments / PAR-Q
    path('onboarding/survey/', MobileOnboardingSurveyView.as_view(), name='mobile-onboarding-survey'),
    path('onboarding-survey/', MobileOnboardingSurveyView.as_view(), name='mobile-onboarding-survey-direct'),
    path('onboarding/submit/', MobileOnboardingSubmitView.as_view(), name='mobile-onboarding-submit'),
]
