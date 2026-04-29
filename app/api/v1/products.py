from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.db.session import get_db
from app.services.product_service import ProductService
from app.schemas.product import ProductCreate, ProductUpdate, ProductResponse

router = APIRouter(prefix="/products", tags=["products"])

@router.get("/", response_model=list[ProductResponse])
async def get_products(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db)
):
    """
    获取产品列表
    
    返回系统中所有可用的产品，支持分页查询。
    
    Args:
        skip (int): 跳过的记录数，用于分页（默认: 0）
        limit (int): 每页返回的最大记录数（默认: 100，最大: 1000）
        db (AsyncSession): 数据库会话依赖注入
        
    Returns:
        list[ProductResponse]: 产品列表，包含产品详细信息
        
    Example:
        ```bash
        curl -X GET "http://localhost:8000/api/v1/products/?skip=0&limit=10"
        ```
    """
    service = ProductService(db)
    products = await service.get_products(skip=skip, limit=limit)
    return [ProductResponse.model_validate(p) for p in products]

@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    根据ID获取特定产品
    
    通过产品ID获取产品的详细信息。
    
    Args:
        product_id (int): 产品ID，必须是正整数
        db (AsyncSession): 数据库会话依赖注入
        
    Returns:
        ProductResponse: 产品详细信息
        
    Raises:
        HTTPException: 404 - 当产品不存在时
        
    Example:
        ```bash
        curl -X GET "http://localhost:8000/api/v1/products/123"
        ```
    """
    service = ProductService(db)
    product = await service.get_product_by_id(product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found"
        )
    return ProductResponse.model_validate(product)

@router.post("/", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    product: ProductCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    创建新产品
    
    在系统中创建一个新的产品。需要产品的基本信息。
    
    Args:
        product (ProductCreate): 产品创建数据，包含名称、描述、价格、库存等
        db (AsyncSession): 数据库会话依赖注入
        
    Returns:
        ProductResponse: 新创建的产品信息
        
    Example:
        ```bash
        curl -X POST "http://localhost:8000/api/v1/products/" \
          -H "Content-Type: application/json" \
          -d '{"name": "iPhone 15", "description": "最新款苹果手机", "price": 7999.99, "stock": 100}'
        ```
    """
    service = ProductService(db)
    new_product = await service.create_product(product)
    return ProductResponse.model_validate(new_product)

@router.put("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: int,
    product: ProductUpdate,
    db: AsyncSession = Depends(get_db)
):
    """
    更新产品信息
    
    更新指定产品的信息。可以更新部分或全部字段。
    
    Args:
        product_id (int): 要更新的产品ID
        product (ProductUpdate): 产品更新数据，可以包含部分字段
        db (AsyncSession): 数据库会话依赖注入
        
    Returns:
        ProductResponse: 更新后的产品信息
        
    Raises:
        HTTPException: 404 - 当产品不存在时
        
    Example:
        ```bash
        curl -X PUT "http://localhost:8000/api/v1/products/123" \
          -H "Content-Type: application/json" \
          -d '{"price": 6999.99, "stock": 50}'
        ```
    """
    service = ProductService(db)
    updated = await service.update_product(product_id, product)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found"
        )
    return ProductResponse.model_validate(updated)

@router.delete("/{product_id}")
async def delete_product(
    product_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    删除产品
    
    从系统中永久删除指定的产品。
    
    Args:
        product_id (int): 要删除的产品ID
        db (AsyncSession): 数据库会话依赖注入
        
    Returns:
        dict: 删除成功消息
        
    Raises:
        HTTPException: 404 - 当产品不存在时
        
    Example:
        ```bash
        curl -X DELETE "http://localhost:8000/api/v1/products/123"
        ```
    """
    service = ProductService(db)
    success = await service.delete_product(product_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with ID {product_id} not found"
        )
    return {"message": f"Product {product_id} deleted successfully"}