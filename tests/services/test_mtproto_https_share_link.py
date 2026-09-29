import importlib.util
from pathlib import Path


def _load_overlay_module():
    overlay_path = (
        Path(__file__).resolve().parents[2]
        / 'custom'
        / 'overlay'
        / 'services'
        / 'mtproto_service.py'
    )
    spec = importlib.util.spec_from_file_location('nexus_mtproto_service', overlay_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proxy_link_uses_https_telegram_share_format():
    module = _load_overlay_module()
    link = 'tg://proxy?server=proxy.example&port=2053&secret=ee0123'

    result = module.MtprotoService._to_https_share_link(link)

    assert result == 'https://t.me/proxy?server=proxy.example&port=2053&secret=ee0123'


def test_non_proxy_link_is_not_rewritten():
    module = _load_overlay_module()
    link = 'https://example.com/path?value=1'

    assert module.MtprotoService._to_https_share_link(link) == link
