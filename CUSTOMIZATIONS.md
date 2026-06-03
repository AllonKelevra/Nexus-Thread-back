# Nexus Thread Customizations

This fork keeps the production Nexus Thread custom backend layer for Bedolaga Bot.

## Deployment Model

- Runtime uses the upstream Docker image `fr1ngg/remnawave-bedolaga-telegram-bot:latest`.
- Nexus custom code is mounted over selected upstream files from `/opt/bedolaga-bot/custom`.
- Source of truth for those mounted files is `custom/overlay`.
- Secrets stay in production `.env`; do not commit runtime env files, logs, databases, Redis dumps, or backups.

## Active Features

### Manual SBP Top-Up

- Payment method id: `manual`.
- Display name: `По номеру телефона через СБП`.
- User flow creates a ticket with `sbp_meta:{ticket_id}` in Redis.
- Admin can confirm from the bot callback `sbp_approve:{ticket_id}` or cabinet endpoint.
- Confirm is idempotent: balance is credited once, ticket gets an admin message, then closes.

Cabinet API dependencies:

- `POST /cabinet/balance/sbp-ticket`
- `GET /cabinet/balance/ticket-actions/{ticket_id}`
- `POST /cabinet/balance/sbp-admin-confirm/{ticket_id}`

### YooMoney Auto Top-Up

- Payment method id: `yoomoney_donate`.
- Display name: `YooMoney`.
- Description: `Оплата картой, комиссия 3%, автоматическое пополнение`.
- User enters the gross amount; credited amount is gross minus 3%.
- Cabinet creates a YooMoney payment form label and redirects through a browser form submit.
- YooMoney HTTP notification validates `YOOMONEY_NOTIFICATION_SECRET` and credits balance automatically.

Cabinet API dependencies:

- `POST /cabinet/balance/yoomoney-auto-payment`
- `POST /cabinet/balance/yoomoney-notification`
- Legacy manual fallback remains in code but is not primary UI:
  `POST /cabinet/balance/yoomoney-ticket`,
  `POST /cabinet/balance/yoomoney-admin-confirm/{ticket_id}`.

### Cabinet Branding Custom Design

- Adds public/admin endpoints for global Nexus Thread design mode.
- Setting key: `CABINET_CUSTOM_DESIGN_ENABLED`.
- Superadmin toggles the design in cabinet branding settings.

Cabinet API dependencies:

- `GET /cabinet/branding/custom-design`
- `PATCH /cabinet/branding/custom-design`

## Production Bind Mount Map

Production `docker-compose.yml` mounts:

- `custom/overlay/config.py` -> `/app/app/config.py`
- `custom/overlay/services/payment_method_config_service.py` -> `/app/app/services/payment_method_config_service.py`
- `custom/overlay/services/sbp_manual_service.py` -> `/app/app/services/sbp_manual_service.py`
- `custom/overlay/services/yoomoney_manual_service.py` -> `/app/app/services/yoomoney_manual_service.py`
- `custom/overlay/cabinet/routes/balance.py` -> `/app/app/cabinet/routes/balance.py`
- `custom/overlay/cabinet/routes/branding.py` -> `/app/app/cabinet/routes/branding.py`
- `custom/overlay/states.py` -> `/app/app/states.py`
- `custom/overlay/handlers/balance/sbp_manual.py` -> `/app/app/handlers/balance/sbp_manual.py`
- `custom/overlay/handlers/balance/main.py` -> `/app/app/handlers/balance/main.py`
- `custom/overlay/keyboards/inline.py` -> `/app/app/keyboards/inline.py`
- `custom/overlay/utils/payment_utils.py` -> `/app/app/utils/payment_utils.py`

## Environment Keys

Required production keys are configured in `/opt/bedolaga-bot/.env`:

- `MANUAL_PAYMENT_ENABLED`
- `CABINET_ENABLED`
- `CABINET_URL`
- `CABINET_JWT_SECRET`
- `CABINET_ALLOWED_ORIGINS`
- `YOOMONEY_NOTIFICATION_SECRET`

Values must not be committed.

## Upstream Update Workflow

1. Backup production `docker-compose.yml`, `.env`, `custom/`, and `/srv/cabinet`.
2. Fetch upstream and update this fork.
3. Compare every file in `custom/overlay` against its upstream target.
4. Preserve upstream changes while keeping Nexus behavior.
5. Run Python syntax checks and relevant lint checks.
6. Upload `custom/overlay` to `/opt/bedolaga-bot/custom`.
7. Run `docker compose config`.
8. Recreate only `bedolaga_bot`.
