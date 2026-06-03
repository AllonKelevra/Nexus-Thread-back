"""Обработчик ручного пополнения через СБП."""

import html
from datetime import UTC, datetime
from app.utils.timezone import format_local_datetime

import structlog
from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.utils.cache import cache
from app.services.admin_notification_service import AdminNotificationService
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

from app.config import settings
from app.database.models import User
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

INSTRUCTION_TEXT = (
    "💳 <b>Перевод по номеру телефона через СБП</b>\n\n"
    "Для пополнения баланса направьте перевод по номеру телефона через СБП.\n\n"
    "📱 <b>Номер:</b> <code>REMOVED_PAYMENT_PHONE</code>\n"
    "🏦 <b>Банки:</b> Яндекс <i>(рекомендуется)</i>\n"
    f"                         Альфа Банк\n"
    f"                         Т-Банк\n"
    f"                         Сбербанк\n\n"
    "После завершения перевода выберите банк, на который сделали перевод, "
    "введите сумму и нажмите кнопку <b>«Перевёл»</b>.\n\n"
    "Администратор проверит ваш перевод и пополнит ваш баланс.\n\n"
    "⏱ <b>Время пополнения до 2-х часов.</b>\n"
    "Обработка платежа в нерабочие часы может занять больше времени."
)

BANK_DISPLAY = {
    'yandex': 'Яндекс (рекомендуется)',
    'alfa': 'Альфа Банк',
    'tbank': 'Т-Банк',
    'sber': 'Сбербанк',
}


def _bank_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='⭐️ Яндекс', callback_data='sbp_bank_yandex')],
            [types.InlineKeyboardButton(text='Альфа Банк', callback_data='sbp_bank_alfa')],
            [types.InlineKeyboardButton(text='Т-Банк', callback_data='sbp_bank_tbank')],
            [types.InlineKeyboardButton(text='Сбербанк', callback_data='sbp_bank_sber')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='balance_topup')],
        ]
    )


@error_handler
async def start_sbp_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    if not settings.is_manual_enabled():
        await callback.answer('❌ Пополнение по СБП временно недоступно', show_alert=True)
        return

    if getattr(db_user, 'restriction_topup', False):
        reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
        await callback.message.edit_text(
            f'🚫 <b>Пополнение ограничено</b>\n\n{reason}',
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='menu_balance')]]
            ),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        INSTRUCTION_TEXT + '\n\n<b>Выберите банк на который сделаете перевод:</b>',
        reply_markup=_bank_keyboard(),
        parse_mode='HTML',
    )
    await state.update_data(sbp_prompt_message_id=callback.message.message_id)
    await state.set_state(BalanceStates.waiting_for_sbp_bank)
    await callback.answer()


@error_handler
async def handle_sbp_bank_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    bank_key = callback.data.replace('sbp_bank_', '')
    bank_name = BANK_DISPLAY.get(bank_key, bank_key)

    await state.update_data(sbp_bank_key=bank_key, sbp_bank_name=bank_name)
    await state.set_state(BalanceStates.waiting_for_sbp_amount)

    _bank_display = bank_name.replace(' (рекомендуется)', '')
    await callback.message.edit_text(
        f'💳 <b>Введите сумму для пополнения (в рублях):</b>\n\n'
        f'Выбранный банк: <b>{html.escape(_bank_display)}</b>',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='topup_manual')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def handle_sbp_amount(message: types.Message, db_user: User, state: FSMContext):
    text = (message.text or '').strip().replace(',', '.').replace(' ', '')

    try:
        amount_rubles = float(text)
        if amount_rubles < 100:
            await message.answer('⚠️ Минимальная сумма пополнения — 100 ₽')
            return
        if amount_rubles > 50000:
            await message.answer('⚠️ Максимальная сумма пополнения — 50 000 ₽')
            return
    except ValueError:
        await message.answer('⚠️ Введите сумму числом, например: 500')
        return

    state_data = await state.get_data()
    bank_name = state_data.get('sbp_bank_name', 'Не указан')
    prompt_message_id = state_data.get('sbp_prompt_message_id')

    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    await state.update_data(sbp_amount_rubles=amount_rubles)

    _bank_display_confirm = bank_name.replace(' (рекомендуется)', '')
    confirm_text = (
        f'💳 <b>Пополнение баланса через — Перевод по СБП</b>\n\n'
        f'🏦 <b>Банк:</b> {html.escape(_bank_display_confirm)}\n'
        f'💰 <b>Сумма:</b> {amount_rubles:.0f} ₽\n\n'
        f'Нажмите кнопку ниже для подтверждения оплаты:'
    )

    confirm_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='⭐️ Перевёл', callback_data='sbp_confirm_pay')],
            [types.InlineKeyboardButton(text='⬅️ Назад', callback_data='topup_manual')],
        ]
    )

    if prompt_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_message_id,
                text=confirm_text,
                reply_markup=confirm_keyboard,
                parse_mode='HTML',
            )
            return
        except TelegramBadRequest:
            pass

    sent = await message.answer(confirm_text, reply_markup=confirm_keyboard, parse_mode='HTML')
    await state.update_data(sbp_prompt_message_id=sent.message_id)


@error_handler
async def handle_sbp_confirm(callback: types.CallbackQuery, db_user: User, state: FSMContext, bot: Bot):
    state_data = await state.get_data()
    bank_key = state_data.get('sbp_bank_key', '')
    bank_name = state_data.get('sbp_bank_name', 'Не указан')
    amount_rubles = state_data.get('sbp_amount_rubles', 0)

    dt_moscow = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M')
    user_full_name = ' '.join(filter(None, [
        getattr(db_user, 'first_name', None),
        getattr(db_user, 'last_name', None),
    ])) or 'Неизвестно'
    username_clean = (getattr(db_user, 'username', None) or '').replace('@', '')
    at_username = f'@{username_clean}' if username_clean else ''
    bank_display = bank_name.replace(' (рекомендуется)', '') if '(рекомендуется)' in bank_name else bank_name

    ticket_title = f'💰 💳 Пополнение баланса по СБП от {user_full_name} {at_username}'.strip()
    NL = '\n'
    ticket_body = (
        '💰 💳 Пополнение баланса переводом через СБП' + NL + NL
        + '<b>Данные перевода:</b>' + NL + NL
        + f'Банк: <b>{bank_display}</b>' + NL
        + f'Сумма платежа: <b>{amount_rubles:.0f} ₽</b>' + NL + NL
        + f'Время пополнения: {dt_moscow}'
    )

    try:
        from app.database.database import AsyncSessionLocal
        from app.database.crud.ticket import TicketCRUD
        from app.handlers.tickets import notify_admins_about_new_ticket

        async with AsyncSessionLocal() as db:
            ticket = await TicketCRUD.create_ticket(
                db=db,
                user_id=db_user.id,
                title=ticket_title,
                message_text=ticket_body,
                priority='high',
            )
            # Redis meta for admin confirm button
            _meta = {
                'user_id': db_user.id,
                'telegram_id': int(getattr(db_user, 'telegram_id', 0) or 0),
                'amount_kopeks': int(amount_rubles * 100),
                'bank': bank_display,
                'confirmed': False,
            }
            await cache.set(f'sbp_meta:{ticket.id}', _meta, expire=2592000)

            # Admin notification with confirm button
            _sn = html.escape(f'{user_full_name} {at_username}'.strip())
            _ntxt = (
                f'🎫 <b>НОВЫЙ ТИКЕТ СБП #{ticket.id}</b>\n\n'
                f'👤 <b>{_sn}</b>\n'
                f'📱 TG: <code>{_meta["telegram_id"]}</code>\n\n'
                f'<b>Данные перевода:</b>\n\n'
                f'Банк: <b>{html.escape(bank_display)}</b>\n'
                f'Сумма: <b>{amount_rubles:.0f} ₽</b>\n\n'
                f'⏰ {dt_moscow}'
            )
            _kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text='💰 Подтвердить платёж',
                    callback_data=f'sbp_approve:{ticket.id}',
                )
            ]])
            await AdminNotificationService(bot).send_ticket_event_notification(_ntxt, _kb)

        logger.info(
            'SBP manual payment ticket created',
            user_id=db_user.id,
            bank=bank_key,
            amount_rubles=amount_rubles,
            ticket_id=ticket.id,
        )
    except Exception as e:
        logger.error('Failed to create SBP ticket', error=str(e), user_id=db_user.id)
        await callback.answer('⚠️ Ошибка при создании заявки. Попробуйте через техподдержку.', show_alert=True)
        return

    await callback.message.edit_text(
        '✅ <b>Платеж отправлен на обработку</b>\n\n'
        f'💰 <b>Сумма:</b> {amount_rubles:.0f} ₽\n'
        f'🏦 <b>Банк:</b> {html.escape(bank_name)}\n\n'
        'Администратор проверит перевод и пополнит ваш баланс.\n\n'
        '⏱ <b>Время пополнения до 2-х часов.</b>\n'
        'Обработка платежа в нерабочие часы может занять больше времени.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='🏠 В начало', callback_data='back_to_menu')]]
        ),
        parse_mode='HTML',
    )
    await state.clear()
    await callback.answer()


@error_handler
async def handle_sbp_approve(callback: types.CallbackQuery, bot: Bot):
    """Admin: approve SBP payment — credit user balance."""
    try:
        ticket_id = int(callback.data.split(':')[1])
    except (IndexError, ValueError):
        await callback.answer('⚠️ Неверный формат', show_alert=True)
        return

    from app.database.database import AsyncSessionLocal
    from app.services.sbp_manual_service import (
        SbpBalanceCreditFailed,
        SbpPaymentAlreadyConfirmed,
        SbpPaymentConfirmInProgress,
        SbpPaymentNotFound,
        confirm_sbp_payment,
    )

    async with AsyncSessionLocal() as db:
        try:
            result = await confirm_sbp_payment(
                db=db,
                ticket_id=ticket_id,
                actor_label=f'bot:{callback.from_user.username or callback.from_user.id}',
                source='бот',
            )
        except SbpPaymentAlreadyConfirmed:
            await callback.answer('✅ Уже подтверждён ранее', show_alert=True)
            return
        except SbpPaymentConfirmInProgress:
            await callback.answer('⏳ Платёж уже подтверждается другим администратором', show_alert=True)
            return
        except SbpPaymentNotFound:
            await callback.answer('⚠️ Данные устарели или платёж уже подтверждён', show_alert=True)
            return
        except SbpBalanceCreditFailed:
            await callback.answer('❌ Ошибка начисления. Проверьте логи.', show_alert=True)
            return

    _ct = (
        f'✅ <b>ПЛАТЁЖ ПОДТВЕРЖДЁН #{ticket_id}</b>\n\n'
        f'Банк: {html.escape(result["bank"])}\n'
        f'Сумма: <b>{result["amount_rubles"]:.0f} ₽</b>\n'
        f'Подтвердил: @{callback.from_user.username or callback.from_user.id}\n'
        f'Тикет закрыт: {"да" if result.get("ticket_closed") else "нет"}'
    )
    try:
        await callback.message.edit_text(_ct, parse_mode='HTML')
    except Exception:
        pass

    logger.info('SBP payment approved', ticket_id=ticket_id, amount_rubles=result['amount_rubles'])
    await callback.answer('✅ Платёж подтверждён, баланс пополнен, тикет закрыт', show_alert=True)
