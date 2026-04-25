from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.services.user_service import UserService
from app.schemas.user import UserResponse
from app.schemas.common import APIResponse
from app.dependencies import get_current_user
from app.models.user import User
from typing import List

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=APIResponse[UserResponse])
async def get_current_user_info(
    current_user: User = Depends(get_current_user),
):
    """获取当前登录用户信息"""
    return APIResponse(data=current_user)


@router.get("/", response_model=APIResponse[List[UserResponse]])
async def get_users(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """获取用户列表"""
    service = UserService(db)
    users = await service.get_users()
    return APIResponse(data=users)


@router.get("/{user_id}", response_model=APIResponse[UserResponse])
async def get_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
):
    """根据 ID 获取用户详情"""
    service = UserService(db)
    user = await service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return APIResponse(data=user)