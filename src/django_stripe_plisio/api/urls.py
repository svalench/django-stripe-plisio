from django.urls import path

from django_stripe_plisio.api.views import (
    BalanceLedgerListView,
    InvoiceDetailView,
    InvoiceListCreateView,
    PlisioInvoiceView,
    PriceListView,
    ProductListView,
    StripeCheckoutView,
)

urlpatterns = [
    path("products/", ProductListView.as_view(), name="dsp-api-products"),
    path("prices/", PriceListView.as_view(), name="dsp-api-prices"),
    path("invoices/", InvoiceListCreateView.as_view(), name="dsp-api-invoices"),
    path("invoices/<int:pk>/", InvoiceDetailView.as_view(), name="dsp-api-invoice-detail"),
    path("payments/stripe/checkout/", StripeCheckoutView.as_view(), name="dsp-api-stripe-checkout"),
    path("payments/plisio/invoice/", PlisioInvoiceView.as_view(), name="dsp-api-plisio-invoice"),
    path("balance/ledger/", BalanceLedgerListView.as_view(), name="dsp-api-ledger"),
]
