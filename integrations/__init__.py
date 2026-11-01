"""Every third-party provider adapter lives here, and ONLY here.

WHY A TOP-LEVEL PACKAGE (next to `base/` and `servers/`, not `servers/integrations/`)
------------------------------------------------------------------------------------
`servers/` is the Django apps (models, views, business rules); `base/` is shared platform
code. An adapter is neither: it is the thin edge that speaks a vendor's SDK or HTTP API. Making
it a sibling gives the vendor boundary one obvious line: business code (`base/`, `servers/`)
imports the capability interfaces and registries below, never a vendor SDK, and
`tests/test_vendor_boundary.py` fails CI the moment one does.

LAYOUT (grouped by capability, one module per vendor)
    email/     ses
    sms/       msg91, twilio, sns, sns_otp (the login one-time code, behaviour unchanged)
    push/      fcm, sns, fcm_direct (the direct firebase_admin sender, behaviour unchanged)
    payments/  cashfree, razorpay (stub), stripe (stub), registry
    payouts/   cashfree, razorpay (stub), stripe_connect (stub)
    storage/   s3 (S3 / Cloudflare R2), errors

SWAPPING A PROVIDER IS CONFIGURATION ONLY
    notifications   settings.NOTIFY_PROVIDERS (env NOTIFY_PROVIDERS_JSON), channel -> country -> name
    payments        settings.PAYMENT_PROVIDERS (env PAYMENT_PROVIDERS_JSON), country -> name
    payouts         settings.PAYOUT_PROVIDERS  (env PAYOUT_PROVIDERS_JSON),  country -> name

This package is plain Python, not a Django app: it has no models and no migrations.
"""
