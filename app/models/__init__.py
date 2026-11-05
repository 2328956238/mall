"""模型包统一出口。

**这个文件必须导入所有模型模块**：`Base.metadata` 只有在模块被导入后才知道有哪些表。
之前这里是空文件，导致 `alembic revision --autogenerate` 生成空迁移
（看起来"没有差异"，实际一张表都没比对上）——问题记录见 docs/PROBLEMS.md 的 P8。

新增模型时记得在这里补一行，否则迁移会漏表。
"""

from .base import Base, TimestampMixin  # noqa: F401
from .inventory import (  # noqa: F401
    ReservationStatus,
    SKU,
    SkuStatus,
    StockReservation,
    StockTransaction,
    StockTxnType,
    Warehouse,
    WarehouseStock,
)
from .operation_log import OperationLog  # noqa: F401
from .rbac import (  # noqa: F401
    DataScope,
    Department,
    Permission,
    PermissionType,
    Role,
    RolePermission,
    UserRole,
)
from .order import Order, OrderStatus  # noqa: F401
from .product import Product  # noqa: F401
from .purchase import (  # noqa: F401
    POStatus,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceipt,
    PurchaseReceiptItem,
    ReceiptStatus,
    Supplier,
)
from .sales import (  # noqa: F401
    Customer,
    LogisticsTracking,
    SalesOrder,
    SalesOrderItem,
    SalesOrderStatus,
    Shipment,
    ShipmentItem,
    ShipmentStatus,
    TrackingStatus,
)
from .seckill_product import SeckillProduct  # noqa: F401
from .tenant import Tenant, TenantStatus  # noqa: F401
from .user import User  # noqa: F401

__all__ = [
    "Base",
    "TimestampMixin",
    "User",
    "Product",
    "SeckillProduct",
    "Order",
    "OrderStatus",
    "OperationLog",
    "Tenant",
    "TenantStatus",
    "SKU",
    "SkuStatus",
    "Warehouse",
    "WarehouseStock",
    "StockTransaction",
    "StockTxnType",
    "StockReservation",
    "ReservationStatus",
    "Supplier",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "PurchaseReceipt",
    "PurchaseReceiptItem",
    "POStatus",
    "ReceiptStatus",
    "Customer",
    "SalesOrder",
    "SalesOrderItem",
    "SalesOrderStatus",
    "Shipment",
    "ShipmentItem",
    "ShipmentStatus",
    "LogisticsTracking",
    "TrackingStatus",
    "Department",
    "Role",
    "Permission",
    "PermissionType",
    "DataScope",
    "UserRole",
    "RolePermission",
]
