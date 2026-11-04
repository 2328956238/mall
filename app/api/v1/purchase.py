"""采购 API。

租户身份暂时取 `current_user.tenant_id`（第一阶段所有用户都在默认租户下），
第四阶段接入真实租户上下文后这里不用改。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_user
from app.models.purchase import PurchaseOrder, PurchaseOrderItem, Supplier
from app.models.user import User
from app.schemas.common import APIResponse
from app.schemas.purchase import (
    ConfirmReceiptRequest,
    POItemResponse,
    PurchaseOrderCreate,
    PurchaseOrderDetail,
    PurchaseOrderResponse,
    ReceiptResponse,
    SupplierCreate,
    SupplierResponse,
)
from app.services.purchase_service import PurchaseService, ReceiptLine

router = APIRouter(prefix="/purchase", tags=["purchase"])


# ---------------------------------------------------------------------------
# 供应商
# ---------------------------------------------------------------------------
@router.post("/suppliers", response_model=APIResponse[SupplierResponse], status_code=201)
async def create_supplier(
    payload: SupplierCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = PurchaseService(db)
    supplier = await svc.create_supplier(
        tenant_id=current_user.tenant_id,
        code=payload.code, name=payload.name, contact=payload.contact,
        email=payload.email, phone=payload.phone, country=payload.country,
    )
    return APIResponse(data=SupplierResponse.model_validate(supplier), message="created")


@router.get("/suppliers", response_model=APIResponse[list[SupplierResponse]])
async def list_suppliers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(Supplier).where(Supplier.tenant_id == current_user.tenant_id)
            .order_by(Supplier.id)
        )
    ).scalars().all()
    return APIResponse(data=[SupplierResponse.model_validate(s) for s in rows])


# ---------------------------------------------------------------------------
# 采购单
# ---------------------------------------------------------------------------
@router.post("/orders", response_model=APIResponse[PurchaseOrderResponse], status_code=201)
async def create_po(
    payload: PurchaseOrderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = PurchaseService(db)
    po = await svc.create_po(
        tenant_id=current_user.tenant_id,
        supplier_id=payload.supplier_id,
        items=[i.model_dump() for i in payload.items],
        created_by=current_user.email,
        remark=payload.remark,
        currency=payload.currency,
    )
    return APIResponse(data=PurchaseOrderResponse.model_validate(po), message="created")


@router.get("/orders", response_model=APIResponse[list[PurchaseOrderResponse]])
async def list_po(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(PurchaseOrder).where(PurchaseOrder.tenant_id == current_user.tenant_id)
            .order_by(PurchaseOrder.id.desc())
        )
    ).scalars().all()
    return APIResponse(data=[PurchaseOrderResponse.model_validate(p) for p in rows])


@router.get("/orders/{po_id}", response_model=APIResponse[PurchaseOrderDetail])
async def get_po(
    po_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = PurchaseService(db)
    po = await svc.get_po(current_user.tenant_id, po_id)
    items = (
        await db.execute(
            select(PurchaseOrderItem).where(PurchaseOrderItem.po_id == po.id)
        )
    ).scalars().all()
    # 不要用 model_validate(po)：它会去读 po.items 这个懒加载关系，
    # 在同步上下文里会抛 MissingGreenlet。这里显式构造。
    detail = PurchaseOrderDetail(
        id=po.id, order_no=po.order_no, supplier_id=po.supplier_id,
        status=po.status.value, total_amount=po.total_amount,
        currency=po.currency, created_at=po.created_at,
        items=[POItemResponse.model_validate(i) for i in items],
    )
    return APIResponse(data=detail)


@router.post("/orders/{po_id}/submit", response_model=APIResponse[PurchaseOrderResponse])
async def submit_po(
    po_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    po = await PurchaseService(db).submit(current_user.tenant_id, po_id)
    return APIResponse(data=PurchaseOrderResponse.model_validate(po), message="submitted")


@router.post("/orders/{po_id}/approve", response_model=APIResponse[PurchaseOrderResponse])
async def approve_po(
    po_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    po = await PurchaseService(db).approve(
        current_user.tenant_id, po_id, approved_by=current_user.email
    )
    return APIResponse(data=PurchaseOrderResponse.model_validate(po), message="approved")


@router.post("/orders/{po_id}/reject", response_model=APIResponse[PurchaseOrderResponse])
async def reject_po(
    po_id: int,
    reason: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    po = await PurchaseService(db).reject(current_user.tenant_id, po_id, reason)
    return APIResponse(data=PurchaseOrderResponse.model_validate(po), message="rejected")


@router.post("/orders/{po_id}/cancel", response_model=APIResponse[PurchaseOrderResponse])
async def cancel_po(
    po_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    po = await PurchaseService(db).cancel(current_user.tenant_id, po_id)
    return APIResponse(data=PurchaseOrderResponse.model_validate(po), message="cancelled")


# ---------------------------------------------------------------------------
# 收货
# ---------------------------------------------------------------------------
@router.post("/orders/{po_id}/receipts", response_model=APIResponse[dict], status_code=201)
async def create_receipt(
    po_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    receipt = await PurchaseService(db).create_receipt_task(
        current_user.tenant_id, po_id, operator_id=current_user.email
    )
    return APIResponse(
        data={"receipt_id": receipt.id, "receipt_no": receipt.receipt_no},
        message="receipt task created",
    )


@router.post("/receipts/{receipt_id}/confirm", response_model=APIResponse[ReceiptResponse])
async def confirm_receipt(
    receipt_id: int,
    payload: ConfirmReceiptRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lines = []
    if payload and payload.lines:
        lines = [ReceiptLine(po_item_id=l.po_item_id, qty=l.qty) for l in payload.lines]
    result = await PurchaseService(db).confirm_receipt(
        current_user.tenant_id, receipt_id, lines=lines
    )
    return APIResponse(data=ReceiptResponse(**result), message="confirmed")
