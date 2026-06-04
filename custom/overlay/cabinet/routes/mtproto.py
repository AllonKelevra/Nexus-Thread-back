"""Personal MTProto proxy route for cabinet users."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.database.models import User
from app.services.mtproto_service import MtprotoServiceError, mtproto_service

from ..dependencies import get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/mtproto', tags=['Cabinet MTProto'])


class MtprotoLinkResponse(BaseModel):
    url: str


@router.get('', response_model=MtprotoLinkResponse)
async def get_mtproto_link(user: User = Depends(get_current_cabinet_user)) -> MtprotoLinkResponse:
    if user.telegram_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Telegram account is not linked',
        )

    try:
        link = await mtproto_service.ensure_proxy_link_for_user(user)
    except MtprotoServiceError as error:
        logger.warning(
            'Не удалось получить персональную MTProto-ссылку для кабинета',
            user_id=user.id,
            telegram_id=user.telegram_id,
            error=str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Telegram proxy is temporarily unavailable',
        ) from error

    if not link:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Telegram proxy is not configured',
        )

    return MtprotoLinkResponse(url=link)
