"""Tests for application exception user_message propagation."""

from app.exceptions import AppError, ExportValidationError


def test_export_validation_error_uses_message_as_user_message():
    exc = ExportValidationError("По указанным фильтрам сделки не найдены в локальной БД")
    assert str(exc) == "По указанным фильтрам сделки не найдены в локальной БД"
    assert exc.user_message == "По указанным фильтрам сделки не найдены в локальной БД"


def test_export_validation_error_explicit_user_message_takes_priority():
    exc = ExportValidationError(
        "internal detail",
        user_message="Понятное сообщение для пользователя",
    )
    assert str(exc) == "internal detail"
    assert exc.user_message == "Понятное сообщение для пользователя"


def test_app_error_default_user_message_without_args():
    exc = ExportValidationError()
    assert exc.user_message == "Ошибка валидации параметров выгрузки"


def test_app_error_subclass_default_user_message():
    exc = AppError()
    assert exc.user_message == "Произошла ошибка"
