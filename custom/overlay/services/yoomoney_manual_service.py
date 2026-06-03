"""Shared helpers for manual YooMoney balance top-ups."""

import html
import hmac
import os
import secrets
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Any
from urllib.parse import quote

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.crud.user import add_user_balance_by_id
from app.database.models import PaymentMethod, TransactionType, User
from app.utils.cache import cache
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

YOOMONEY_META_TTL = 2_592_000
YOOMONEY_CONFIRM_LOCK_TTL = 60
YOOMONEY_AUTO_PENDING_TTL = 86_400
YOOMONEY_AUTO_OPERATION_TTL = 2_592_000
YOOMONEY_FEE_PERCENT = Decimal('3')
YOOMONEY_NET_MULTIPLIER = Decimal('0.97')
YOOMONEY_RECEIVER = 'REMOVED_YOOMONEY_WALLET'
YOOMONEY_FORM_ACTION = 'https://yoomoney.ru/quickpay/confirm'


class YooMoneyPaymentError(Exception):
    """Base YooMoney payment error."""


class YooMoneyPaymentNotFound(YooMoneyPaymentError):
    """YooMoney payment metadata does not exist."""


class YooMoneyPaymentAlreadyConfirmed(YooMoneyPaymentError):
    """YooMoney payment was already confirmed."""


class YooMoneyPaymentConfirmInProgress(YooMoneyPaymentError):
    """Another actor is confirming this payment now."""


class YooMoneyBalanceCreditFailed(YooMoneyPaymentError):
    """Balance credit failed."""


class YooMoneyNotificationSignatureInvalid(YooMoneyPaymentError):
    """YooMoney notification signature is invalid."""


class YooMoneyNotificationSecretMissing(YooMoneyPaymentError):
    """YooMoney notification secret is not configured."""


def calculate_credit_amount(gross_amount_kopeks: int) -> int:
    """Return YooMoney top-up amount after the 3% service commission."""
    return int(
        (Decimal(gross_amount_kopeks) * YOOMONEY_NET_MULTIPLIER).quantize(
            Decimal('1'),
            rounding=ROUND_HALF_UP,
        )
    )


def _amount_to_kopeks(value: str | int | float | Decimal | None) -> int:
    if value is None:
        return 0
    return int((Decimal(str(value).replace(',', '.')) * Decimal('100')).quantize(Decimal('1')))


def _verify_notification_sign(params: dict[str, Any]) -> None:
    secret = os.getenv('YOOMONEY_NOTIFICATION_SECRET', '').strip()
    if not secret:
        raise YooMoneyNotificationSecretMissing('YOOMONEY_NOTIFICATION_SECRET is not configured')

    incoming_sign = str(params.get('sign') or '').strip().lower()
    if not incoming_sign:
        raise YooMoneyNotificationSignatureInvalid('Missing YooMoney notification sign')

    payload_parts = []
    for key in sorted(k for k in params if k != 'sign'):
        value = '' if params[key] is None else str(params[key])
        payload_parts.append(f'{key}={quote(value, safe="")}')
    payload = '&'.join(payload_parts)
    expected = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), sha256).hexdigest()

    if not hmac.compare_digest(expected.lower(), incoming_sign):
        raise YooMoneyNotificationSignatureInvalid('Invalid YooMoney notification sign')


def _user_display(user: User) -> tuple[str, str, str]:
    user_full_name = ' '.join(
        filter(None, [getattr(user, 'first_name', None), getattr(user, 'last_name', None)])
    ) or 'Неизвестно'
    username_clean = (getattr(user, 'username', None) or '').replace('@', '')
    at_username = f'@{username_clean}' if username_clean else ''
    return user_full_name, username_clean, at_username


async def create_yoomoney_auto_payment(
    *,
    user: User,
    amount_kopeks: int,
    success_url: str | None = None,
) -> dict[str, Any]:
    """Create pending metadata and form fields for automatic YooMoney notification flow."""
    if amount_kopeks <= 0:
        raise ValueError('Invalid YooMoney amount')

    label = f'ntym-{user.id}-{int(datetime.now(UTC).timestamp())}-{secrets.token_urlsafe(6)}'
    credit_amount_kopeks = calculate_credit_amount(amount_kopeks)
    meta = {
        'user_id': user.id,
        'telegram_id': int(getattr(user, 'telegram_id', 0) or 0),
        'gross_amount_kopeks': int(amount_kopeks),
        'credit_amount_kopeks': int(credit_amount_kopeks),
        'fee_percent': int(YOOMONEY_FEE_PERCENT),
        'created_at': datetime.now(UTC).isoformat(),
        'status': 'pending',
    }
    await cache.set(f'yoomoney_auto_pending:{label}', meta, expire=YOOMONEY_AUTO_PENDING_TTL)

    amount_rubles = amount_kopeks / 100
    fields = {
        'receiver': YOOMONEY_RECEIVER,
        'quickpay-form': 'button',
        'paymentType': 'AC',
        'sum': f'{amount_rubles:.2f}',
        'label': label,
    }
    if success_url:
        fields['successURL'] = success_url

    logger.info(
        'YooMoney auto payment pending created',
        user_id=user.id,
        label=label,
        amount_kopeks=amount_kopeks,
        credit_amount_kopeks=credit_amount_kopeks,
    )
    return {
        'status': 'pending_created',
        'label': label,
        'receiver': YOOMONEY_RECEIVER,
        'form_action': YOOMONEY_FORM_ACTION,
        'form_fields': fields,
        'gross_amount_kopeks': amount_kopeks,
        'gross_amount_rubles': amount_rubles,
        'credit_amount_kopeks': credit_amount_kopeks,
        'credit_amount_rubles': credit_amount_kopeks / 100,
        'fee_percent': int(YOOMONEY_FEE_PERCENT),
    }


async def _notify_admins(ticket_id: int, user: User, amount_kopeks: int, dt_moscow: str) -> None:
    from app.services.admin_notification_service import AdminNotificationService

    user_full_name, _, at_username = _user_display(user)
    telegram_id = int(getattr(user, 'telegram_id', 0) or 0)
    safe_name = html.escape(f'{user_full_name} {at_username}'.strip())

    text = (
        f'🎫 <b>НОВЫЙ ТИКЕТ YOOMONEY #{ticket_id}</b>\n\n'
        f'👤 <b>{safe_name}</b>\n'
        f'📱 TG: <code>{telegram_id}</code>\n\n'
        f'<b>Данные перевода:</b>\n\n'
        f'Метод: <b>YooMoney</b>\n'
        f'Сумма платежа: <b>{amount_kopeks / 100:.0f} ₽</b>\n'
        f'К зачислению: <b>{calculate_credit_amount(amount_kopeks) / 100:.0f} ₽</b>\n'
        f'Комиссия: <b>{YOOMONEY_FEE_PERCENT:.0f}%</b>\n\n'
        f'⏰ {dt_moscow}'
    )

    async with create_bot() as bot:
        await AdminNotificationService(bot).send_ticket_event_notification(text, None)


async def create_yoomoney_ticket(
    *,
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    notify_admins: bool = True,
) -> dict[str, Any]:
    """Create a support ticket and Redis metadata for a manual YooMoney top-up."""
    if amount_kopeks <= 0:
        raise ValueError('Invalid YooMoney amount')

    dt_moscow = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M')
    credit_amount_kopeks = calculate_credit_amount(amount_kopeks)

    title = '💰 💳 Пополнение баланса через YooMoney'
    body = (
        '💰 💳 Пополнение баланса через YooMoney\n\n'
        '<b>Данные перевода:</b>\n\n'
        'Метод: <b>YooMoney</b>\n'
        f'Сумма платежа: <b>{amount_kopeks / 100:.0f} ₽</b>\n'
        f'К зачислению: <b>{credit_amount_kopeks / 100:.0f} ₽</b>\n'
        f'Комиссия сервиса: <b>{YOOMONEY_FEE_PERCENT:.0f}%</b>\n\n'
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
        'gross_amount_kopeks': int(amount_kopeks),
        'credit_amount_kopeks': int(credit_amount_kopeks),
        'fee_percent': int(YOOMONEY_FEE_PERCENT),
        'confirmed': False,
    }
    await cache.set(f'yoomoney_meta:{ticket.id}', meta, expire=YOOMONEY_META_TTL)

    if notify_admins:
        await _notify_admins(ticket.id, user, amount_kopeks, dt_moscow)

    logger.info(
        'YooMoney manual payment ticket created',
        user_id=user.id,
        ticket_id=ticket.id,
        amount_kopeks=amount_kopeks,
        credit_amount_kopeks=credit_amount_kopeks,
    )
    return {
        'status': 'ticket_created',
        'ticket_id': ticket.id,
        'gross_amount_kopeks': amount_kopeks,
        'gross_amount_rubles': amount_kopeks / 100,
        'credit_amount_kopeks': credit_amount_kopeks,
        'credit_amount_rubles': credit_amount_kopeks / 100,
        'fee_percent': int(YOOMONEY_FEE_PERCENT),
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
        logger.warning('Failed to notify user about YooMoney ticket reply', error=error)

    try:
        from app.cabinet.routes.websocket import notify_user_ticket_reply
        from app.database.crud.ticket_notification import TicketNotificationCRUD

        notification = await TicketNotificationCRUD.create_user_notification_for_admin_reply(
            db, ticket, message_text
        )
        if notification:
            await notify_user_ticket_reply(ticket.user_id, ticket.id, message_text[:100])
    except Exception as error:
        logger.warning('Failed to create YooMoney cabinet ticket notification', error=error)


async def confirm_yoomoney_payment(
    *,
    db: AsyncSession,
    ticket_id: int,
    actor_label: str,
    source: str,
) -> dict[str, Any]:
    """Confirm a manual YooMoney top-up once, add a ticket reply, then close the ticket."""
    meta = await cache.get(f'yoomoney_meta:{ticket_id}')
    if not meta:
        raise YooMoneyPaymentNotFound('Payment metadata not found or expired')
    if meta.get('confirmed'):
        raise YooMoneyPaymentAlreadyConfirmed('Payment already confirmed')

    lock_key = f'yoomoney_confirm_lock:{ticket_id}'
    lock_acquired = await cache.setnx(
        lock_key,
        {'source': source, 'at': datetime.now(UTC).isoformat()},
        expire=YOOMONEY_CONFIRM_LOCK_TTL,
    )
    if not lock_acquired:
        raise YooMoneyPaymentConfirmInProgress('Payment confirmation is already in progress')

    meta = await cache.get(f'yoomoney_meta:{ticket_id}')
    if not meta:
        raise YooMoneyPaymentNotFound('Payment metadata not found or expired')
    if meta.get('confirmed'):
        raise YooMoneyPaymentAlreadyConfirmed('Payment already confirmed')

    gross_amount_kopeks = int(meta.get('gross_amount_kopeks') or 0)
    if gross_amount_kopeks <= 0:
        raise ValueError('Invalid YooMoney amount')

    credit_amount_kopeks = calculate_credit_amount(gross_amount_kopeks)
    ok = await add_user_balance_by_id(
        db=db,
        telegram_id=meta['telegram_id'],
        amount_kopeks=credit_amount_kopeks,
        description=f'YooMoney пополнение #{ticket_id} ({source})',
        transaction_type=TransactionType.DEPOSIT,
        payment_method=PaymentMethod.MANUAL,
    )
    if not ok:
        raise YooMoneyBalanceCreditFailed('Balance credit failed')

    now_iso = datetime.now(UTC).isoformat()
    meta['gross_amount_kopeks'] = int(gross_amount_kopeks)
    meta['credit_amount_kopeks'] = int(credit_amount_kopeks)
    meta['confirmed'] = True
    meta['confirmed_at'] = now_iso
    meta['confirmed_by'] = actor_label
    meta['confirmed_source'] = source
    await cache.set(f'yoomoney_meta:{ticket_id}', meta, expire=YOOMONEY_META_TTL)

    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False, load_user=True)
    ticket_closed = False
    message_text = (
        f'✅ Баланс пополнен на {credit_amount_kopeks / 100:.0f} ₽. '
        f'Платёж YooMoney подтверждён. '
        f'Сумма доната: {gross_amount_kopeks / 100:.0f} ₽, комиссия YooMoney: {YOOMONEY_FEE_PERCENT:.0f}%.'
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
        logger.warning('YooMoney payment confirmed but ticket not found', ticket_id=ticket_id)

    logger.info(
        'YooMoney payment confirmed',
        ticket_id=ticket_id,
        gross_amount_kopeks=gross_amount_kopeks,
        credit_amount_kopeks=credit_amount_kopeks,
        source=source,
        actor=actor_label,
        ticket_closed=ticket_closed,
    )
    return {
        'status': 'confirmed',
        'ticket_id': ticket_id,
        'gross_amount_rubles': gross_amount_kopeks / 100,
        'credit_amount_rubles': credit_amount_kopeks / 100,
        'fee_percent': int(YOOMONEY_FEE_PERCENT),
        'ticket_closed': ticket_closed,
    }


async def process_yoomoney_notification(
    *,
    db: AsyncSession,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Process YooMoney HTTP notification for the automatic payment flow."""
    _verify_notification_sign(params)

    label = str(params.get('label') or '').strip()
    operation_id = str(params.get('operation_id') or '').strip()
    if not label or not operation_id:
        return {'status': 'ignored', 'reason': 'missing_label_or_operation_id'}

    meta = await cache.get(f'yoomoney_auto_pending:{label}')
    if not meta:
        return {'status': 'ignored', 'reason': 'unknown_label', 'label': label}

    operation_key = f'yoomoney_auto_operation:{operation_id}'
    operation_locked = await cache.setnx(
        operation_key,
        {'label': label, 'at': datetime.now(UTC).isoformat()},
        expire=YOOMONEY_AUTO_OPERATION_TTL,
    )
    if not operation_locked:
        return {'status': 'duplicate', 'operation_id': operation_id, 'label': label}

    gross_amount_kopeks = int(meta.get('gross_amount_kopeks') or 0)
    expected_credit_kopeks = int(meta.get('credit_amount_kopeks') or calculate_credit_amount(gross_amount_kopeks))
    notification_amount_kopeks = _amount_to_kopeks(params.get('amount'))
    withdraw_amount_kopeks = _amount_to_kopeks(params.get('withdraw_amount'))
    allowed_amounts = {gross_amount_kopeks, expected_credit_kopeks}
    if notification_amount_kopeks not in allowed_amounts and withdraw_amount_kopeks not in allowed_amounts:
        logger.warning(
            'YooMoney auto payment amount mismatch',
            label=label,
            operation_id=operation_id,
            gross_amount_kopeks=gross_amount_kopeks,
            expected_credit_kopeks=expected_credit_kopeks,
            notification_amount_kopeks=notification_amount_kopeks,
            withdraw_amount_kopeks=withdraw_amount_kopeks,
        )
        return {'status': 'ignored', 'reason': 'amount_mismatch', 'label': label}

    ok = await add_user_balance_by_id(
        db=db,
        telegram_id=meta['telegram_id'],
        amount_kopeks=expected_credit_kopeks,
        description=f'YooMoney авто-пополнение {operation_id}',
        transaction_type=TransactionType.DEPOSIT,
        payment_method=PaymentMethod.MANUAL,
    )
    if not ok:
        raise YooMoneyBalanceCreditFailed('Balance credit failed')

    meta['status'] = 'confirmed'
    meta['confirmed_at'] = datetime.now(UTC).isoformat()
    meta['operation_id'] = operation_id
    meta['notification_amount_kopeks'] = notification_amount_kopeks
    meta['withdraw_amount_kopeks'] = withdraw_amount_kopeks
    await cache.set(f'yoomoney_auto_pending:{label}', meta, expire=YOOMONEY_AUTO_OPERATION_TTL)

    logger.info(
        'YooMoney auto payment confirmed',
        label=label,
        operation_id=operation_id,
        user_id=meta.get('user_id'),
        amount_kopeks=expected_credit_kopeks,
    )
    return {
        'status': 'confirmed',
        'label': label,
        'operation_id': operation_id,
        'gross_amount_rubles': gross_amount_kopeks / 100,
        'credit_amount_rubles': expected_credit_kopeks / 100,
    }
