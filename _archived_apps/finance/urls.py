"""
URL routing for Finance & Billing app.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import InvoiceViewSet, PaymentViewSet, CouponViewSet, FinanceSummaryView

router = DefaultRouter()
router.register('coupons', CouponViewSet, basename='finance-coupons')
router.register('invoices', InvoiceViewSet, basename='finance-invoices')
router.register('payments', PaymentViewSet, basename='finance-payments')


urlpatterns = [
    # Summary KPI endpoint: GET /api/v1/finance/summary/
    path('summary/', FinanceSummaryView.as_view(), name='finance-summary'),

    # CRUD Endpoints
    path('', include(router.urls)),
]
