from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram filters import Command, CommandStart
from aiogram filters.command import CommandObject
from aiogram fsm.context import FSMContext
from aiogram fsm.storage.memory import MemoryStorage
from aiogram types import BotCommand, CallbackQuery, Message, TelegramObject

from config import Config, load_config
from crypto_pay import CryptoPayClient, CryptoPayError
from db import Database, WithdrawalHold, dec, money_text
from keyboards import (
    admin_back_menu,
    admin_menu,
    admin_withdrawal_menu,
    after_game_menu,
    back_to_main,
    bet_menu,
    deposit_menu,
    games_menu,
    invoice_menu,
    main_menu,
    profile_menu,
    withdraw_menu,
)
from .states import AdminCheckFlow, AdminReserveFlow, BetFlow, DepositFlow, WithdrawFlow


logger = logging.getLogger(__name__)
router = Router()
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Game:
    title: str
    description: str
    dice_count: int
    multiplier: Decimal
    is_win: Callable[[list[int]], bool]
    score_text: Callable[[list[int]], str]


GAMES: dict[str, Game] = {
    "cube18": Game(
        title="Куб 18+",
        description="Бросаются два настоящих кубика. Второй кубик умножает первый, и произведение должно быть больше 18.",
        dice_count=2,
        multiplier=Decimal("5"),
        is_win=lambda rolls: rolls[0] * rolls[1] > 18,
        score_text=lambda rolls: f"Произведение: {rolls[0]} x {rolls[1]} = {rolls[0] * rolls[1]}",
    ),
    "odd": Game(
        title="Куб нечет",
        description="Бросается один настоящий кубик. Нужно поймать 1, 3 или 5.",
        dice_count=1,
        multiplier=Decimal("2"),
        is_win=lambda rolls: rolls[0] in {1, 3, 5},
        score_text=lambda rolls: f"Выпало: {rolls[0]}",
    ),
    "even": Game(
        title="Куб чет",
        description="Бросается один настоящий кубик. Нужно поймать 2, 4 или 6.",
        dice_count=1,
        multiplier=Decimal("2"),
        is_win=lambda rolls: rolls[0] in {2, 4, 6},
        score_text=lambda rolls: f"Выпало: {rolls[0]}",
    ),
    "seven": Game(
        title="Куб 7",
        description="Бросаются два настоящих кубика. Сумма должна быть ровно 7.",
        dice_count=2,
        multiplier=Decimal("4"),
        is_win=lambda rolls: sum(rolls) == 7,
        score_text=lambda rolls: f"Сумма: {rolls[0]} + {rolls[1]} = {sum(rolls)}",
    ),
    "seven_plus": Game(
        title="Куб 7+",
        description="Бросаются два настоящих кубика. Сумма должна быть больше 7.",
        dice_count=2,
        multiplier=Decimal("4"),
        is_win=lambda rolls: sum(rolls) > 7,
        score_text=lambda rolls: f"Сумма: {rolls[0]} + {rolls[1]} = {sum(rolls)}",
    ),
}


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        db: Database | None = data.get("db")
        if user is not None and db is not None and not user.is_bot:
            await db.upsert_user(user.id, user.username, user.first_name)
        return await handler(event, data)


def is_admin(user_id: int, config: Config) -> bool:
    return user_id in config.admin_ids


def parse_amount(raw: str) -> Decimal | None:
    cleaned = raw.strip().replace("$", "").replace(",", ".")
    try:
        amount = Decimal(cleaned).quantize(CENT, rounding=ROUND_DOWN)
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0:
        return None
    return amount


def parse_int(raw: str) -> int | None:
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def parse_zero_or_amount(raw: str) -> Decimal | None:
    cleaned = raw.strip().replace("$", "").replace(",", ".")
    try:
        value = Decimal(cleaned).quantize(CENT, rounding=ROUND_DOWN)
    except (InvalidOperation, ValueError):
        return None
    if value < 0:
        return None
    return value


async def edit_or_send(
    callback: CallbackQuery,
    text: str,
    reply_markup: Any = None,
) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await callback.message.answer(text, reply_markup=reply_markup)
    await callback.answer()


async def render_main(user_id: int, db: Database, config: Config) -> str:
    user = await db.get_user(user_id)
    reserve = await db.get_reserve()
    balance = dec(user["balance"]) if user else Decimal("0")
    return (
        "<b>Главное меню</b>\n\n"
        f"Баланс: <b>{money_text(balance)} $</b>\n"
        f"Резерв проекта: <b>{money_text(reserve)} $</b>\n\n"
        "Все разделы открываются кнопками ниже."
    )


async def render_profile(user_id: int, db: Database) -> str:
    user = await db.get_user(user_id)
    if user is None:
        return "Профиль не найден. Нажмите /start."

    games_played = int(user["games_played"])
    games_won = int(user["games_won"])
    win_rate = Decimal("0")
    if games_played:
        win_rate = (Decimal(games_won) / Decimal(games_played) * Decimal("100")).quantize(CENT)

    username = f"@{html.escape(user['username'])}" if user.get("username") else "без username"
    return (
        "<b>Профиль</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"Username: {username}\n\n"
        f"Баланс: <b>{money_text(user['balance'])} $</b>\n"
        f"Пополнено: <b>{money_text(user['total_deposits'])} $</b>\n"
        f"Выведено: <b>{money_text(user['total_withdrawals'])} $</b>\n\n"
        f"Игр сыграно: <b>{games_played}</b>\n"
        f"Побед: <b>{games_won}</b>\n"
        f"Win-rate: <b>{money_text(win_rate)}%</b>"
    )


async def render_admin_panel(db: Database) -> tuple[str, Any]:
    reserve = await db.get_reserve()
    auto_enabled = await db.get_auto_withdrawals()
    text = (
        "<b>Админ-панель</b>\n\n"
        f"Доступный резерв: <b>{money_text(reserve)} $</b>\n"
        f"Автовыводы: <b>{'включены' if auto_enabled else 'отключены'}</b>"
    )
    return text, admin_menu(auto_enabled)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db: Database,
    config: Config,
) -> None:
    await state.clear()
    args = (command.args or "").strip()
    if args.startswith("check_"):
        code = args.removeprefix("check_").strip()
        ok, status_text, amount = await db.claim_promo_check(code=code, user_id=message.from_user.id)
        if ok and amount is not None:
            text = (
                "<b>Чек активирован</b>\n\n"
                f"На баланс начислено: <b>{money_text(amount)} $</b>\n\n"
                "Можно вернуться в меню."
            )
        else:
            text = f"<b>Чек не активирован</b>\n\n{html.escape(status_text)}"
        await message.answer(text, reply_markup=main_menu(is_admin(message.from_user.id, config)))
        return

    await message.answer(
        await render_main(message.from_user.id, db, config),
        reply_markup=main_menu(is_admin(message.from_user.id, config)),
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, db: Database, config: Config) -> None:
    if not is_admin(message.from_user.id, config):
        await message.answer("Нет доступа.")
        return
    text, markup = await render_admin_panel(db)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "menu:main")
async def menu_main(callback: CallbackQuery, state: FSMContext, db: Database, config: Config) -> None:
    await state.clear()
    await edit_or_send(
        callback,
        await render_main(callback.from_user.id, db, config),
        main_menu(is_admin(callback.from_user.id, config)),
    )


@router.callback_query(F.data == "menu:profile")
async def menu_profile(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await edit_or_send(callback, await render_profile(callback.from_user.id, db), profile_menu())


@router.callback_query(F.data == "menu:games")
async def menu_games(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    await state.clear()
    text = (
        "<b>Игры</b>\n\n"
        f"Минимальная ставка во всех играх: <b>{money_text(config.min_bet_usd)} $</b>\n"
        "Выберите режим."
    )
    await edit_or_send(callback, text, games_menu())


@router.callback_query(F.data == "menu:reserve")
async def menu_reserve(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    reserve = await db.get_reserve()
    await edit_or_send(
        callback,
        "<b>Резерв</b>\n\n"
        f"Сейчас доступно: <b>{money_text(reserve)} $</b>\n\n"
        "Резерв обновляет администрация проекта.",
        back_to_main(),
    )


@router.callback_query(F.data == "deposit:start")
async def deposit_start(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    await state.set_state(DepositFlow.amount)
    await edit_or_send(
        callback,
        "<b>Пополнение</b>\n\n"
        f"Минимальная сумма: <b>{money_text(config.min_deposit_usd)} $</b>\n"
        "Введите сумму сообщением или выберите быстрый вариант.",
        deposit_menu(),
    )


@router.callback_query(F.data.startswith("deposit:amount:"))
async def deposit_quick_amount(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    crypto: CryptoPayClient,
    config: Config,
) -> None:
    await state.clear()
    amount = parse_amount(callback.data.rsplit(":", 1)[-1])
    if amount is None:
        await callback.answer("Некорректная сумма.", show_alert=True)
        return
    await create_invoice(callback, amount, db, crypto, config)


@router.message(DepositFlow.amount)
async def deposit_amount_message(
    message: Message,
    state: FSMContext,
    db: Database,
    crypto: CryptoPayClient,
    config: Config,
) -> None:
    amount = parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите сумму числом, например 0.3.", reply_markup=deposit_menu())
        return
    await state.clear()
    await create_invoice(message, amount, db, crypto, config)


async def create_invoice(
    target: Message | CallbackQuery,
    amount: Decimal,
    db: Database,
    crypto: CryptoPayClient,
    config: Config,
) -> None:
    user_id = target.from_user.id
    if amount < config.min_deposit_usd:
        text = f"Минимальное пополнение: {money_text(config.min_deposit_usd)} $."
        if isinstance(target, CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text, reply_markup=deposit_menu())
        return

    try:
        invoice = await crypto.create_invoice_usd(
            amount=amount,
            user_id=user_id,
            accepted_assets=config.accepted_assets,
        )
    except CryptoPayError as exc:
        logger.exception("Failed to create invoice")
        text = f"Не удалось создать счет CryptoBot.\n\n<code>{html.escape(str(exc))}</code>"
        if isinstance(target, CallbackQuery):
            await edit_or_send(target, text, profile_menu())
        else:
            await target.answer(text, reply_markup=profile_menu())
        return

    invoice_id = int(invoice["invoice_id"])
    pay_url = (
        invoice.get("pay_url")
        or invoice.get("bot_invoice_url")
        or invoice.get("mini_app_invoice_url")
        or invoice.get("web_app_invoice_url")
    )
    if not pay_url:
        text = "CryptoBot создал счет, но не вернул ссылку оплаты."
        if isinstance(target, CallbackQuery):
            await edit_or_send(target, text, profile_menu())
        else:
            await target.answer(text, reply_markup=profile_menu())
        return

    await db.create_invoice(invoice_id=invoice_id, user_id=user_id, amount=amount, pay_url=pay_url)
    text = (
        "<b>Счет создан</b>\n\n"
        f"Сумма: <b>{money_text(amount)} $</b>\n"
        f"Invoice ID: <code>{invoice_id}</code>\n\n"
        "После оплаты нажмите проверку. Бот также проверяет счета автоматически."
    )
    if isinstance(target, CallbackQuery):
        await edit_or_send(target, text, invoice_menu(invoice_id, pay_url))
    else:
        await target.answer(text, reply_markup=invoice_menu(invoice_id, pay_url))


@router.callback_query(F.data.startswith("deposit:check:"))
async def deposit_check(
    callback: CallbackQuery,
    db: Database,
    crypto: CryptoPayClient,
    bot: Bot,
) -> None:
    invoice_id = int(callback.data.rsplit(":", 1)[-1])
    paid, status = await sync_invoice(invoice_id, db, crypto, bot=None)
    if paid:
        await edit_or_send(
            callback,
            "<b>Оплата найдена</b>\n\nБаланс пополнен. Откройте профиль, чтобы посмотреть сумму.",
            profile_menu(),
        )
        return
    await callback.answer(f"Пока не оплачено. Статус: {status}", show_alert=True)


@router.callback_query(F.data == "withdraw:start")
async def withdraw_start(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    await state.set_state(WithdrawFlow.amount)
    await edit_or_send(
        callback,
        "<b>Вывод</b>\n\n"
        f"Минимальная сумма: <b>{money_text(config.min_withdraw_usd)} $</b>\n"
        f"Для вывода нужно иметь депозитов от <b>{money_text(config.min_required_deposits_usd)} $</b>.\n\n"
        "Введите сумму сообщением или выберите быстрый вариант.",
        withdraw_menu(),
    )


@router.callback_query(F.data.startswith("withdraw:amount:"))
async def withdraw_quick_amount(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    crypto: CryptoPayClient,
    config: Config,
    bot: Bot,
) -> None:
    await state.clear()
    amount = parse_amount(callback.data.rsplit(":", 1)[-1])
    if amount is None:
        await callback.answer("Некорректная сумма.", show_alert=True)
        return
    await request_withdrawal(callback, amount, db, crypto, config, bot)


@router.message(WithdrawFlow.amount)
async def withdraw_amount_message(
    message: Message,
    state: FSMContext,
    db: Database,
    crypto: CryptoPayClient,
    config: Config,
    bot: Bot,
) -> None:
    amount = parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите сумму числом, например 1.7.", reply_markup=withdraw_menu())
        return
    await state.clear()
    await request_withdrawal(message, amount, db, crypto, config, bot)


async def request_withdrawal(
    target: Message | CallbackQuery,
    amount: Decimal,
    db: Database,
    crypto: CryptoPayClient,
    config: Config,
    bot: Bot,
) -> None:
    user = await db.get_user(target.from_user.id)
    if user is None:
        await answer_target(target, "Профиль не найден. Нажмите /start.", profile_menu())
        return
    if amount < config.min_withdraw_usd:
        await answer_target(
            target,
            f"Минимальный вывод: {money_text(config.min_withdraw_usd)} $.",
            withdraw_menu(),
        )
        return
    if dec(user["total_deposits"]) < config.min_required_deposits_usd:
        await answer_target(
            target,
            "Вывод откроется после депозитов на "
            f"{money_text(config.min_required_deposits_usd)} $.",
            profile_menu(),
        )
        return

    try:
        hold = await db.create_withdrawal_hold(
            user_id=target.from_user.id,
            username=target.from_user.username,
            amount=amount,
        )
    except ValueError as exc:
        text = "Недостаточно баланса или резерва для вывода." if "Insufficient" in str(exc) else str(exc)
        await answer_target(target, text, profile_menu())
        return

    auto_enabled = await db.get_auto_withdrawals()
    if auto_enabled:
        sent = await send_withdrawal_transfer(
            hold=hold,
            db=db,
            crypto=crypto,
            config=config,
            bot=bot,
            admin_id=None,
        )
        if sent:
            await answer_target(
                target,
                "<b>Вывод отправлен</b>\n\n"
                f"Сумма: <b>{money_text(amount)} $</b>\n"
                f"Актив: <b>{html.escape(config.payout_asset)}</b>",
                profile_menu(),
            )
            return

        await notify_admins_about_withdrawal(bot, config, hold, reason="Автовывод не прошел, нужна ручная проверка.")
        await answer_target(
            target,
            "Автовывод сейчас не прошел. Заявка отправлена администратору.",
            profile_menu(),
        )
        return

    await notify_admins_about_withdrawal(bot, config, hold, reason="Автовывод отключен.")
    await answer_target(
        target,
        "<b>Заявка создана</b>\n\n"
        "Администратор получил сообщение с вашим username и суммой.",
        profile_menu(),
    )


async def answer_target(target: Message | CallbackQuery, text: str, reply_markup: Any = None) -> None:
    if isinstance(target, CallbackQuery):
        await edit_or_send(target, text, reply_markup)
    else:
        await target.answer(text, reply_markup=reply_markup)


async def send_withdrawal_transfer(
    *,
    hold: WithdrawalHold,
    db: Database,
    crypto: CryptoPayClient,
    config: Config,
    bot: Bot,
    admin_id: int | None,
) -> bool:
    try:
        result = await crypto.transfer(
            user_id=hold.user_id,
            asset=config.payout_asset,
            amount=hold.amount,
            spend_id=hold.spend_id,
            comment=f"Withdrawal #{hold.id}",
        )
    except CryptoPayError as exc:
        logger.warning("Withdrawal transfer failed: %s", exc)
        await db.set_withdrawal_fail_reason(hold.id, str(exc))
        return False

    transfer_id = str(result.get("transfer_id") or result.get("id") or "")
    await db.mark_withdrawal_paid(
        withdrawal_id=hold.id,
        transfer_id=transfer_id,
        admin_id=admin_id,
    )
    if admin_id is not None:
        await safe_user_message(
            bot,
            hold.user_id,
            "<b>Вывод одобрен</b>\n\n"
            f"Сумма: <b>{money_text(hold.amount)} $</b>\n"
            f"Актив: <b>{html.escape(config.payout_asset)}</b>",
        )
    return True


async def notify_admins_about_withdrawal(
    bot: Bot,
    config: Config,
    hold: WithdrawalHold,
    reason: str,
) -> None:
    if not config.admin_ids:
        logger.warning("No ADMIN_IDS configured; withdrawal %s has no admin recipient", hold.id)
        return

    username = f"@{html.escape(hold.username)}" if hold.username else "без username"
    text = (
        "<b>Заявка на вывод</b>\n\n"
        f"ID заявки: <code>{hold.id}</code>\n"
        f"Пользователь: <code>{hold.user_id}</code> ({username})\n"
        f"Сумма: <b>{money_text(hold.amount)} $</b>\n"
        f"Причина ручной обработки: {html.escape(reason)}"
    )
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=admin_withdrawal_menu(hold.id))
        except TelegramForbiddenError:
            logger.warning("Admin %s cannot receive messages from bot", admin_id)


async def safe_user_message(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except TelegramForbiddenError:
        logger.info("User %s cannot receive messages from bot", user_id)


@router.callback_query(F.data.startswith("game:select:"))
async def game_select(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    game_code = callback.data.rsplit(":", 1)[-1]
    game = GAMES.get(game_code)
    if game is None:
        await callback.answer("Игра не найдена.", show_alert=True)
        return
    await state.set_state(BetFlow.amount)
    await state.update_data(game_code=game_code)
    text = (
        f"<b>{game.title}</b>\n\n"
        f"{html.escape(game.description)}\n\n"
        f"Множитель: <b>x{money_text(game.multiplier)}</b>\n"
        f"Минимальная ставка: <b>{money_text(config.min_bet_usd)} $</b>\n\n"
        "Введите ставку сообщением или выберите быстрый вариант."
    )
    await edit_or_send(callback, text, bet_menu(game_code))


@router.callback_query(F.data.startswith("game:bet:"))
async def game_quick_bet(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    config: Config,
    bot: Bot,
) -> None:
    _, _, game_code, raw_amount = callback.data.split(":", 3)
    amount = parse_amount(raw_amount)
    await state.clear()
    if amount is None:
        await callback.answer("Некорректная ставка.", show_alert=True)
        return
    await callback.answer()
    await play_game(callback.message, callback.from_user.id, game_code, amount, db, config, bot)


@router.message(BetFlow.amount)
async def game_bet_message(
    message: Message,
    state: FSMContext,
    db: Database,
    config: Config,
    bot: Bot,
) -> None:
    data = await state.get_data()
    game_code = data.get("game_code")
    amount = parse_amount(message.text or "")
    if not game_code or game_code not in GAMES:
        await state.clear()
        await message.answer("Игра не найдена.", reply_markup=games_menu())
        return
    if amount is None:
        await message.answer("Введите ставку числом, например 0.1.", reply_markup=bet_menu(game_code))
        return
    await state.clear()
    await play_game(message, message.from_user.id, game_code, amount, db, config, bot)


async def play_game(
    message: Message,
    user_id: int,
    game_code: str,
    amount: Decimal,
    db: Database,
    config: Config,
    bot: Bot,
) -> None:
    game = GAMES[game_code]
    if amount < config.min_bet_usd:
        await message.answer(
            f"Минимальная ставка: {money_text(config.min_bet_usd)} $.",
            reply_markup=bet_menu(game_code),
        )
        return

    user = await db.get_user(user_id)
    if user is None or dec(user["balance"]) < amount:
        await message.answer("Недостаточно баланса для ставки.", reply_markup=profile_menu())
        return

    potential_net_win = amount * (game.multiplier - Decimal("1"))
    reserve = await db.get_reserve()
    if reserve < potential_net_win:
        await message.answer(
            "Сейчас резерв не покрывает максимальный выигрыш по этой ставке.",
            reply_markup=bet_menu(game_code),
        )
        return

    await message.answer(
        f"<b>{game.title}</b>\n\n"
        f"Ставка: <b>{money_text(amount)} $</b>\n"
        "Бросаю кубик..."
    )

    rolls: list[int] = []
    for roll_index in range(game.dice_count):
        dice_message = await bot.send_dice(message.chat.id, emoji="🎲")
        rolls.append(dice_message.dice.value)
        if roll_index + 1 < game.dice_count:
            await asyncio.sleep(config.dice_animation_delay)
    await asyncio.sleep(config.dice_animation_delay)

    won = game.is_win(rolls)
    payout = amount * game.multiplier if won else Decimal("0")
    try:
        settlement = await db.settle_game(
            user_id=user_id,
            game_code=game_code,
            stake=amount,
            payout=payout,
            rolls=rolls,
        )
    except ValueError:
        await message.answer(
            "Ставка отменена: баланс или резерв изменились во время броска.",
            reply_markup=games_menu(),
        )
        return

    if won:
        result_text = (
            "<b>Победа</b>\n\n"
            f"Кубики: <b>{' / '.join(str(roll) for roll in rolls)}</b>\n"
            f"{game.score_text(rolls)}\n\n"
            f"Начислено: <b>{money_text(payout)} $</b>\n"
            f"Баланс: <b>{money_text(settlement['balance'])} $</b>"
        )
    else:
        result_text = (
            "<b>Проигрыш</b>\n\n"
            f"Кубики: <b>{' / '.join(str(roll) for roll in rolls)}</b>\n"
            f"{game.score_text(rolls)}\n\n"
            f"Списано: <b>{money_text(amount)} $</b>\n"
            f"Баланс: <b>{money_text(settlement['balance'])} $</b>"
        )
    await message.answer(result_text, reply_markup=after_game_menu(game_code))


@router.callback_query(F.data == "admin:panel")
async def admin_panel(callback: CallbackQuery, state: FSMContext, db: Database, config: Config) -> None:
    await state.clear()
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    text, markup = await render_admin_panel(db)
    await edit_or_send(callback, text, markup)


@router.callback_query(F.data == "admin:reserve")
async def admin_reserve(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminReserveFlow.amount)
    await edit_or_send(
        callback,
        "<b>Изменение резерва</b>\n\nВведите новую сумму резерва в долларах.",
        admin_back_menu(),
    )


@router.message(AdminReserveFlow.amount)
async def admin_reserve_amount(
    message: Message,
    state: FSMContext,
    db: Database,
    config: Config,
) -> None:
    if not is_admin(message.from_user.id, config):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    amount = parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите сумму числом.", reply_markup=admin_back_menu())
        return
    await db.set_reserve(amount)
    await state.clear()
    text, markup = await render_admin_panel(db)
    await message.answer(f"Резерв установлен: <b>{money_text(amount)} $</b>\n\n{text}", reply_markup=markup)


@router.callback_query(F.data == "admin:auto")
async def admin_auto_toggle(callback: CallbackQuery, db: Database, config: Config) -> None:
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    enabled = await db.get_auto_withdrawals()
    await db.set_auto_withdrawals(not enabled)
    text, markup = await render_admin_panel(db)
    await edit_or_send(callback, text, markup)


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery, db: Database, config: Config) -> None:
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    stats = await db.admin_stats()
    text = (
        "<b>Статистика</b>\n\n"
        f"Пользователей: <b>{stats['users']}</b>\n"
        f"Балансы пользователей: <b>{money_text(stats['balances'])} $</b>\n"
        f"Пополнено всего: <b>{money_text(stats['deposits'])} $</b>\n"
        f"Выведено всего: <b>{money_text(stats['withdrawals'])} $</b>\n"
        f"Заявок на вывод: <b>{stats['pending_withdrawals']}</b>\n"
        f"Резерв: <b>{money_text(stats['reserve'])} $</b>"
    )
    await edit_or_send(callback, text, admin_back_menu())


@router.callback_query(F.data == "admin:check")
async def admin_check_start(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminCheckFlow.amount)
    await edit_or_send(
        callback,
        "<b>Создание чека</b>\n\nВведите сумму, которая будет начисляться за одну активацию.",
        admin_back_menu(),
    )


@router.message(AdminCheckFlow.amount)
async def admin_check_amount(message: Message, state: FSMContext, config: Config) -> None:
    if not is_admin(message.from_user.id, config):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    amount = parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите сумму числом.", reply_markup=admin_back_menu())
        return
    await state.update_data(check_amount=str(amount))
    await state.set_state(AdminCheckFlow.activations)
    await message.answer("Сколько активаций будет у чека?", reply_markup=admin_back_menu())


@router.message(AdminCheckFlow.activations)
async def admin_check_activations(message: Message, state: FSMContext, config: Config) -> None:
    if not is_admin(message.from_user.id, config):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    activations = parse_int(message.text or "")
    if activations is None:
        await message.answer("Введите целое число больше 0.", reply_markup=admin_back_menu())
        return
    await state.update_data(check_activations=activations)
    await state.set_state(AdminCheckFlow.min_deposits)
    await message.answer(
        "Сколько депозитов нужно иметь для активации? Введите 0, если требование не нужно.",
        reply_markup=admin_back_menu(),
    )


@router.message(AdminCheckFlow.min_deposits)
async def admin_check_min_deposits(
    message: Message,
    state: FSMContext,
    db: Database,
    config: Config,
    bot_username: str,
) -> None:
    if not is_admin(message.from_user.id, config):
        await state.clear()
        await message.answer("Нет доступа.")
        return
    min_deposits = parse_zero_or_amount(message.text or "0")
    if min_deposits is None:
        await message.answer("Введите сумму числом или 0.", reply_markup=admin_back_menu())
        return

    data = await state.get_data()
    amount = Decimal(data["check_amount"])
    activations = int(data["check_activations"])
    code = await db.create_promo_check(
        amount=amount,
        max_activations=activations,
        min_deposits=min_deposits,
        created_by=message.from_user.id,
    )
    await state.clear()
    link = f"https://t.me/{bot_username}?start=check_{code}"
    await message.answer(
        "<b>Чек создан</b>\n\n"
        f"Сумма: <b>{money_text(amount)} $</b>\n"
        f"Активаций: <b>{activations}</b>\n"
        f"Требуемые депозиты: <b>{money_text(min_deposits)} $</b>\n\n"
        f"<a href=\"{html.escape(link)}\">Открыть чек</a>\n"
        f"<code>{html.escape(link)}</code>",
        reply_markup=admin_back_menu(),
    )


@router.callback_query(F.data.startswith("admin:wd:approve:"))
async def admin_withdrawal_approve(
    callback: CallbackQuery,
    db: Database,
    crypto: CryptoPayClient,
    config: Config,
    bot: Bot,
) -> None:
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    withdrawal_id = int(callback.data.rsplit(":", 1)[-1])
    row = await db.get_withdrawal(withdrawal_id)
    if row is None or row["status"] != "pending":
        await callback.answer("Заявка уже обработана или не найдена.", show_alert=True)
        return
    hold = WithdrawalHold(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        username=row["username"],
        amount=dec(row["amount"]),
        spend_id=row["spend_id"],
    )
    sent = await send_withdrawal_transfer(
        hold=hold,
        db=db,
        crypto=crypto,
        config=config,
        bot=bot,
        admin_id=callback.from_user.id,
    )
    if not sent:
        await callback.answer("CryptoBot не отправил перевод. Можно повторить позже.", show_alert=True)
        return
    await edit_or_send(
        callback,
        "<b>Заявка оплачена</b>\n\n"
        f"ID: <code>{withdrawal_id}</code>\n"
        f"Сумма: <b>{money_text(hold.amount)} $</b>",
    )


@router.callback_query(F.data.startswith("admin:wd:decline:"))
async def admin_withdrawal_decline(
    callback: CallbackQuery,
    db: Database,
    config: Config,
    bot: Bot,
) -> None:
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    withdrawal_id = int(callback.data.rsplit(":", 1)[-1])
    try:
        row = await db.decline_withdrawal(withdrawal_id=withdrawal_id, admin_id=callback.from_user.id)
    except ValueError:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    if row is None:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    await safe_user_message(
        bot,
        int(row["user_id"]),
        "<b>Вывод отклонен</b>\n\n"
        f"Сумма <b>{money_text(row['amount'])} $</b> возвращена на баланс.",
    )
    await edit_or_send(
        callback,
        "<b>Заявка отклонена</b>\n\n"
        f"ID: <code>{withdrawal_id}</code>\n"
        f"Сумма возвращена пользователю.",
    )


async def sync_invoice(
    invoice_id: int,
    db: Database,
    crypto: CryptoPayClient,
    bot: Bot | None,
) -> tuple[bool, str]:
    invoice = await db.get_invoice(invoice_id)
    if invoice is None:
        return False, "not_found"
    if invoice["status"] == "paid":
        return True, "paid"

    try:
        items = await crypto.get_invoices([invoice_id])
    except CryptoPayError as exc:
        logger.warning("Invoice sync failed: %s", exc)
        return False, "api_error"

    def same_invoice(item: dict[str, Any]) -> bool:
        try:
            return int(item.get("invoice_id")) == invoice_id
        except (TypeError, ValueError):
            return False

    remote = next((item for item in items if same_invoice(item)), None)
    if remote is None:
        return False, "not_found_in_cryptobot"

    status = str(remote.get("status", "unknown"))
    if status == "paid":
        await db.mark_invoice_paid(invoice_id)
        if bot is not None:
            await safe_user_message(
                bot,
                int(invoice["user_id"]),
                "<b>Пополнение зачислено</b>\n\n"
                f"Сумма: <b>{money_text(invoice['amount'])} $</b>",
            )
        return True, status
    return False, status


async def invoice_watcher(bot: Bot, db: Database, crypto: CryptoPayClient, config: Config) -> None:
    while True:
        try:
            pending = await db.list_pending_invoices(limit=100)
            for invoice in pending:
                await sync_invoice(int(invoice["invoice_id"]), db, crypto, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Invoice watcher failed")
        await asyncio.sleep(config.invoice_poll_seconds)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    db = Database(config.db_path, config.default_reserve_usd)
    await db.init()
    crypto = CryptoPayClient(config.crypto_pay_token, config.crypto_pay_base_url)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    me = await bot.get_me()
    bot_username = config.bot_username or me.username
    if not bot_username:
        raise RuntimeError("Bot username is required")

    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["config"] = config
    dp["crypto"] = crypto
    dp["bot_username"] = bot_username

    async def startup(startup_bot: Bot) -> None:
        await startup_bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="admin", description="Админ-панель"),
            ]
        )
        dp["invoice_task"] = asyncio.create_task(invoice_watcher(startup_bot, db, crypto, config))

    async def shutdown() -> None:
        task: asyncio.Task | None = dp.get("invoice_task")
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await crypto.close()
        await db.close()

    dp.startup.register(startup)
    dp.shutdown.register(shutdown)

    router.message.middleware(UserMiddleware())
    router.callback_query.middleware(UserMiddleware())
    dp.include_router(router)

    logger.info("Bot started as @%s", bot_username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
