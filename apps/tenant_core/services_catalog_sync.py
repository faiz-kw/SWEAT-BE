"""
apps/tenant_core/services_catalog_sync.py — Dynamic Shop Catalog Ingestion & Sync Service

Dynamically fetches live product listings, images, pricing, and descriptions from the official
store reference (https://sweatfitwellness.com/shop/) via its REST API:
    https://sweatfitwellness.com/wp-json/wc/store/v1/products?per_page=100

Features:
1. Fully dynamic, maintainable, and scalable: no hardcoded product records or static prices.
2. Normalizes HTML entities, Unicode punctuation, and category mappings.
3. Idempotently creates or updates Product records in the tenant database.
4. Preserves primary and multi-image gallery URLs.
5. Accurately provisions initial stock and cost price calculation.
"""

import html
import json
import logging
import re
import urllib.request
import urllib.error
from decimal import Decimal
from typing import Dict, List, Any, Optional

from django.db import transaction
from django.utils import timezone

from .models_org import Organization, Branch
from .models_inventory import Product

logger = logging.getLogger(__name__)


def clean_html_text(text: Optional[str]) -> str:
    """Cleans HTML entities and strips unwanted markup while preserving readable structure."""
    if not text:
        return ""
    unescaped = html.unescape(text)
    # Replace unicode dash variants and common symbols with clean ASCII/standard characters
    unescaped = (
        unescaped.replace('\u2013', '-')
        .replace('\u2014', '-')
        .replace('\u2018', "'")
        .replace('\u2019', "'")
        .replace('\u201c', '"')
        .replace('\u201d', '"')
        .replace('&#8211;', '-')
        .replace('&#8212;', '-')
        .replace('&#038;', '&')
        .replace('&amp;', '&')
        .replace('&nbsp;', ' ')
    )
    # Remove excessive blank lines or trailing whitespace
    return re.sub(r'\n{3,}', '\n\n', unescaped).strip()


def strip_html_tags(html_content: Optional[str]) -> str:
    """Converts HTML markup into clean readable plaintext descriptions."""
    if not html_content:
        return ""
    text = clean_html_text(html_content)
    # Replace break tags and list items with clean line breaks
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</h[1-6]>', '\n\n', text, flags=re.IGNORECASE)
    # Strip remaining HTML tags
    clean = re.sub(r'<[^>]+>', '', text)
    # Clean up whitespace
    clean = re.sub(r'[ \t]+', ' ', clean)
    clean = re.sub(r'\n{3,}', '\n\n', clean)
    return clean.strip()


class ShopCatalogSyncService:
    """
    Authoritative ingestion service for synchronizing products from Sweat Fit Wellness store.
    """
    DEFAULT_SOURCE_API_URL = "https://sweatfitwellness.com/wp-json/wc/store/v1/products?per_page=100"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SWEAT-SaaS-CatalogSync/1.0"

    @classmethod
    def fetch_live_products(cls, source_url: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Dynamically fetches raw product objects from the store API.
        """
        url = source_url or cls.DEFAULT_SOURCE_API_URL
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': cls.USER_AGENT,
                'Accept': 'application/json',
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                content = response.read().decode('utf-8')
                data = json.loads(content)
                if not isinstance(data, list):
                    logger.error("Unexpected response payload type: %s", type(data))
                    return []
                logger.info("Successfully fetched %d live products from %s", len(data), url)
                return data
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            logger.error("Failed to fetch live products from source URL %s: %s", url, exc)
            raise RuntimeError(f"Failed to fetch live catalog from {url}: {exc}") from exc

    @classmethod
    def map_category_and_type(cls, raw_categories: List[Dict[str, Any]]) -> tuple[str, str]:
        """
        Maps source WooCommerce category tags to SWEAT CRM internal CATEGORY_CHOICES and PRODUCT_TYPE_CHOICES.
        """
        cat_names = [c.get('name', '').lower() for c in raw_categories]
        cat_slugs = [c.get('slug', '').lower() for c in raw_categories]
        combined = set(cat_names + cat_slugs)

        if any('ebook' in c or 'e-book' in c or 'digital' in c for c in combined):
            return 'Digital Guides', 'Digital Download'
        elif any('supplements' in c or 'nutrition' in c or 'protein' in c for c in combined):
            return 'Supplements', 'Supplement / Nutrition'
        elif any('apparel' in c or 'clothing' in c or 'wear' in c for c in combined):
            return 'Apparel', 'Physical Goods'
        elif any('beverages' in c or 'drinks' in c for c in combined):
            return 'Beverages', 'Physical Goods'
        elif any('equipment' in c or 'gear' in c for c in combined):
            return 'Equipment', 'Physical Goods'
        elif any('accessories' in c or 'accessory' in c or 'socks' in c for c in combined):
            return 'Accessories', 'Physical Goods'
        elif any('snacks' in c or 'food' in c for c in combined):
            return 'Snacks', 'Physical Goods'
        else:
            return 'Accessories', 'Physical Goods'

    @classmethod
    def transform_product_payload(cls, raw_item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transforms a raw WooCommerce product into a clean dictionary ready for Product model upsert.
        """
        raw_id = raw_item.get('id')
        raw_name = clean_html_text(raw_item.get('name') or f"Product #{raw_id}")
        raw_sku = (raw_item.get('sku') or '').strip()
        slug = raw_item.get('slug') or f"product-{raw_id}"

        # Category & Product Type
        category, product_type = cls.map_category_and_type(raw_item.get('categories') or [])

        # Stable, deterministic SKU
        if not raw_sku:
            prefix = "EBOOK" if product_type == 'Digital Download' else "ACC"
            sku = f"SFW-{prefix}-{raw_id}"
        else:
            sku = raw_sku

        # Pricing (WooCommerce store API returns prices in integer minor units, e.g. "39900" = INR 399.00)
        prices_obj = raw_item.get('prices') or {}
        raw_price_minor = prices_obj.get('price') or prices_obj.get('sale_price') or prices_obj.get('regular_price') or 0
        try:
            price_inr = round(Decimal(str(raw_price_minor)) / Decimal('100.0'), 2)
        except Exception:
            price_inr = Decimal('0.00')

        # Estimated cost price (60% of retail price)
        cost_price_inr = round(price_inr * Decimal('0.60'), 2)

        # Images
        raw_images = raw_item.get('images') or []
        image_urls = [img.get('src') for img in raw_images if img.get('src')]
        primary_image = image_urls[0] if image_urls else None

        # Stock quantity: digital downloads have unlimited stock; physical goods use store max or default 35
        is_in_stock = bool(raw_item.get('is_in_stock', True))
        add_to_cart = raw_item.get('add_to_cart') or {}
        max_allowed = add_to_cart.get('maximum')
        if product_type == 'Digital Download':
            stock_qty = 9999
            status = 'In Stock'
        elif is_in_stock:
            stock_qty = max(10, int(max_allowed)) if max_allowed and max_allowed > 0 else 35
            status = 'In Stock'
        else:
            stock_qty = 0
            status = 'Out of Stock'


        # Description
        raw_desc = raw_item.get('description') or raw_item.get('short_description') or ''
        description = strip_html_tags(raw_desc)
        if not description:
            description = f"Official {raw_name} from Sweat Fit Wellness."

        return {
            'sku': sku,
            'name': raw_name,
            'category': category,
            'product_type': product_type,
            'price': price_inr,
            'cost_price': cost_price_inr,
            'stock_quantity': stock_qty,
            'image_url': primary_image,
            'images': image_urls,
            'description': description,
            'status': status,
            'is_active': True,
        }

    @classmethod
    def sync_to_database(
        cls,
        db_alias: str = 'default',
        organization_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        products_data: Optional[List[Dict[str, Any]]] = None,
        source_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes dynamic synchronization of products into the specified tenant database.
        """
        if products_data is None:
            products_data = cls.fetch_live_products(source_url=source_url)

        if not products_data:
            logger.warning("No products fetched to sync.")
            return {'total': 0, 'created': 0, 'updated': 0, 'products': []}

        from config.routers import set_tenant_db_alias, get_tenant_db_alias

        prev_alias = get_tenant_db_alias()
        set_tenant_db_alias(db_alias)
        try:
            # Resolve organization
            org = None
            if organization_id:
                org = Organization.objects.using(db_alias).filter(id=organization_id).first()
            if not org:
                org = Organization.objects.using(db_alias).filter(status='ACTIVE').first()
            if not org:
                org = Organization.objects.using(db_alias).first()

            if not org:
                raise RuntimeError(f"No Organization found in database '{db_alias}' to associate products with.")

            branch = None
            if branch_id:
                branch = Branch.objects.using(db_alias).filter(id=branch_id).first()

            created_count = 0
            updated_count = 0
            synced_products = []

            with transaction.atomic(using=db_alias):
                for raw_item in products_data:
                    payload = cls.transform_product_payload(raw_item)
                    sku = payload['sku']

                    # Look up existing product by SKU or exact Name under this organization
                    existing_product = Product.objects.using(db_alias).filter(
                        organization=org,
                        sku=sku
                    ).first()

                    if not existing_product:
                        existing_product = Product.objects.using(db_alias).filter(
                            organization=org,
                            name=payload['name']
                        ).first()

                    if existing_product:
                        existing_product.name = payload['name']
                        existing_product.category = payload['category']
                        existing_product.product_type = payload['product_type']
                        existing_product.price = payload['price']
                        existing_product.cost_price = payload['cost_price']
                        existing_product.stock_quantity = payload['stock_quantity']
                        existing_product.image_url = payload['image_url']
                        existing_product.images = payload['images']
                        existing_product.description = payload['description']
                        existing_product.status = payload['status']
                        existing_product.is_active = True
                        if branch:
                            existing_product.branch = branch
                        existing_product.save(using=db_alias)
                        updated_count += 1
                        synced_products.append(existing_product)
                    else:
                        new_product = Product.objects.using(db_alias).create(
                            organization=org,
                            branch=branch,
                            sku=sku,
                            name=payload['name'],
                            category=payload['category'],
                            product_type=payload['product_type'],
                            price=payload['price'],
                            cost_price=payload['cost_price'],
                            stock_quantity=payload['stock_quantity'],
                            image_url=payload['image_url'],
                            images=payload['images'],
                            description=payload['description'],
                            status=payload['status'],
                            is_active=True,
                        )
                        created_count += 1
                        synced_products.append(new_product)

            logger.info(
                "Sync complete on db '%s': total=%d, created=%d, updated=%d",
                db_alias, len(products_data), created_count, updated_count
            )

            return {
                'total': len(products_data),
                'created': created_count,
                'updated': updated_count,
                'organization': org.name,
                'products': [
                    {
                        'id': str(p.id),
                        'sku': p.sku,
                        'name': p.name,
                        'price': float(p.price),
                        'category': p.category,
                        'stock': p.stock_quantity,
                        'image': p.image_url,
                    }
                    for p in synced_products
                ],
            }
        finally:
            set_tenant_db_alias(prev_alias)

