from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_current_admin
from app.db.session import get_db
from app.services.product_service import ProductService
from app.schemas.product import ProductCreate, ProductUpdate, ProductResponse
from app.schemas.common import APIResponse
from app.models.user import User
from typing import List

router = APIRouter(prefix="/products", tags=["admin"])


@router.get("/", response_model=APIResponse[List[ProductResponse]])
async def admin_list_products(
    skip: int = 0,
    limit: int = 100,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：获取商品列表"""
    service = ProductService(db)
    products = await service.get_products(skip, limit)
    return APIResponse(data=products)


@router.post("/", response_model=APIResponse[ProductResponse])
async def admin_create_product(
    product_in: ProductCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：创建商品"""
    service = ProductService(db)
    product = await service.create_product(product_in)
    return APIResponse(data=product, message="商品创建成功")


@router.put("/{product_id}", response_model=APIResponse[ProductResponse])
async def admin_update_product(
    product_id: int,
    product_in: ProductUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：更新商品信息"""
    service = ProductService(db)
    product = await service.update_product(product_id, product_in)
    if not product:
        raise HTTPException(404, "商品不存在")
    return APIResponse(data=product, message="商品更新成功")


@router.delete("/{product_id}", response_model=APIResponse)
async def admin_delete_product(
    product_id: int,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：删除商品"""
    service = ProductService(db)
    success = await service.delete_product(product_id)
    if not success:
        raise HTTPException(404, "商品不存在")
    return APIResponse(message="商品删除成功")