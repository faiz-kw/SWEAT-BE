"""
Serializers for Module: Retail Products, Pro Shop & Inventory
"""

from rest_framework import serializers
from .models_inventory import Product


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = [
            'id',
            'name',
            'category',
            'product_type',
            'sku',
            'price',
            'cost_price',
            'stock_quantity',
            'image_url',
            'images',
            'video_url',
            'description',
            'status',
            'is_active',
            'branch',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Ensure images is always a list
        if not isinstance(ret.get('images'), list):
            ret['images'] = [ret['images']] if ret.get('images') else []
        return ret


from .models_inventory import InventoryMovement


class InventoryMovementSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    product_category = serializers.CharField(source='product.category', read_only=True)

    class Meta:
        model = InventoryMovement
        fields = [
            'id',
            'product',
            'product_name',
            'product_sku',
            'product_category',
            'movement_type',
            'quantity_change',
            'balance_after',
            'notes',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']

