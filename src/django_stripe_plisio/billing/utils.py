"""Вспомогательные функции биллинга."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_stripe_plisio.conf import PackageSettings

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser


def user_external_id(user: AbstractBaseUser) -> str:
    """Идентификатор пользователя для metadata провайдеров."""
    field_name = PackageSettings.user_id_field()
    if field_name == "pk":
        return str(user.pk)
    return str(getattr(user, field_name))
