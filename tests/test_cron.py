import pytest

from django_stripe_plisio.cron import build_cronjobs


@pytest.mark.django_db
def test_build_cronjobs_enabled(settings):
    settings.DJANGO_STRIPE_PLISIO_CRON = {
        "sync_invoices": {"enabled": True, "schedule": "*/10 * * * *"},
        "expire_invoices": {"enabled": True, "schedule": "0 * * * *"},
    }
    jobs = build_cronjobs()
    assert len(jobs) == 2
    assert jobs[0][2] == ["dsp_sync_invoices"]
    assert jobs[1][2] == ["dsp_expire_invoices"]


@pytest.mark.django_db
def test_build_cronjobs_disabled(settings):
    settings.DJANGO_STRIPE_PLISIO_CRON = {
        "sync_invoices": {"enabled": False, "schedule": "*/10 * * * *"},
        "expire_invoices": {"enabled": True, "schedule": "0 * * * *"},
    }
    jobs = build_cronjobs()
    assert len(jobs) == 1
    assert jobs[0][2] == ["dsp_expire_invoices"]


@pytest.mark.django_db
def test_build_cronjobs_extra(settings):
    settings.DJANGO_STRIPE_PLISIO_CRON = {
        "sync_invoices": {"enabled": True, "schedule": "*/5 * * * *"},
    }
    extra = [("0 0 * * *", "myapp.tasks.daily", [])]
    jobs = build_cronjobs(extra=extra)
    assert len(jobs) == 2
    assert jobs[-1] == extra[0]
