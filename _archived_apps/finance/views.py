"""
Views for Invoices, Payments, Coupon redemptions, and Financial KPI summaries.
"""

from rest_framework import viewsets, permissions, status, filters
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Sum
from decimal import Decimal
import uuid

from .models import Invoice, Payment, Coupon, InvoiceStatus, PaymentStatus, PaymentMethod
from .serializers import InvoiceSerializer, PaymentSerializer, CouponSerializer


class CouponViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Discount & Promotional Coupons.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CouponSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['code', 'description']
    ordering = ['-valid_from']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            return Coupon.objects.all()
        return Coupon.objects.filter(tenant=user.tenant)

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        cpn_id = serializer.validated_data.get('id') or f"CPN-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=cpn_id, tenant=tenant)


class PaymentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Viewing and Logging Payment settlements.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PaymentSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['member__name', 'transaction_id']
    ordering_fields = ['paid_at', 'amount']
    ordering = ['-paid_at']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            return Payment.objects.all().select_related('member', 'invoice', 'tenant')
        return Payment.objects.filter(tenant=user.tenant).select_related('member', 'invoice', 'tenant')

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        pay_id = serializer.validated_data.get('id') or f"PAY-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=pay_id, tenant=tenant)


class InvoiceViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Invoices with Razorpay / Gateway Settlement actions.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = InvoiceSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['id', 'member__name', 'description']
    ordering_fields = ['due_date', 'total_amount', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = Invoice.objects.all()
        else:
            qs = Invoice.objects.filter(tenant=user.tenant)

        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)

        member_id = self.request.query_params.get('member')
        if member_id:
            qs = qs.filter(member_id=member_id)

        location_id = self.request.query_params.get('location')
        if location_id and location_id != 'all':
            qs = qs.filter(location_id=location_id)

        return qs.select_related('member', 'subscription', 'location', 'coupon', 'tenant').prefetch_related('payments')

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        subtotal = serializer.validated_data.get('subtotal', Decimal('0.00'))
        coupon = serializer.validated_data.get('coupon')

        discount_amount = serializer.validated_data.get('discount_amount')
        if discount_amount is None or discount_amount == Decimal('0.00'):
            if coupon:
                discount_amount = Decimal(str(coupon.calculate_discount(float(subtotal))))
                coupon.times_used += 1
                coupon.save(update_fields=['times_used'])
            else:
                discount_amount = Decimal('0.00')

        tax_amount = serializer.validated_data.get('tax_amount')
        if tax_amount is None or tax_amount == Decimal('0.00'):
            taxable_base = max(Decimal('0.00'), subtotal - discount_amount)
            tax_amount = round(taxable_base * Decimal('0.18'), 2)

        total_amount = serializer.validated_data.get('total_amount')
        if total_amount is None or total_amount == Decimal('0.00'):
            total_amount = round(subtotal - discount_amount + tax_amount, 2)

        inv_id = serializer.validated_data.get('id') or f"INV-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(
            id=inv_id,
            tenant=tenant,
            discount_amount=discount_amount,
            tax_amount=tax_amount,
            total_amount=total_amount
        )

    @action(detail=True, methods=['post'], url_path='pay')
    def record_payment(self, request, pk=None):
        """
        Settles invoice with payment (Razorpay / UPI / Cash) and marks invoice 'Paid'.
        """
        invoice = self.get_object()
        amount = request.data.get('amount', invoice.total_amount)
        payment_method = request.data.get('payment_method', 'Razorpay')
        transaction_id = request.data.get('transaction_id', f"rzp_tx_{uuid.uuid4().hex[:8]}")

        # 1. Create Payment record
        payment = Payment.objects.create(
            id=f"PAY-{uuid.uuid4().hex[:6].upper()}",
            tenant=invoice.tenant,
            invoice=invoice,
            member=invoice.member,
            amount=amount,
            payment_method=payment_method,
            transaction_id=transaction_id,
            status=PaymentStatus.SUCCESS,
            paid_at=timezone.now(),
        )

        # 2. Update Invoice to Paid
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = payment.paid_at
        invoice.save(update_fields=['status', 'paid_at'])

        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_200_OK)


class FinanceSummaryView(APIView):
    """
    High-level financial KPIs: Total Revenue, Outstanding Receivables, Paid & Overdue invoice metrics.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        tenant = user.tenant

        if user.is_superuser and not tenant:
            invoices = Invoice.objects.all()
            payments = Payment.objects.filter(status=PaymentStatus.SUCCESS)
        else:
            invoices = Invoice.objects.filter(tenant=tenant)
            payments = Payment.objects.filter(tenant=tenant, status=PaymentStatus.SUCCESS)

        total_revenue = payments.aggregate(sum=Sum('amount'))['sum'] or 0.00
        total_outstanding = invoices.filter(status__in=[InvoiceStatus.ISSUED, InvoiceStatus.OVERDUE]).aggregate(sum=Sum('total_amount'))['sum'] or 0.00
        paid_count = invoices.filter(status=InvoiceStatus.PAID).count()
        overdue_count = invoices.filter(status=InvoiceStatus.OVERDUE).count()

        return Response({
            'total_revenue': float(total_revenue),
            'total_outstanding': float(total_outstanding),
            'paid_invoices_count': paid_count,
            'overdue_invoices_count': overdue_count,
        }, status=status.HTTP_200_OK)
