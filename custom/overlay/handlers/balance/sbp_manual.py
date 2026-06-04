"""Обработчик ручного пополнения через СБП."""

import html
import re

import structlog
from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.services.custom_payment_settings_service import get_public_provider_config, is_provider_configured
from app.services.payment_method_config_service import get_config_by_method_id
from app.services.sbp_manual_service import create_sbp_ticket
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


def _telegram_instruction(value: str) -> str:
    """Convert sanitized cabinet rich text to Telegram-compatible HTML."""
    text = re.sub(r'<br\s*/?>|</p>|</li>', '\n', value or '', flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>', '• ', text, flags=re.IGNORECASE)
    text = re.sub(r'</?(?:p|ul|ol)[^>]*>', '', text, flags=re.IGNORECASE)
    return text.strip()


def _bank_keyboard(banks: list[dict]) -> types.InlineKeyboardMarkup:
    rows = [
        [
            types.InlineKeyboardButton(
                text=f"{'⭐️ ' if bank['recommended'] else ''}{bank['label']}",
                callback_data=f"sbp_bank_{bank['id']}",
            )
        ]
        for bank in banks
    ]
    rows.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data='balance_topup')])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@error_handler
async def start_sbp_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    async with AsyncSessionLocal() as db:
        if not await is_provider_configured(db, 'manual'):
            await callback.answer('❌ Пополнение по СБП временно недоступно', show_alert=True)
            return
        provider_config = await get_public_provider_config(db, 'manual')
        method_config = await get_config_by_method_id(db, 'manual')

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

    banks = provider_config['banks']
    instruction = _telegram_instruction(provider_config['instruction_html'])
    phone = html.escape(provider_config['phone'])
    prompt = f'{instruction}\n\n' if instruction else ''
    prompt += f'📱 <b>Номер:</b> <code>{phone}</code>\n\n<b>Выберите банк для перевода:</b>'
    min_amount = method_config.min_amount_kopeks if method_config and method_config.min_amount_kopeks else 10_000
    max_amount = method_config.max_amount_kopeks if method_config and method_config.max_amount_kopeks else 5_000_000
    await callback.message.edit_text(prompt, reply_markup=_bank_keyboard(banks), parse_mode='HTML')
    await state.update_data(
        sbp_prompt_message_id=callback.message.message_id,
        sbp_banks={bank['id']: bank['label'] for bank in banks},
        sbp_min_amount_kopeks=min_amount,
        sbp_max_amount_kopeks=max_amount,
    )
    await state.set_state(BalanceStates.waiting_for_sbp_bank)
    await callback.answer()


@error_handler
async def handle_sbp_bank_selection(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    bank_key = callback.data.replace('sbp_bank_', '')
    state_data = await state.get_data()
    bank_name = state_data.get('sbp_banks', {}).get(bank_key)
    if not bank_name:
        await callback.answer('Банк больше недоступен', show_alert=True)
        return

    await state.update_data(sbp_bank_key=bank_key, sbp_bank_name=bank_name)
    await state.set_state(BalanceStates.waiting_for_sbp_amount)
    await callback.message.edit_text(
        '💳 <b>Введите сумму для пополнения (в рублях):</b>\n\n'
        f'Выбранный банк: <b>{html.escape(bank_name)}</b>',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='⬅️ Назад', callback_data='topup_manual')]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def handle_sbp_amount(message: types.Message, db_user: User, state: FSMContext):
    text = (message.text or '').strip().replace(',', '.').replace(' ', '')
    state_data = await state.get_data()
    min_kopeks = int(state_data.get('sbp_min_amount_kopeks', 10_000))
    max_kopeks = int(state_data.get('sbp_max_amount_kopeks', 5_000_000))

    try:
        amount_rubles = float(text)
        amount_kopeks = int(round(amount_rubles * 100))
        if amount_kopeks < min_kopeks:
            await message.answer(f'⚠️ Минимальная сумма пополнения — {min_kopeks / 100:.0f} ₽')
            return
        if amount_kopeks > max_kopeks:
            await message.answer(f'⚠️ Максимальная сумма пополнения — {max_kopeks / 100:.0f} ₽')
            return
    except ValueError:
        await message.answer('⚠️ Введите сумму числом, например: 500')
        return

    bank_name = state_data.get('sbp_bank_name', 'Не указан')
    prompt_message_id = state_data.get('sbp_prompt_message_id')
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    await state.update_data(sbp_amount_kopeks=amount_kopeks)
    confirm_text = (
        '💳 <b>Пополнение баланса через СБП</b>\n\n'
        f'🏦 <b>Банк:</b> {html.escape(bank_name)}\n'
        f'💰 <b>Сумма:</b> {amount_rubles:.0f} ₽\n\n'
        'Нажмите кнопку ниже для подтверждения оплаты:'
    )
    keyboard = types.InlineKeyboardMarkup(
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
                reply_markup=keyboard,
                parse_mode='HTML',
            )
            return
        except TelegramBadRequest:
            pass
    sent = await message.answer(confirm_text, reply_markup=keyboard, parse_mode='HTML')
    await state.update_data(sbp_prompt_message_id=sent.message_id)


@error_handler
async def handle_sbp_confirm(callback: types.CallbackQuery, db_user: User, state: FSMContext, bot: Bot):
    state_data = await state.get_data()
    try:
        async with AsyncSessionLocal() as db:
            result = await create_sbp_ticket(
                db=db,
                user=db_user,
                amount_kopeks=int(state_data.get('sbp_amount_kopeks') or 0),
                bank_key=str(state_data.get('sbp_bank_key') or ''),
                notify_admins=True,
            )
    except Exception as error:
        logger.error('Failed to create SBP ticket', error=str(error), user_id=db_user.id)
        await callback.answer('⚠️ Ошибка при создании заявки. Попробуйте через техподдержку.', show_alert=True)
        return

    await callback.message.edit_text(
        '✅ <b>Платёж отправлен на обработку</b>\n\n'
        f'💰 <b>Сумма:</b> {result["amount_rubles"]:.0f} ₽\n'
        f'🏦 <b>Банк:</b> {html.escape(result["bank"])}\n\n'
        'Администратор проверит перевод и пополнит ваш баланс.',
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

    text = (
        f'✅ <b>ПЛАТЁЖ ПОДТВЕРЖДЁН #{ticket_id}</b>\n\n'
        f'Банк: {html.escape(result["bank"])}\n'
        f'Сумма: <b>{result["amount_rubles"]:.0f} ₽</b>\n'
        f'Подтвердил: @{callback.from_user.username or callback.from_user.id}\n'
        f'Тикет закрыт: {"да" if result.get("ticket_closed") else "нет"}'
    )
    try:
        await callback.message.edit_text(text, parse_mode='HTML')
    except Exception:
        pass
    logger.info('SBP payment approved', ticket_id=ticket_id, amount_rubles=result['amount_rubles'])
    await callback.answer('✅ Платёж подтверждён, баланс пополнен, тикет закрыт', show_alert=True)
