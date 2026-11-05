"""销售与履约 API。"""

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.order import Order
from app.models.sales import (
    Customer,
    LogisticsTracking,
    SalesOrder,
    SalesOrderItem,
    Shipment,
    TrackingStatus,
)
from app.models.user import User
from app.schemas.common import APIResponse
from app.schemas.sales import (
    ConfirmShipmentRequest,
    CustomerCreate,
    CustomerResponse,
    SalesItemResponse,
    SalesOrderCreate,
    SalesOrderDetail,
    SalesOrderResponse,
    ShipmentResult,
    TrackingCreate,
    TrackingResponse,
    UnifiedOrder,
)
from app.services.permission_service import require_permission
from app.services.sales_service import SalesService, ShipLine

router = APIRouter(prefix="/sales", tags=["sales"])


# ---------------------------------------------------------------------------
# 客户
# ---------------------------------------------------------------------------
@router.post("/customers", response_model=APIResponse[CustomerResponse], status_code=201)
async def create_customer(
    payload: CustomerCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = await SalesService(db).create_customer(
        tenant_id=current_user.tenant_id,
        code=payload.code, name=payload.name, contact=payload.contact,
        email=payload.email, phone=payload.phone, country=payload.country,
        address=payload.address,
    )
    return APIResponse(data=CustomerResponse.model_validate(c), message="created")


@router.get("/customers", response_model=APIResponse[list[CustomerResponse]])
async def list_customers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(Customer).where(Customer.tenant_id == current_user.tenant_id)
            .order_by(Customer.id)
        )
    ).scalars().all()
    return APIResponse(data=[CustomerResponse.model_validate(c) for c in rows])


# ---------------------------------------------------------------------------
# 销售订单
# ---------------------------------------------------------------------------
@router.post("/orders", response_model=APIResponse[SalesOrderResponse], status_code=201,
             dependencies=[Depends(require_permission("sales.create"))])
async def create_sales_order(
    payload: SalesOrderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await SalesService(db).create_order(
        tenant_id=current_user.tenant_id,
        customer_id=payload.customer_id,
        items=[i.model_dump() for i in payload.items],
        user_id=str(current_user.id),
        created_by=current_user.email,
        currency=payload.currency,
        shipping_address=payload.shipping_address,
        remark=payload.remark,
    )
    return APIResponse(data=SalesOrderResponse.model_validate(order), message="created")


@router.get("/orders", response_model=APIResponse[list[SalesOrderResponse]])
async def list_sales_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列表受数据权限约束：`self` 范围只看自己的单。"""
    from app.config import settings
    from app.models.rbac import DataScope
    from app.services.permission_service import PermissionService

    stmt = select(SalesOrder).where(SalesOrder.tenant_id == current_user.tenant_id)

    if settings.ENFORCE_PERMISSIONS:
        scope = await PermissionService(db).effective_scope(str(current_user.id))
        if scope == DataScope.SELF:
            stmt = stmt.where(SalesOrder.user_id == str(current_user.id))
        elif scope == DataScope.DEPARTMENT:
            me = (
                await db.execute(
                    select(User.department_id).where(User.id == current_user.id)
                )
            ).scalar_one_or_none()
            if me is None:
                stmt = stmt.where(SalesOrder.user_id == str(current_user.id))
            else:
                svc = PermissionService(db)
                dept_ids = await svc.department_subtree_ids(current_user.tenant_id, me)
                user_ids = (
                    await db.execute(
                        select(User.id).where(User.department_id.in_(dept_ids))
                    )
                ).scalars().all()
                stmt = stmt.where(SalesOrder.user_id.in_([str(u) for u in user_ids]))

    rows = (await db.execute(stmt.order_by(SalesOrder.id.desc()))).scalars().all()
    return APIResponse(data=[SalesOrderResponse.model_validate(o) for o in rows])


@router.get("/orders/{order_id}", response_model=APIResponse[SalesOrderDetail])
async def get_sales_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = SalesService(db)
    order = await svc.get_order(current_user.tenant_id, order_id)
    items = (
        await db.execute(
            select(SalesOrderItem).where(SalesOrderItem.sales_order_id == order.id)
        )
    ).scalars().all()
    return APIResponse(data=SalesOrderDetail(
        id=order.id, order_no=order.order_no, customer_id=order.customer_id,
        status=order.status.value, total_amount=order.total_amount,
        currency=order.currency, shipping_address=order.shipping_address,
        created_at=order.created_at,
        items=[SalesItemResponse.model_validate(i) for i in items],
    ))


@router.post("/orders/{order_id}/approve", response_model=APIResponse[SalesOrderResponse],
             dependencies=[Depends(require_permission("sales.approve"))])
async def approve_sales_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await SalesService(db).approve(
        current_user.tenant_id, order_id, approved_by=current_user.email
    )
    return APIResponse(data=SalesOrderResponse.model_validate(order), message="approved")


@router.post("/orders/{order_id}/cancel", response_model=APIResponse[SalesOrderResponse])
async def cancel_sales_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await SalesService(db).cancel(current_user.tenant_id, order_id)
    return APIResponse(data=SalesOrderResponse.model_validate(order), message="cancelled")


@router.post("/orders/{order_id}/complete", response_model=APIResponse[SalesOrderResponse])
async def complete_sales_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    order = await SalesService(db).complete(current_user.tenant_id, order_id)
    return APIResponse(data=SalesOrderResponse.model_validate(order), message="completed")


# ---------------------------------------------------------------------------
# 发货与物流
# ---------------------------------------------------------------------------
@router.post("/orders/{order_id}/shipments", response_model=APIResponse[dict], status_code=201)
async def create_shipment(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    shipment = await SalesService(db).create_shipment(
        current_user.tenant_id, order_id, operator_id=current_user.email
    )
    return APIResponse(
        data={"shipment_id": shipment.id, "shipment_no": shipment.shipment_no},
        message="shipment task created",
    )


@router.post("/shipments/{shipment_id}/confirm", response_model=APIResponse[ShipmentResult],
             dependencies=[Depends(require_permission("sales.ship"))])
async def confirm_shipment(
    shipment_id: int,
    payload: ConfirmShipmentRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lines = []
    if payload and payload.lines:
        lines = [ShipLine(sales_order_item_id=l.sales_order_item_id, qty=l.qty)
                 for l in payload.lines]
    result = await SalesService(db).confirm_shipment(
        current_user.tenant_id, shipment_id,
        lines=lines,
        carrier=payload.carrier if payload else None,
        tracking_no=payload.tracking_no if payload else None,
    )
    return APIResponse(data=ShipmentResult(**result), message="shipped")


@router.post("/shipments/{shipment_id}/tracking",
             response_model=APIResponse[TrackingResponse], status_code=201)
async def add_tracking(
    shipment_id: int,
    payload: TrackingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = await SalesService(db).add_tracking(
        tenant_id=current_user.tenant_id, shipment_id=shipment_id,
        carrier=payload.carrier, tracking_no=payload.tracking_no,
        status=TrackingStatus(payload.status), description=payload.description,
        occurred_at=payload.occurred_at,
    )
    return APIResponse(data=TrackingResponse.model_validate(row), message="created")


@router.get("/shipments/{shipment_id}/tracking",
            response_model=APIResponse[list[TrackingResponse]])
async def list_tracking(
    shipment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(LogisticsTracking).where(
                LogisticsTracking.tenant_id == current_user.tenant_id,
                LogisticsTracking.shipment_id == shipment_id,
            ).order_by(LogisticsTracking.occurred_at, LogisticsTracking.id)
        )
    ).scalars().all()
    return APIResponse(data=[TrackingResponse.model_validate(r) for r in rows])
