#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from datetime import datetime, timedelta

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from fastapi.security.utils import get_authorization_scheme_param
from jose import jwt
from passlib.context import CryptContext
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.common.exception.errors import AuthorizationError, TokenError
from backend.app.common.redis import redis_client
from backend.app.core.conf import settings
from backend.app.crud.crud_user import UserDao
from backend.app.models import User

pwd_context = CryptContext(schemes=['bcrypt'], deprecated='auto')

oauth2_schema = OAuth2PasswordBearer(tokenUrl=settings.TOKEN_URL_SWAGGER)


def get_hash_password(password: str) -> str:
    """
    Encrypt passwords using the hash algorithm

    :param password:
    :return:
    """
    return pwd_context.hash(password)


def password_verify(plain_password: str, hashed_password: str) -> bool:
    """
    Password verification

    :param plain_password: The password to verify
    :param hashed_password: The hash ciphers to compare
    :return:
    """
    return pwd_context.verify(plain_password, hashed_password)


async def create_access_token(sub: str, expires_delta: timedelta | None = None, **kwargs) -> tuple[str, datetime]:
    """
    Generate encryption token

    :param sub: The subject/userid of the JWT
    :param expires_delta: Increased expiry time
    :return:
    """
    if expires_delta:
        expire = datetime.now() + expires_delta
        expire_seconds = int(expires_delta.total_seconds())
    else:
        expire = datetime.now() + timedelta(seconds=settings.TOKEN_EXPIRE_SECONDS)
        expire_seconds = settings.TOKEN_EXPIRE_SECONDS
    multi_login = kwargs.pop('multi_login', None)
    to_encode = {'exp': expire, 'sub': sub, **kwargs}
    token = jwt.encode(to_encode, settings.TOKEN_SECRET_KEY, settings.TOKEN_ALGORITHM)
    if multi_login is False:
        prefix = f'{settings.TOKEN_REDIS_PREFIX}:{sub}:'
        await redis_client.delete_prefix(prefix)
    key = f'{settings.TOKEN_REDIS_PREFIX}:{sub}:{token}'
    await redis_client.setex(key, expire_seconds, token)
    return token, expire


async def create_refresh_token(sub: str, expire_time: datetime | None = None, **kwargs) -> tuple[str, datetime]:
    """
    Generate encryption refresh token, only used to create a new token

    :param sub: The subject/userid of the JWT
    :param expire_time: expiry time
    :return:
    """
    if expire_time:
        expire = expire_time + timedelta(seconds=settings.TOKEN_REFRESH_EXPIRE_SECONDS)
        expire_seconds = int((expire - datetime.now()).total_seconds())
    else:
        expire = datetime.now() + timedelta(seconds=settings.TOKEN_REFRESH_EXPIRE_SECONDS)
        expire_seconds = settings.TOKEN_REFRESH_EXPIRE_SECONDS
    multi_login = kwargs.pop('multi_login', None)
    to_encode = {'exp': expire, 'sub': sub, **kwargs}
    refresh_token = jwt.encode(to_encode, settings.TOKEN_SECRET_KEY, settings.TOKEN_ALGORITHM)
    if multi_login is False:
        prefix = f'{settings.TOKEN_REFRESH_REDIS_PREFIX}:{sub}:'
        await redis_client.delete_prefix(prefix)
    key = f'{settings.TOKEN_REFRESH_REDIS_PREFIX}:{sub}:{refresh_token}'
    await redis_client.setex(key, expire_seconds, refresh_token)
    return refresh_token, expire


async def create_new_token(sub: str, refresh_token: str, **kwargs) -> tuple[str, datetime]:
    """
    Generate new token

    :param sub:
    :param refresh_token:
    :return:
    """
    redis_refresh_token = await redis_client.get(f'{settings.TOKEN_REFRESH_REDIS_PREFIX}:{sub}:{refresh_token}')
    if not redis_refresh_token or redis_refresh_token != refresh_token:
        raise TokenError(msg='refresh_token 已过期')
    new_token, expire = await create_access_token(sub, **kwargs)
    return new_token, expire


def get_token(request: Request) -> str:
    """
    Get token for request header

    :return:
    """
    authorization = request.headers.get('Authorization')
    scheme, token = get_authorization_scheme_param(authorization)
    if not authorization or scheme.lower() != 'bearer':
        raise TokenError
    return token


def jwt_decode(token: str) -> tuple[int, list[int]]:
    """
    Decode token

    :param token:
    :return:
    """
    try:
        payload = jwt.decode(token, settings.TOKEN_SECRET_KEY, algorithms=[settings.TOKEN_ALGORITHM])
        user_id = int(payload.get('sub'))
        role_ids = list(payload.get('role_ids'))
        if not user_id or not role_ids:
            raise TokenError
    except (jwt.JWTError, ValidationError, Exception):
        raise TokenError
    return user_id, role_ids


async def jwt_authentication(token: str) -> dict[str, int]:
    """
    JWT authentication

    :param token:
    :return:
    """
    user_id, _ = jwt_decode(token)
    key = f'{settings.TOKEN_REDIS_PREFIX}:{user_id}:{token}'
    token_verify = await redis_client.get(key)
    if not token_verify:
        raise TokenError(msg='token 已过期')
    return {'sub': user_id}


async def get_current_user(db: AsyncSession, data: dict) -> User:
    """
    Get the current user through token

    :param db:
    :param data:
    :return:
    """
    user_id = data.get('sub')
    user = await UserDao.get_with_relation(db, user_id=user_id)
    if not user:
        raise TokenError
    if not user.is_active:
        raise AuthorizationError(msg='用户已锁定')
    return user


async def superuser_verify(request: Request) -> bool:
    """
    Verify the current user permissions through token

    :param request:
    :return:
    """
    is_superuser = request.user.is_superuser
    if not is_superuser:
        raise AuthorizationError
    return is_superuser


# Jwt verify dependency
DependsJwtAuth = Depends(oauth2_schema)
