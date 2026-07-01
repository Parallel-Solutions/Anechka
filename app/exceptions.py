"""Пользовательские исключения приложения."""


class AppError(Exception):
    """Базовое исключение приложения."""

    user_message: str = "Произошла ошибка"

    def __init__(self, message: str | None = None, user_message: str | None = None):
        super().__init__(message or user_message or self.user_message)
        if user_message:
            self.user_message = user_message
        elif message:
            self.user_message = message


class BitrixAPIError(AppError):
    user_message = "Ошибка при обращении к Bitrix24"


class BitrixAuthenticationError(AppError):
    user_message = "Не удалось подключиться к Bitrix24. Проверьте вебхук"


class BitrixRateLimitError(AppError):
    user_message = "Bitrix24 временно ограничил количество запросов. Попробуйте позже"


class ExportCancelledError(AppError):
    user_message = "Выгрузка была отменена пользователем"


class ExportValidationError(AppError):
    user_message = "Ошибка валидации параметров выгрузки"
