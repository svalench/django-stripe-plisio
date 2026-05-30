from rest_framework import serializers

from django_stripe_plisio.billing.models import (
    BalanceLedger,
    Invoice,
    InvoiceLine,
    Price,
    Product,
)
from django_stripe_plisio.payments.models import PaymentAttempt


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "code", "name", "description", "is_active", "metadata")


class PriceSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(source="product.code", read_only=True)

    class Meta:
        model = Price
        fields = (
            "id",
            "product",
            "product_code",
            "currency",
            "amount_minor",
            "billing_period",
            "is_active",
            "stripe_price_id",
        )


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = ("description", "quantity", "unit_amount_minor", "line_total_minor", "currency")


class InvoiceSerializer(serializers.ModelSerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = (
            "id",
            "status",
            "provider",
            "currency",
            "subtotal_minor",
            "discount_minor",
            "total_minor",
            "payment_url",
            "external_id",
            "expires_at",
            "paid_at",
            "metadata",
            "lines",
            "created_at",
        )
        read_only_fields = fields


class CreateInvoiceSerializer(serializers.Serializer):
    price_id = serializers.IntegerField()
    quantity = serializers.IntegerField(default=1, min_value=1)
    provider = serializers.ChoiceField(choices=["stripe", "plisio"])
    promo_code = serializers.CharField(required=False, allow_blank=True)


class PaymentAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentAttempt
        fields = (
            "id",
            "invoice",
            "provider",
            "status",
            "external_id",
            "payment_url",
            "error_code",
            "error_message",
            "created_at",
        )


class BalanceLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = BalanceLedger
        fields = (
            "id",
            "entry_type",
            "amount_minor",
            "currency",
            "reference",
            "note",
            "created_at",
        )


class CheckoutSerializer(serializers.Serializer):
    invoice_id = serializers.IntegerField()
