import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.services.custom_payment_settings_service import (
    CustomPaymentSecretError,
    calculate_credit_amount,
    decrypt_secret,
    encrypt_secret,
    validate_provider_config,
)


def test_manual_config_sanitizes_rich_text_and_rejects_duplicate_banks():
    config = validate_provider_config(
        'manual',
        {
            'phone': '+7 000 000 00 00',
            'banks': [{'id': 'bank', 'label': 'Банк', 'enabled': True, 'sort_order': 0}],
            'instruction_html': (
                '<p onclick="steal()">Текст<script>alert(1)</script>'
                '<a href="javascript:alert(2)">опасно</a><a href="https://example.com">ссылка</a></p>'
            ),
            'description': 'СБП',
            'quick_amounts_kopeks': [10_000, 10_000, 30_000],
        },
    )

    assert 'script' not in config['instruction_html']
    assert 'onclick' not in config['instruction_html']
    assert 'javascript:' not in config['instruction_html']
    assert 'https://example.com' in config['instruction_html']
    assert config['quick_amounts_kopeks'] == [10_000, 30_000]

    with pytest.raises(ValidationError):
        validate_provider_config(
            'manual',
            {
                **config,
                'banks': [
                    {'id': 'same', 'label': 'A', 'enabled': True, 'sort_order': 0},
                    {'id': 'same', 'label': 'B', 'enabled': True, 'sort_order': 1},
                ],
            },
        )


def test_yoomoney_fee_uses_basis_points():
    assert calculate_credit_amount(10_000, 300) == 9_700
    assert calculate_credit_amount(10_001, 250) == 9_751


def test_secret_is_versioned_encrypted_and_never_plaintext(monkeypatch):
    key = Fernet.generate_key().decode('ascii')
    monkeypatch.setenv('CUSTOM_PAYMENT_SETTINGS_MASTER_KEY', key)

    ciphertext = encrypt_secret('notification-secret')

    assert ciphertext.startswith('fernet:v1:')
    assert 'notification-secret' not in ciphertext
    assert decrypt_secret(ciphertext) == 'notification-secret'


def test_secret_crypto_fails_closed_without_or_with_wrong_master_key(monkeypatch):
    monkeypatch.delenv('CUSTOM_PAYMENT_SETTINGS_MASTER_KEY', raising=False)
    with pytest.raises(CustomPaymentSecretError):
        encrypt_secret('secret')

    good_key = Fernet.generate_key().decode('ascii')
    monkeypatch.setenv('CUSTOM_PAYMENT_SETTINGS_MASTER_KEY', good_key)
    ciphertext = encrypt_secret('secret')
    monkeypatch.setenv('CUSTOM_PAYMENT_SETTINGS_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    with pytest.raises(CustomPaymentSecretError):
        decrypt_secret(ciphertext)
