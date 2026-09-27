"""Алиасы типов для аннотаций (используются только под TYPE_CHECKING)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypeAlias

    # Плагин django-stubs подставляет в FK модель из AUTH_USER_MODEL настроек mypy,
    # поэтому аннотируем ею; в рантайме это любая (в т.ч. кастомная) модель пользователя
    from django.contrib.auth.models import User

    UserType: TypeAlias = User
