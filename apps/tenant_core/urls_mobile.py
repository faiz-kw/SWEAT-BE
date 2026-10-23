from django.urls import path
from apps.authentication.views_otp import SendOTPView, VerifyOTPView
from . import views_wellness
from . import views_community
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
    MobileClassCategoriesView,
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
    MobileOrdersView,
    MobileOnboardingSurveyView,
    MobileOnboardingSubmitView,
    MobilePTAppointmentsView,
    MobileCancelPTAppointmentView,
    MobileMembershipActionsView,
    MobileMembershipRenewView,
    MobileMembershipUpgradeView,
    MobileMembershipExtendView,
    MobileMembershipFreezeView,
    MobileMembershipUnfreezeView,
)

app_name = 'mobile_api'

urlpatterns = [
    # 1. Auth & Profile
    path('auth/otp/send/', SendOTPView.as_view(), name='mobile-auth-otp-send'),
    path('auth/otp/verify/', VerifyOTPView.as_view(), name='mobile-auth-otp-verify'),
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
    path('categories/', MobileClassCategoriesView.as_view(), name='mobile-categories'),
    path('class-categories/', MobileClassCategoriesView.as_view(), name='mobile-class-categories'),
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
    path('memberships/actions/', MobileMembershipActionsView.as_view(), name='mobile-membership-actions'),
    path('memberships/renew/', MobileMembershipRenewView.as_view(), name='mobile-membership-renew'),
    path('memberships/upgrade/', MobileMembershipUpgradeView.as_view(), name='mobile-membership-upgrade'),
    path('memberships/extend/', MobileMembershipExtendView.as_view(), name='mobile-membership-extend'),
    path('memberships/freeze/', MobileMembershipFreezeView.as_view(), name='mobile-membership-freeze'),
    path('memberships/unfreeze/', MobileMembershipUnfreezeView.as_view(), name='mobile-membership-unfreeze'),
    path('packages/', MobilePackagesView.as_view(), name='mobile-packages'),
    path('my-credits/', MobileMyCreditsView.as_view(), name='mobile-my-credits'),
    path('credits/', MobileMyCreditsView.as_view(), name='mobile-credits-direct'),
    path('coupons/', MobileDiscountCouponsView.as_view(), name='mobile-coupons'),
    path('discounts/', MobileDiscountCouponsView.as_view(), name='mobile-discounts'),
    path('coupons/validate/', MobileValidateCouponView.as_view(), name='mobile-validate-coupon'),
    path('checkout/create-order/', MobileCheckoutOrderView.as_view(), name='mobile-checkout-create-order'),
    path('checkout/verify/', MobileCheckoutVerifyView.as_view(), name='mobile-checkout-verify'),
    path('orders/', MobileOrdersView.as_view(), name='mobile-orders'),

    # 6. Onboarding & Health Assessments / PAR-Q
    path('onboarding/survey/', MobileOnboardingSurveyView.as_view(), name='mobile-onboarding-survey'),
    path('onboarding-survey/', MobileOnboardingSurveyView.as_view(), name='mobile-onboarding-survey-direct'),
    path('onboarding/submit/', MobileOnboardingSubmitView.as_view(), name='mobile-onboarding-submit'),

    # 7. Nutrition & Wellness MVP (Milestone 1)
    path('wellness/foods/', views_wellness.FoodSearchMobileView.as_view(), name='mobile-wellness-foods'),
    path('wellness/foods/barcode/<str:barcode>/', views_wellness.FoodBarcodeMobileView.as_view(), name='mobile-wellness-food-barcode'),
    path('wellness/foods/analyze-image/', views_wellness.FoodImageAnalysisMobileView.as_view(), name='mobile-wellness-food-analyze-image'),
    path('wellness/targets/', views_wellness.NutritionTargetMobileView.as_view(), name='mobile-wellness-targets'),
    path('wellness/meal-logs/', views_wellness.MealLogListCreateMobileView.as_view(), name='mobile-wellness-meal-logs'),
    path('wellness/meal-logs/<uuid:pk>/', views_wellness.MealLogDetailMobileView.as_view(), name='mobile-wellness-meal-log-detail'),
    path('wellness/water-logs/', views_wellness.WaterLogListCreateMobileView.as_view(), name='mobile-wellness-water-logs'),
    path('wellness/water-logs/<uuid:pk>/', views_wellness.WaterLogDetailMobileView.as_view(), name='mobile-wellness-water-log-detail'),
    path('wellness/sleep-logs/', views_wellness.SleepLogListCreateMobileView.as_view(), name='mobile-wellness-sleep-logs'),
    path('wellness/sleep-logs/<uuid:pk>/', views_wellness.SleepLogDetailMobileView.as_view(), name='mobile-wellness-sleep-log-detail'),
    path('wellness/daily-summary/', views_wellness.DailySummaryMobileView.as_view(), name='mobile-wellness-daily-summary'),

    # 8. Community & Transformations Engine
    path('community/categories/', views_community.CommunityCategoriesMobileView.as_view(), name='mobile-community-categories'),
    path('community/feed/', views_community.CommunityFeedMobileView.as_view(), name='mobile-community-feed'),
    path('community/media/upload/', views_community.CommunityMediaUploadView.as_view(), name='mobile-community-media-upload'),
    path('community/posts/', views_community.CommunityPostCreateMobileView.as_view(), name='mobile-community-posts-create'),
    path('community/posts/<uuid:pk>/', views_community.CommunityPostDetailMobileView.as_view(), name='mobile-community-posts-detail'),
    path('community/posts/<uuid:pk>/react/', views_community.CommunityPostReactMobileView.as_view(), name='mobile-community-posts-react'),
    path('community/posts/<uuid:pk>/bookmark/', views_community.CommunityPostBookmarkMobileView.as_view(), name='mobile-community-posts-bookmark'),
    path('community/posts/<uuid:pk>/comments/', views_community.CommunityPostCommentsMobileView.as_view(), name='mobile-community-posts-comments'),
    path('community/report/', views_community.CommunityReportMobileView.as_view(), name='mobile-community-report'),
    path('community/requests/', views_community.CommunityRequestsMobileView.as_view(), name='mobile-community-requests'),
    path('community/requests/<uuid:pk>/upvote/', views_community.CommunityRequestUpvoteMobileView.as_view(), name='mobile-community-requests-upvote'),
]
