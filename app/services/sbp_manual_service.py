"""Shared helpers for manual SBP balance top-ups."""

import html
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.crud.user import add_user_balance_by_id
from app.database.models import PaymentMethod, TransactionType, User
from app.services.custom_payment_settings_service import get_enabled_bank
from app.utils.cache import cache
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

SBP_META_TTL = 2_592_000
SBP_CONFIRM_LOCK_TTL = 60

class SbpPaymentError(Exception):
    """Base SBP payment error."""


class SbpPaymentNotFound(SbpPaymentError):
    """SBP payment metadata does not exist."""


class SbpPaymentAlreadyConfirmed(SbpPaymentError):
    """SBP payment was already confirmed."""


class SbpPaymentConfirmInProgress(SbpPaymentError):
    """Another actor is confirming this payment now."""


class SbpBalanceCreditFailed(SbpPaymentError):
    """Balance credit failed."""


async def normalize_bank(db: AsyncSession, bank: str | None) -> tuple[str, str]:
    """Return safe bank key and display name."""
    bank_key = (bank or '').strip().lower()
    bank_config = await get_enabled_bank(db, bank_key)
    return bank_key, bank_config['label']


def _user_display(user: User) -> tuple[str, str, str]:
    user_full_name = ' '.join(
        filter(None, [getattr(user, 'first_name', None), getattr(user, 'last_name', None)])
    ) or 'Неизвестно'
    username_clean = (getattr(user, 'username', None) or '').replace('@', '')
    at_username = f'@{username_clean}' if username_clean else ''
    return user_full_name, username_clean, at_username


async def _notify_admins(ticket_id: int, user: User, bank: str, amount_kopeks: int, dt_moscow: str) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.services.admin_notification_service import AdminNotificationService

    user_full_name, _, at_username = _user_display(user)
    telegram_id = int(getattr(user, 'telegram_id', 0) or 0)
    safe_name = html.escape(f'{user_full_name} {at_username}'.strip())

    text = (
        f'🎫 <b>НОВЫЙ ТИКЕТ СБП #{ticket_id}</b>\n\n'
        f'👤 <b>{safe_name}</b>\n'
        f'📱 TG: <code>{telegram_id}</code>\n\n'
        f'<b>Данные перевода:</b>\n\n'
        f'Банк: <b>{html.escape(bank)}</b>\n'
        f'Сумма: <b>{amount_kopeks / 100:.0f} ₽</b>\n\n'
        f'⏰ {dt_moscow}'
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='💰 Подтвердить платёж', callback_data=f'sbp_approve:{ticket_id}')]
        ]
    )

    async with create_bot() as bot:
        await AdminNotificationService(bot).send_ticket_event_notification(text, keyboard)


async def create_sbp_ticket(
    *,
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    bank_key: str,
    notify_admins: bool = True,
) -> dict[str, Any]:
    """Create a support ticket and Redis metadata for a manual SBP top-up."""
    _, bank_display = await normalize_bank(db, bank_key)
    dt_moscow = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M')
    user_full_name, _, at_username = _user_display(user)

    title = f'💰 💳 Пополнение баланса по СБП от {user_full_name} {at_username}'.strip()
    body = (
        '💰 💳 Пополнение баланса переводом через СБП\n\n'
        '<b>Данные перевода:</b>\n\n'
        f'Банк: <b>{bank_display}</b>\n'
        f'Сумма платежа: <b>{amount_kopeks / 100:.0f} ₽</b>\n\n'
        f'Время пополнения: {dt_moscow}'
    )

    ticket = await TicketCRUD.create_ticket(
        db=db,
        user_id=user.id,
        title=title,
        message_text=body,
        priority='high',
    )
    meta = {
        'user_id': user.id,
        'telegram_id': int(getattr(user, 'telegram_id', 0) or 0),
        'amount_kopeks': int(amount_kopeks),
        'bank': bank_display,
        'confirmed': False,
    }
    await cache.set(f'sbp_meta:{ticket.id}', meta, expire=SBP_META_TTL)

    if notify_admins:
        await _notify_admins(ticket.id, user, bank_display, amount_kopeks, dt_moscow)

    logger.info(
        'SBP manual payment ticket created',
        user_id=user.id,
        bank=bank_key,
        amount_kopeks=amount_kopeks,
        ticket_id=ticket.id,
    )
    return {
        'status': 'ticket_created',
        'ticket_id': ticket.id,
        'amount_kopeks': amount_kopeks,
        'amount_rubles': amount_kopeks / 100,
        'bank': bank_display,
    }


async def _notify_user_about_confirm(db: AsyncSession, ticket, message_text: str) -> None:
    try:
        from app.handlers.admin.tickets import notify_user_about_ticket_reply

        bot = create_bot()
        try:
            await notify_user_about_ticket_reply(bot, ticket, message_text, db)
        finally:
            await bot.session.close()
    except Exception as error:
        logger.warning('Failed to notify user about SBP ticket reply', error=error)

    try:
        from app.cabinet.routes.websocket import notify_user_ticket_reply
        from app.database.crud.ticket_notification import TicketNotificationCRUD

        notification = await TicketNotificationCRUD.create_user_notification_for_admin_reply(
            db, ticket, message_text
        )
        if notification:
            await notify_user_ticket_reply(ticket.user_id, ticket.id, message_text[:100])
    except Exception as error:
        logger.warning('Failed to create SBP cabinet ticket notification', error=error)


async def confirm_sbp_payment(
    *,
    db: AsyncSession,
    ticket_id: int,
    actor_label: str,
    source: str,
) -> dict[str, Any]:
    """Confirm a manual SBP top-up once, add a ticket reply, then close the ticket."""
    meta = await cache.get(f'sbp_meta:{ticket_id}')
    if not meta:
        raise SbpPaymentNotFound('Payment metadata not found or expired')
    if meta.get('confirmed'):
        raise SbpPaymentAlreadyConfirmed('Payment already confirmed')

    lock_key = f'sbp_confirm_lock:{ticket_id}'
    lock_acquired = await cache.setnx(lock_key, {'source': source, 'at': datetime.now(UTC).isoformat()}, expire=60)
    if not lock_acquired:
        raise SbpPaymentConfirmInProgress('Payment confirmation is already in progress')

    meta = await cache.get(f'sbp_meta:{ticket_id}')
    if not meta:
        raise SbpPaymentNotFound('Payment metadata not found or expired')
    if meta.get('confirmed'):
        raise SbpPaymentAlreadyConfirmed('Payment already confirmed')

    ok = await add_user_balance_by_id(
        db=db,
        telegram_id=meta['telegram_id'],
        amount_kopeks=meta['amount_kopeks'],
        description=f'СБП пополнение #{ticket_id} ({source})',
        transaction_type=TransactionType.DEPOSIT,
        payment_method=PaymentMethod.MANUAL,
    )
    if not ok:
        raise SbpBalanceCreditFailed('Balance credit failed')

    now_iso = datetime.now(UTC).isoformat()
    meta['confirmed'] = True
    meta['confirmed_at'] = now_iso
    meta['confirmed_by'] = actor_label
    meta['confirmed_source'] = source
    await cache.set(f'sbp_meta:{ticket_id}', meta, expire=SBP_META_TTL)

    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False, load_user=True)
    ticket_closed = False
    message_text = (
        f'✅ Баланс пополнен на {meta["amount_kopeks"] / 100:.0f} ₽. '
        f'Платёж СБП подтверждён.'
    )
    if ticket:
        await TicketMessageCRUD.add_message(
            db=db,
            ticket_id=ticket_id,
            user_id=ticket.user_id,
            message_text=message_text,
            is_from_admin=True,
        )
        ticket_closed = await TicketCRUD.close_ticket(db, ticket_id)
        await _notify_user_about_confirm(db, ticket, message_text)
    else:
        logger.warning('SBP payment confirmed but ticket not found', ticket_id=ticket_id)

    logger.info(
        'SBP payment confirmed',
        ticket_id=ticket_id,
        amount_kopeks=meta['amount_kopeks'],
        source=source,
        actor=actor_label,
        ticket_closed=ticket_closed,
    )
    return {
        'status': 'confirmed',
        'ticket_id': ticket_id,
        'amount_rubles': meta['amount_kopeks'] / 100,
        'bank': meta['bank'],
        'ticket_closed': ticket_closed,
    }
