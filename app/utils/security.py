import bcrypt
import hashlib
from jose import jwt
from datetime import datetime, timedelta
from app.config import settings


def get_password_hash(password: str) -> str:
    """使用 bcrypt 对密码进行哈希，自动截断超过72字节的部分"""
    # bcrypt 原生限制：密码不能超过72字节
    # 先做 SHA-256 摘要再用 bcrypt，避免长度限制问题
    # （更安全的方式：SHA-256 → base64 → bcrypt）
    password_bytes = password.encode('utf-8')
    # 用 SHA-256 预处理，解决 bcrypt 72字节限制
    prehashed = hashlib.sha256(password_bytes).hexdigest()
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(prehashed.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码是否匹配 bcrypt 哈希"""
    password_bytes = plain_password.encode('utf-8')
    prehashed = hashlib.sha256(password_bytes).hexdigest()
    return bcrypt.checkpw(
        prehashed.encode('utf-8'),
        hashed_password.encode('utf-8')
    )


def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except:
        return None