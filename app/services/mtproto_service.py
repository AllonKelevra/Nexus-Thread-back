from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
import structlog

from app.config import settings
from app.services.event_emitter import event_emitter


logger = structlog.get_logger(__name__)

_USERNAME_RE = re.compile(r'[^A-Za-z0-9_.-]+')


class MtprotoServiceError(RuntimeError):
    """Telemt API is unavailable or returned an invalid response."""


class MtprotoService:
    def __init__(self) -> None:
        self._timeout = httpx.Timeout(10.0, connect=5.0)

    @staticmethod
    def build_username(username: str | None, telegram_id: int) -> str:
        suffix = f'_{telegram_id}'
        clean = _USERNAME_RE.sub('_', (username or '').lstrip('@')).strip('_.-') or 'user'
        clean = clean[: 64 - len(suffix)].rstrip('_.-') or 'user'
        return f'{clean}{suffix}'

    @staticmethod
    def _unwrap_data(response: httpx.Response) -> Any:
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('ok') is not True or 'data' not in payload:
            raise MtprotoServiceError('Telemt API returned an invalid response')
        return payload['data']

    @staticmethod
    def _find_link(user: dict[str, Any]) -> str | None:
        links = user.get('links')
        if not isinstance(links, dict):
            return None

        for mode in ('tls', 'secure', 'classic'):
            values = links.get(mode)
            if isinstance(values, list):
                link = next((value for value in values if isinstance(value, str) and value), None)
                if link:
                    return link

        tls_domains = links.get('tls_domains')
        if isinstance(tls_domains, list):
            for item in tls_domains:
                if isinstance(item, dict) and isinstance(item.get('link'), str) and item['link']:
                    return item['link']

        return None

    @staticmethod
    def _use_public_host(link: str) -> str:
        public_host = settings.MTPROTO_PUBLIC_HOST.strip()
        if not public_host:
            return link

        parsed = urlsplit(link)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        replaced = [(key, public_host if key == 'server' else value) for key, value in query]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(replaced), parsed.fragment))

    async def _get_user(self, client: httpx.AsyncClient, username: str) -> dict[str, Any] | None:
        encoded_username = quote(username, safe='')
        response = await client.get(f'v1/users/{encoded_username}')
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = self._unwrap_data(response)
        return data if isinstance(data, dict) else None

    async def _find_user_by_telegram_id(
        self,
        client: httpx.AsyncClient,
        telegram_id: int,
    ) -> dict[str, Any] | None:
        response = await client.get('v1/users')
        response.raise_for_status()
        data = self._unwrap_data(response)
        if not isinstance(data, list):
            return None

        suffix = f'_{telegram_id}'
        return next(
            (
                user
                for user in data
                if isinstance(user, dict)
                and isinstance(user.get('username'), str)
                and user['username'].endswith(suffix)
            ),
            None,
        )

    async def ensure_proxy_link(self, telegram_id: int | None, username: str | None) -> str | None:
        api_url = (settings.MTPROTO_API_URL or '').rstrip('/') + '/'
        if not api_url or telegram_id is None:
            return None

        desired_username = self.build_username(username, telegram_id)

        try:
            async with httpx.AsyncClient(base_url=api_url, timeout=self._timeout) as client:
                user = await self._get_user(client, desired_username)
                if user is None:
                    user = await self._find_user_by_telegram_id(client, telegram_id)

                if user is None:
                    response = await client.post(
                        'v1/users',
                        json={
                            'username': desired_username,
                            'max_tcp_conns': settings.MTPROTO_MAX_TCP_CONNS,
                            'expiration_rfc3339': settings.MTPROTO_EXPIRATION_RFC3339,
                        },
                    )
                    if response.status_code == 409:
                        user = await self._get_user(client, desired_username)
                    else:
                        response.raise_for_status()
                        data = self._unwrap_data(response)
                        user = data.get('user') if isinstance(data, dict) else None

                if not isinstance(user, dict):
                    raise MtprotoServiceError('Telemt API did not return a user')

                link = self._find_link(user)
                if not link:
                    raise MtprotoServiceError('Telemt API did not return a proxy link')

                return self._use_public_host(link)
        except (httpx.HTTPError, ValueError) as error:
            raise MtprotoServiceError('Telemt API request failed') from error

    async def ensure_proxy_link_for_user(self, user: Any) -> str | None:
        return await self.ensure_proxy_link(
            telegram_id=getattr(user, 'telegram_id', None),
            username=getattr(user, 'username', None),
        )


mtproto_service = MtprotoService()


async def _provision_mtproto_user(event: dict[str, Any]) -> None:
    payload = event.get('payload')
    if not isinstance(payload, dict) or payload.get('telegram_id') is None:
        return

    try:
        await mtproto_service.ensure_proxy_link(
            telegram_id=payload['telegram_id'],
            username=payload.get('username'),
        )
    except MtprotoServiceError as error:
        logger.warning(
            'Не удалось создать пользователя MTProto после регистрации',
            telegram_id=payload.get('telegram_id'),
            error=str(error),
        )


event_emitter.on('user.created', _provision_mtproto_user)
