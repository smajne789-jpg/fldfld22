from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Профиль", callback_data="menu:profile")
    builder.button(text="Играть", callback_data="menu:games")
    builder.button(text="Резерв", callback_data="menu:reserve")
    if is_admin:
        builder.button(text="Админ-панель", callback_data="admin:panel")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu:main")]]
    )


def profile_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Пополнить", callback_data="deposit:start")
    builder.button(text="Вывести", callback_data="withdraw:start")
    builder.button(text="Назад", callback_data="menu:main")
    builder.adjust(2, 1)
    return builder.as_markup()


def deposit_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for amount in ("0.3", "1", "5", "10"):
        builder.button(text=f"{amount} $", callback_data=f"deposit:amount:{amount}")
    builder.button(text="Назад", callback_data="menu:profile")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def invoice_menu(invoice_id: int, pay_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=pay_url)],
            [InlineKeyboardButton(text="Проверить оплату", callback_data=f"deposit:check:{invoice_id}")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:profile")],
        ]
    )


def withdraw_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for amount in ("1.7", "5", "10", "25"):
        builder.button(text=f"{amount} $", callback_data=f"withdraw:amount:{amount}")
    builder.button(text="Назад", callback_data="menu:profile")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def games_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Куб 18+", callback_data="game:select:cube18")
    builder.button(text="Куб нечет", callback_data="game:select:odd")
    builder.button(text="Куб чет", callback_data="game:select:even")
    builder.button(text="Куб 7", callback_data="game:select:seven")
    builder.button(text="Куб 7+", callback_data="game:select:seven_plus")
    builder.button(text="Назад", callback_data="menu:main")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def bet_menu(game_code: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for amount in ("0.1", "0.5", "1", "5"):
        builder.button(text=f"{amount} $", callback_data=f"game:bet:{game_code}:{amount}")
    builder.button(text="Назад", callback_data="menu:games")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def after_game_menu(game_code: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Сыграть еще", callback_data=f"game:select:{game_code}")
    builder.button(text="Игры", callback_data="menu:games")
    builder.button(text="Профиль", callback_data="menu:profile")
    builder.button(text="Меню", callback_data="menu:main")
    builder.adjust(1, 2, 1)
    return builder.as_markup()


def admin_menu(auto_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Изменить резерв", callback_data="admin:reserve")
    builder.button(text="Создать чек", callback_data="admin:check")
    builder.button(
        text="Отключить автовывод" if auto_enabled else "Включить автовывод",
        callback_data="admin:auto",
    )
    builder.button(text="Статистика", callback_data="admin:stats")
    builder.button(text="Назад", callback_data="menu:main")
    builder.adjust(1, 1, 1, 1, 1)
    return builder.as_markup()


def admin_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="admin:panel")]]
    )


def admin_withdrawal_menu(withdrawal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Одобрить",
                    callback_data=f"admin:wd:approve:{withdrawal_id}",
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"admin:wd:decline:{withdrawal_id}",
                ),
            ]
        ]
    )
