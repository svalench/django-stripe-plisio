from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from django_stripe_plisio.api.serializers import (
    BalanceLedgerSerializer,
    CheckoutSerializer,
    CreateInvoiceSerializer,
    InvoiceSerializer,
    PaymentAttemptSerializer,
    PriceSerializer,
    ProductSerializer,
)
from django_stripe_plisio.billing.enums import PaymentProvider
from django_stripe_plisio.billing.models import BalanceLedger, Invoice, Price, Product
from django_stripe_plisio.billing.services import create_invoice
from django_stripe_plisio.payments.services import create_checkout


class ProductListView(generics.ListAPIView):
    queryset = Product.objects.filter(is_active=True)
    serializer_class = ProductSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]


class PriceListView(generics.ListAPIView):
    queryset = Price.objects.filter(is_active=True).select_related("product")
    serializer_class = PriceSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]


class InvoiceListCreateView(generics.ListCreateAPIView):
    serializer_class = InvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Invoice.objects.filter(user=self.request.user).prefetch_related("lines")

    def create(self, request, *args, **kwargs):
        ser = CreateInvoiceSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        price = get_object_or_404(Price, pk=ser.validated_data["price_id"], is_active=True)
        promo = ser.validated_data.get("promo_code") or None
        invoice = create_invoice(
            user=request.user,
            price=price,
            provider=ser.validated_data["provider"],
            quantity=ser.validated_data["quantity"],
            promo_code=promo,
        )
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)


class InvoiceDetailView(generics.RetrieveAPIView):
    serializer_class = InvoiceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Invoice.objects.filter(user=self.request.user).prefetch_related("lines")


class StripeCheckoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ser = CheckoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        invoice = get_object_or_404(
            Invoice,
            pk=ser.validated_data["invoice_id"],
            user=request.user,
            provider=PaymentProvider.STRIPE,
        )
        attempt = create_checkout(invoice)
        return Response(PaymentAttemptSerializer(attempt).data, status=status.HTTP_201_CREATED)


class PlisioInvoiceView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ser = CheckoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        invoice = get_object_or_404(
            Invoice,
            pk=ser.validated_data["invoice_id"],
            user=request.user,
            provider=PaymentProvider.PLISIO,
        )
        attempt = create_checkout(invoice)
        return Response(PaymentAttemptSerializer(attempt).data, status=status.HTTP_201_CREATED)


class BalanceLedgerListView(generics.ListAPIView):
    serializer_class = BalanceLedgerSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = BalanceLedger.objects.filter(user=self.request.user)
        currency = self.request.query_params.get("currency")
        if currency:
            qs = qs.filter(currency=currency.upper())
        return qs
