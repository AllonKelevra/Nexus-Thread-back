from types import SimpleNamespace

import httpx
import pytest

from app.services.mtproto_service import MtprotoService


def _response(status_code: int, data) -> httpx.Response:
    request = httpx.Request('GET', 'https://telemt.example/v1/users')
    return httpx.Response(status_code, request=request, json={'ok': True, 'data': data})


class FakeClient:
    def __init__(self, users: list[dict] | None = None) -> None:
        self.users = users or []
        self.posts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, path: str) -> httpx.Response:
        if path == 'v1/users':
            return _response(200, self.users)

        username = path.rsplit('/', 1)[-1]
        user = next((item for item in self.users if item['username'] == username), None)
        return _response(200, user) if user else _response(404, {})

    async def post(self, path: str, json: dict) -> httpx.Response:
        assert path == 'v1/users'
        self.posts.append(json)
        user = {
            'username': json['username'],
            'links': {'tls': ['tg://proxy?server=old.example&port=443&secret=abc']},
        }
        self.users.append(user)
        return _response(200, {'user': user, 'secret': 'not-logged'})


def test_build_username_matches_required_format_and_limit():
    username = MtprotoService.build_username('@user name/with:bad*chars' * 5, 123456789)

    assert username.endswith('_123456789')
    assert len(username) <= 64
    assert all(char.isalnum() or char in '_.-' for char in username)


def test_find_link_prefers_tls():
    user = {
        'links': {
            'classic': ['tg://proxy?server=classic'],
            'secure': ['tg://proxy?server=secure'],
            'tls': ['tg://proxy?server=tls'],
        }
    }

    assert MtprotoService._find_link(user) == 'tg://proxy?server=tls'


@pytest.mark.asyncio
async def test_ensure_proxy_link_creates_user_with_required_limits(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr('app.services.mtproto_service.settings.MTPROTO_API_URL', 'https://telemt.example')
    monkeypatch.setattr('app.services.mtproto_service.settings.MTPROTO_PUBLIC_HOST', 'cloud.nexus-thread.com')
    monkeypatch.setattr('app.services.mtproto_service.settings.MTPROTO_MAX_TCP_CONNS', 3)
    monkeypatch.setattr(
        'app.services.mtproto_service.settings.MTPROTO_EXPIRATION_RFC3339',
        '2099-12-31T23:59:59Z',
    )
    monkeypatch.setattr('app.services.mtproto_service.httpx.AsyncClient', lambda **kwargs: client)

    link = await MtprotoService().ensure_proxy_link(12345, 'test_user')

    assert link == 'tg://proxy?server=cloud.nexus-thread.com&port=443&secret=abc'
    assert client.posts == [
        {
            'username': 'test_user_12345',
            'max_tcp_conns': 3,
            'expiration_rfc3339': '2099-12-31T23:59:59Z',
        }
    ]


@pytest.mark.asyncio
async def test_ensure_proxy_link_reuses_existing_user_by_telegram_suffix(monkeypatch):
    existing = {
        'username': 'old_name_12345',
        'links': {'secure': ['tg://proxy?server=old.example&port=443&secret=abc']},
    }
    client = FakeClient([existing])
    monkeypatch.setattr('app.services.mtproto_service.settings.MTPROTO_API_URL', 'https://telemt.example')
    monkeypatch.setattr('app.services.mtproto_service.settings.MTPROTO_PUBLIC_HOST', 'cloud.nexus-thread.com')
    monkeypatch.setattr('app.services.mtproto_service.httpx.AsyncClient', lambda **kwargs: client)

    link = await MtprotoService().ensure_proxy_link_for_user(
        SimpleNamespace(telegram_id=12345, username='new_name')
    )

    assert link == 'tg://proxy?server=cloud.nexus-thread.com&port=443&secret=abc'
    assert client.posts == []
