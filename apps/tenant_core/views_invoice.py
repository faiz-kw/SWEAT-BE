"""Mobile Package History: single endpoint returning all memberships with an inline signed invoice_url each,
plus a public endpoint that renders the invoice as a PDF."""
import io
import uuid
from decimal import Decimal
from html import escape
from django.http import HttpResponse
from django.core import signing
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework import status

from .models_commerce import MemberInvoice, OrderItem
from .models_memberships import Membership, MembershipEntitlement
from .views_mobile import _resolve_mobile_tenant_and_db, _get_or_create_user_profile
from .context import get_tenant_db_alias, set_tenant_db_alias

INVOICE_SALT = 'sweat.invoice.v1'
MAX_AGE = 60 * 60 * 24 * 60  # 60 days


def _sign(invoice_id, alias):
    return signing.dumps({'i': str(invoice_id), 'a': alias}, salt=INVOICE_SALT)


def _member_name(profile):
    if not profile:
        return 'Member'
    v = getattr(profile, 'full_name', None)
    if v:
        return v
    u = getattr(profile, 'user', None)
    if u:
        fn = (getattr(u, 'full_name', None) or '').strip()
        if fn:
            return fn
        nm = ' '.join(p for p in [getattr(u, 'first_name', '') or '', getattr(u, 'last_name', '') or ''] if p).strip()
        if nm:
            return nm
    return 'Member'


def _invoice_for_order(alias, order):
    if not order:
        return None
    inv = MemberInvoice.objects.using(alias).filter(order=order).order_by('-issued_at').first()
    if inv:
        return inv
    return MemberInvoice.objects.using(alias).create(
        invoice_number=f"INV-{uuid.uuid4().hex[:8].upper()}",
        order=order, user_profile=order.user_profile, branch=order.branch,
        subtotal=order.subtotal or Decimal('0.00'),
        discount_amount=order.discount_amount or Decimal('0.00'),
        reward_amount=getattr(order, 'reward_amount', Decimal('0.00')) or Decimal('0.00'),
        tax_amount=order.tax_amount or Decimal('0.00'),
        total_amount=order.total_amount or Decimal('0.00'),
        currency=order.currency or 'INR',
        status='PAID' if order.status == 'PAID' else 'ISSUED',
        issued_at=timezone.now())


class MobileMembershipHistoryView(APIView):
    """GET /api/v1/mobile/memberships/history/ -> [{...membership..., invoice_url}]."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        alias, _ = _resolve_mobile_tenant_and_db(request)
        if not alias:
            alias = get_tenant_db_alias() or 'default'
        set_tenant_db_alias(alias)
        profile = _get_or_create_user_profile(request.user, alias)
        today = timezone.now().date()
        mems = (Membership.objects.using(alias)
                .filter(user_profile=profile)
                .select_related('package', 'source_order', 'home_branch')
                .order_by('-start_date', '-created_at'))
        out = []
        for m in mems:
            total = 0
            used = 0
            unlimited = False
            for e in MembershipEntitlement.objects.using(alias).filter(membership=m):
                if getattr(e, 'is_unlimited', False):
                    unlimited = True
                    continue
                total += int(getattr(e, 'allocated_units', 0) or 0)
                used += int(getattr(e, 'consumed_units', 0) or 0)
            inv = _invoice_for_order(alias, getattr(m, 'source_order', None))
            invoice_url = None
            invoice_number = None
            if inv:
                invoice_number = inv.invoice_number
                base = request.build_absolute_uri('/api/v1/mobile/invoice/%s/' % inv.id)
                invoice_url = '%s?token=%s' % (base, _sign(inv.id, alias))
            out.append({
                'id': str(m.id),
                'membership_number': getattr(m, 'membership_number', None),
                'package_name': getattr(m.package, 'name', 'Membership'),
                'status': m.status,
                'is_active': m.status == 'ACTIVE' and (not m.end_date or m.end_date >= today),
                'sessions_unlimited': unlimited,
                'sessions_total': total,
                'sessions_used': used,
                'sessions_remaining': max(0, total - used),
                'sessions_label': 'Unlimited' if unlimited else ('%d / %d used' % (used, total)),
                'start_date': m.start_date.isoformat() if m.start_date else None,
                'end_date': m.end_date.isoformat() if m.end_date else None,
                'branch_name': getattr(m.home_branch, 'name', '') if getattr(m, 'home_branch', None) else '',
                'invoice_number': invoice_number,
                'invoice_url': invoice_url,
            })
        return Response({'history': out, 'count': len(out)}, status=status.HTTP_200_OK)


class MobileInvoiceRenderView(APIView):
    """GET /api/v1/mobile/invoice/<invoice_id>/?token=...  -> PDF invoice (public, signed)."""
    permission_classes = [AllowAny]

    def get(self, request, invoice_id):
        token = request.query_params.get('token') or ''
        try:
            data = signing.loads(token, salt=INVOICE_SALT, max_age=MAX_AGE)
        except Exception:
            return HttpResponse('Invalid or expired invoice link.', status=403, content_type='text/plain')
        if str(data.get('i')) != str(invoice_id):
            return HttpResponse('Invalid invoice link.', status=403, content_type='text/plain')
        alias = data.get('a') or get_tenant_db_alias() or 'default'
        set_tenant_db_alias(alias)
        inv = (MemberInvoice.objects.using(alias)
               .filter(id=invoice_id).select_related('order', 'user_profile', 'branch').first())
        if not inv:
            return HttpResponse('Invoice not found.', status=404, content_type='text/plain')
        items = list(OrderItem.objects.using(alias).filter(order=inv.order)) if inv.order else []
        html = _render_pdf_html(inv, items)
        # html fallback if requested
        if (request.query_params.get('format') or '').lower() == 'html':
            return HttpResponse(html, content_type='text/html')
        try:
            from xhtml2pdf import pisa
            buf = io.BytesIO()
            pisa.CreatePDF(src=html, dest=buf, encoding='utf-8')
            pdf = buf.getvalue()
            if not pdf:
                raise RuntimeError('empty pdf')
            resp = HttpResponse(pdf, content_type='application/pdf')
            resp['Content-Disposition'] = 'inline; filename="%s.pdf"' % (inv.invoice_number or 'invoice')
            return resp
        except Exception:
            return HttpResponse(html, content_type='text/html')


def _money(v, cur='INR'):
    pre = 'Rs. ' if cur == 'INR' else (cur + ' ')
    try:
        return '%s%0.2f' % (pre, float(v or 0))
    except Exception:
        return '%s0.00' % pre


def _render_pdf_html(inv, items):
    cur = inv.currency or 'INR'
    member = escape(_member_name(inv.user_profile))
    branch = escape(getattr(inv.branch, 'name', '') or '')
    order_no = escape(getattr(inv.order, 'order_number', '') or '')
    issued = inv.issued_at.strftime('%d %b %Y, %I:%M %p') if inv.issued_at else ''
    rows = ''
    if items:
        for it in items:
            nm = escape(getattr(it, 'item_name_snapshot', '') or 'Item')
            qty = '%0.0f' % float(getattr(it, 'quantity', 1) or 1)
            unit = _money(getattr(it, 'unit_price_snapshot', 0), cur)
            disc = _money(getattr(it, 'discount_amount', 0), cur)
            amt = _money(getattr(it, 'total_amount', 0), cur)
            rows += ('<tr><td>%s</td><td align="center">%s</td><td align="right">%s</td>'
                     '<td align="right">%s</td><td align="right">%s</td></tr>' % (nm, qty, unit, disc, amt))
    else:
        rows = ('<tr><td>Membership</td><td align="center">1</td><td align="right">%s</td>'
                '<td align="right">%s</td><td align="right">%s</td></tr>'
                % (_money(inv.subtotal, cur), _money(inv.discount_amount, cur), _money(inv.total_amount, cur)))
    return """<!doctype html><html><head><meta charset="utf-8"><style>
@page { size: a4; margin: 1.6cm; }
body { font-family: Helvetica; color: #1a1a1a; font-size: 11px; }
.brand { font-size: 24px; font-weight: bold; letter-spacing: 1px; }
.muted { color: #888; font-size: 10px; }
.hr { border-bottom: 1.5px solid #222; margin: 8px 0 12px 0; }
table { width: 100%%; border-collapse: collapse; }
.meta td { padding: 2px 0; font-size: 11px; }
.items th { background-color: #f3f3f3; text-align: left; padding: 7px 8px; font-size: 10px; color: #555; border-bottom: 1px solid #ccc; }
.items td { padding: 7px 8px; border-bottom: 1px solid #eee; }
.tot td { padding: 4px 8px; color: #555; }
.grand td { font-size: 14px; font-weight: bold; color: #111; border-top: 1.5px solid #ccc; padding-top: 8px; }
.tag { color: %(sc)s; font-weight: bold; font-size: 11px; }
.ft { color: #aaa; font-size: 9px; text-align: center; margin-top: 24px; }
</style></head><body>
<table><tr>
  <td><div class="brand">SWEAT</div><div class="muted">Tax Invoice</div></td>
  <td align="right"><div class="tag">%(status)s</div><div class="muted">%(inv_no)s</div></td>
</tr></table>
<div class="hr"></div>
<table class="meta">
  <tr><td><b>Billed To:</b> %(member)s</td><td align="right"><b>Date:</b> %(issued)s</td></tr>
  <tr><td><b>Branch:</b> %(branch)s</td><td align="right"><b>Order:</b> %(order_no)s</td></tr>
</table>
<br/>
<table class="items">
  <tr><th>Description</th><th align="center">Qty</th><th align="right">Unit</th><th align="right">Discount</th><th align="right">Amount</th></tr>
  %(rows)s
</table>
<br/>
<table class="tot">
  <tr><td align="right">Subtotal</td><td align="right" width="140">%(subtotal)s</td></tr>
  <tr><td align="right">Discount</td><td align="right">- %(discount)s</td></tr>
  <tr><td align="right">Tax</td><td align="right">%(tax)s</td></tr>
  <tr class="grand"><td align="right">Total Paid</td><td align="right">%(total)s</td></tr>
</table>
<div class="ft">This is a computer-generated invoice.</div>
</body></html>""" % {
        'inv_no': escape(inv.invoice_number or ''), 'order_no': order_no, 'issued': escape(issued),
        'member': member, 'branch': branch, 'status': escape(inv.status or ''),
        'sc': '#0a7d33' if inv.status == 'PAID' else '#b45309', 'rows': rows,
        'subtotal': _money(inv.subtotal, cur), 'discount': _money(inv.discount_amount, cur),
        'tax': _money(inv.tax_amount, cur), 'total': _money(inv.total_amount, cur),
    }


class WebOrderInvoiceView(APIView):
    """GET /api/v1/tenant/invoice/order/<order_id>/ -> PDF invoice (authenticated CRM/web)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        from .models_commerce import Order
        alias = get_tenant_db_alias() or 'default'
        set_tenant_db_alias(alias)
        order = Order.objects.using(alias).filter(id=order_id).first()
        if not order:
            return HttpResponse('Order not found.', status=404, content_type='text/plain')
        inv = _invoice_for_order(alias, order)
        items = list(OrderItem.objects.using(alias).filter(order=order))
        html = _render_pdf_html(inv, items)
        if (request.query_params.get('format') or '').lower() == 'html':
            return HttpResponse(html, content_type='text/html')
        try:
            from xhtml2pdf import pisa
            buf = io.BytesIO()
            pisa.CreatePDF(src=html, dest=buf, encoding='utf-8')
            pdf = buf.getvalue()
            if not pdf:
                raise RuntimeError('empty pdf')
            resp = HttpResponse(pdf, content_type='application/pdf')
            resp['Content-Disposition'] = 'inline; filename="%s.pdf"' % (inv.invoice_number or 'invoice')
            return resp
        except Exception:
            return HttpResponse(html, content_type='text/html')


class WebInvoiceRenderView(APIView):
    """GET /api/v1/tenant/invoice/<invoice_id>/ -> PDF invoice by invoice id (authenticated CRM/web)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, invoice_id):
        alias = get_tenant_db_alias() or 'default'
        set_tenant_db_alias(alias)
        inv = (MemberInvoice.objects.using(alias)
               .filter(id=invoice_id).select_related('order', 'user_profile', 'branch').first())
        if not inv:
            return HttpResponse('Invoice not found.', status=404, content_type='text/plain')
        items = list(OrderItem.objects.using(alias).filter(order=inv.order)) if inv.order else []
        html = _render_pdf_html(inv, items)
        if (request.query_params.get('format') or '').lower() == 'html':
            return HttpResponse(html, content_type='text/html')
        try:
            from xhtml2pdf import pisa
            buf = io.BytesIO()
            pisa.CreatePDF(src=html, dest=buf, encoding='utf-8')
            pdf = buf.getvalue()
            if not pdf:
                raise RuntimeError('empty pdf')
            resp = HttpResponse(pdf, content_type='application/pdf')
            resp['Content-Disposition'] = 'inline; filename="%s.pdf"' % (inv.invoice_number or 'invoice')
            return resp
        except Exception:
            return HttpResponse(html, content_type='text/html')
