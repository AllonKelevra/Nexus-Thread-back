"""Runtime settings for custom payment providers."""

import json
import os
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.system_setting import delete_system_setting, get_setting_value, upsert_system_setting
from app.database.models import SystemSetting


MANUAL_CONFIG_KEY = 'CUSTOM_PAYMENT_MANUAL_CONFIG'
YOOMONEY_CONFIG_KEY = 'CUSTOM_PAYMENT_YOOMONEY_CONFIG'
YOOMONEY_SECRET_KEY = 'CUSTOM_PAYMENT_YOOMONEY_NOTIFICATION_SECRET_ENC'
YOOMONEY_SECRET_NAME = 'notification_secret'
MASTER_KEY_ENV = 'CUSTOM_PAYMENT_SETTINGS_MASTER_KEY'
LEGACY_YOOMONEY_SECRET_ENV = 'YOOMONEY_NOTIFICATION_SECRET'
FERNET_PREFIX = 'fernet:v1:'

CUSTOM_METHODS = {'manual', 'yoomoney_donate'}
SETTING_KEYS = {
    'manual': MANUAL_CONFIG_KEY,
    'yoomoney_donate': YOOMONEY_CONFIG_KEY,
}


class CustomPaymentSettingsError(RuntimeError):
    """Base custom payment settings error."""


class CustomPaymentSecretError(CustomPaymentSettingsError):
    """Secret cannot be encrypted or decrypted safely."""


class SbpBank(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=50, pattern=r'^[a-z0-9_-]+$')
    label: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    sort_order: int = Field(default=0, ge=0, le=1000)
    recommended: bool = False


class ManualProviderConfig(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    phone: str = Field(default='', max_length=64)
    banks: list[SbpBank] = Field(default_factory=list, max_length=20)
    instruction_html: str = Field(default='', max_length=20_000)
    description: str = Field(default='Перевод по номеру телефона через СБП', max_length=500)
    quick_amounts_kopeks: list[int] = Field(default_factory=lambda: [10_000, 30_000, 50_000, 100_000])

    @field_validator('quick_amounts_kopeks')
    @classmethod
    def validate_quick_amounts(cls, value: list[int]) -> list[int]:
        if len(value) > 20 or any(amount <= 0 or amount > 1_000_000_000 for amount in value):
            raise ValueError('quick_amounts_kopeks must contain up to 20 positive amounts')
        return list(dict.fromkeys(value))

    @model_validator(mode='after')
    def validate_banks(self):
        ids = [bank.id for bank in self.banks]
        if len(ids) != len(set(ids)):
            raise ValueError('bank ids must be unique')
        return self


class YooMoneyProviderConfig(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    receiver_wallet: str = Field(default='', max_length=64)
    fee_basis_points: int = Field(default=300, ge=0, le=10_000)
    description: str = Field(default='Оплата через YooMoney', max_length=500)
    quick_amounts_kopeks: list[int] = Field(default_factory=lambda: [10_000, 30_000, 50_000, 100_000])

    @field_validator('quick_amounts_kopeks')
    @classmethod
    def validate_quick_amounts(cls, value: list[int]) -> list[int]:
        if len(value) > 20 or any(amount <= 0 or amount > 1_000_000_000 for amount in value):
            raise ValueError('quick_amounts_kopeks must contain up to 20 positive amounts')
        return list(dict.fromkeys(value))


class SecretStatus(BaseModel):
    configured: bool
    source: Literal['database', 'environment', 'none']
    updated_at: datetime | None = None


class _RichTextSanitizer(HTMLParser):
    allowed_tags = {'p', 'br', 'strong', 'b', 'em', 'i', 'u', 'ul', 'ol', 'li', 'a'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {'script', 'style'}:
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in self.allowed_tags:
            return
        if tag == 'a':
            href = next((value for name, value in attrs if name.lower() == 'href'), None)
            if href and urlparse(href).scheme.lower() in {'http', 'https'}:
                self.parts.append(f'<a href="{escape(href, quote=True)}" rel="noopener noreferrer">')
                self.open_tags.append(tag)
            return
        self.parts.append(f'<{tag}>')
        if tag != 'br':
            self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {'script', 'style'} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth or tag not in self.open_tags:
            return
        while self.open_tags:
            current = self.open_tags.pop()
            self.parts.append(f'</{current}>')
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(escape(data))

    def result(self) -> str:
        while self.open_tags:
            self.parts.append(f'</{self.open_tags.pop()}>')
        return ''.join(self.parts)


def sanitize_instruction_html(value: str) -> str:
    sanitizer = _RichTextSanitizer()
    sanitizer.feed(value or '')
    sanitizer.close()
    return sanitizer.result()


def _config_model(method_id: str):
    if method_id == 'manual':
        return ManualProviderConfig
    if method_id == 'yoomoney_donate':
        return YooMoneyProviderConfig
    raise ValueError(f'Unsupported custom payment method: {method_id}')


def validate_provider_config(method_id: str, value: dict[str, Any]) -> dict[str, Any]:
    model = _config_model(method_id)
    normalized = dict(value)
    if method_id == 'manual':
        normalized['instruction_html'] = sanitize_instruction_html(str(normalized.get('instruction_html') or ''))
    return model.model_validate(normalized).model_dump()


async def get_provider_config(db: AsyncSession, method_id: str) -> dict[str, Any]:
    model = _config_model(method_id)
    raw = await get_setting_value(db, SETTING_KEYS[method_id])
    if not raw:
        return model().model_dump()
    try:
        value = json.loads(raw)
        return validate_provider_config(method_id, value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return model().model_dump()


async def save_provider_config(db: AsyncSession, method_id: str, value: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_provider_config(method_id, value)
    await upsert_system_setting(
        db,
        SETTING_KEYS[method_id],
        json.dumps(normalized, ensure_ascii=False, separators=(',', ':')),
        description=f'Runtime provider config for {method_id}',
    )
    await db.commit()
    return normalized


def _fernet() -> Fernet:
    raw_key = os.getenv(MASTER_KEY_ENV, '').strip()
    if not raw_key:
        raise CustomPaymentSecretError(f'{MASTER_KEY_ENV} is not configured')
    try:
        return Fernet(raw_key.encode('ascii'))
    except (ValueError, UnicodeEncodeError) as error:
        raise CustomPaymentSecretError(f'{MASTER_KEY_ENV} is invalid') from error


def encrypt_secret(plaintext: str) -> str:
    value = plaintext.strip()
    if not value:
        raise ValueError('Secret cannot be empty')
    return FERNET_PREFIX + _fernet().encrypt(value.encode('utf-8')).decode('ascii')


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext.startswith(FERNET_PREFIX):
        raise CustomPaymentSecretError('Unsupported encrypted secret format')
    try:
        return _fernet().decrypt(ciphertext.removeprefix(FERNET_PREFIX).encode('ascii')).decode('utf-8')
    except (InvalidToken, UnicodeError) as error:
        raise CustomPaymentSecretError('Encrypted secret cannot be decrypted') from error


async def set_secret(db: AsyncSession, method_id: str, secret_name: str, plaintext: str) -> SecretStatus:
    _validate_secret_name(method_id, secret_name)
    await upsert_system_setting(
        db,
        YOOMONEY_SECRET_KEY,
        encrypt_secret(plaintext),
        description='Encrypted YooMoney notification secret',
    )
    await db.commit()
    return await get_secret_status(db, method_id, secret_name)


async def delete_secret(db: AsyncSession, method_id: str, secret_name: str) -> SecretStatus:
    _validate_secret_name(method_id, secret_name)
    await delete_system_setting(db, YOOMONEY_SECRET_KEY)
    await db.commit()
    return await get_secret_status(db, method_id, secret_name)


def _validate_secret_name(method_id: str, secret_name: str) -> None:
    if method_id != 'yoomoney_donate' or secret_name != YOOMONEY_SECRET_NAME:
        raise ValueError('Unsupported custom payment secret')


async def get_secret_status(db: AsyncSession, method_id: str, secret_name: str) -> SecretStatus:
    _validate_secret_name(method_id, secret_name)
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == YOOMONEY_SECRET_KEY))
    setting = result.scalar_one_or_none()
    if setting and setting.value:
        return SecretStatus(configured=True, source='database', updated_at=setting.updated_at)
    if os.getenv(LEGACY_YOOMONEY_SECRET_ENV, '').strip():
        return SecretStatus(configured=True, source='environment', updated_at=None)
    return SecretStatus(configured=False, source='none', updated_at=None)


async def resolve_yoomoney_notification_secret(db: AsyncSession) -> str:
    ciphertext = await get_setting_value(db, YOOMONEY_SECRET_KEY)
    if ciphertext:
        return decrypt_secret(ciphertext)
    legacy = os.getenv(LEGACY_YOOMONEY_SECRET_ENV, '').strip()
    if legacy:
        return legacy
    raise CustomPaymentSecretError('YooMoney notification secret is not configured')


async def is_provider_configured(db: AsyncSession, method_id: str) -> bool:
    if method_id not in CUSTOM_METHODS or not settings.MANUAL_PAYMENT_ENABLED:
        return False
    config = await get_provider_config(db, method_id)
    if method_id == 'manual':
        return bool(config['phone'] and any(bank['enabled'] for bank in config['banks']))
    if not config['receiver_wallet']:
        return False
    try:
        return bool(await resolve_yoomoney_notification_secret(db))
    except CustomPaymentSecretError:
        return False


async def get_public_provider_config(db: AsyncSession, method_id: str) -> dict[str, Any]:
    config = await get_provider_config(db, method_id)
    if method_id == 'manual':
        config['banks'] = sorted(
            (bank for bank in config['banks'] if bank['enabled']),
            key=lambda bank: (bank['sort_order'], bank['label']),
        )
    return config


async def get_enabled_bank(db: AsyncSession, bank_id: str) -> dict[str, Any]:
    config = await get_provider_config(db, 'manual')
    bank = next((item for item in config['banks'] if item['id'] == bank_id and item['enabled']), None)
    if not bank:
        raise ValueError('Invalid SBP bank')
    return bank


def fee_percent(fee_basis_points: int) -> Decimal:
    return Decimal(fee_basis_points) / Decimal(100)


def calculate_credit_amount(gross_amount_kopeks: int, fee_basis_points: int) -> int:
    multiplier = (Decimal(10_000) - Decimal(fee_basis_points)) / Decimal(10_000)
    return int((Decimal(gross_amount_kopeks) * multiplier).quantize(Decimal(1), rounding=ROUND_HALF_UP))
