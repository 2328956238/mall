from fastapi import HTTPException, status


class AppException(HTTPException):
    """基础应用异常"""
    def __init__(self, detail: str, status_code: int = status.HTTP_400_BAD_REQUEST):
        super().__init__(status_code=status_code, detail=detail)


class NotFoundException(AppException):
    """资源未找到"""
    def __init__(self, detail: str = "Resource not found"):
        super().__init__(detail=detail, status_code=status.HTTP_404_NOT_FOUND)


class UnauthorizedException(AppException):
    """未授权"""
    def __init__(self, detail: str = "Unauthorized"):
        super().__init__(detail=detail, status_code=status.HTTP_401_UNAUTHORIZED)


class ForbiddenException(AppException):
    """禁止访问"""
    def __init__(self, detail: str = "Forbidden"):
        super().__init__(detail=detail, status_code=status.HTTP_403_FORBIDDEN)


class ConflictException(AppException):
    """资源冲突"""
    def __init__(self, detail: str = "Conflict"):
        super().__init__(detail=detail, status_code=status.HTTP_409_CONFLICT)


class RateLimitException(AppException):
    """请求频率限制"""
    def __init__(self, detail: str = "Too many requests"):
        super().__init__(detail=detail, status_code=status.HTTP_429_TOO_MANY_REQUESTS)


class OutOfStockException(AppException):
    """库存不足"""
    def __init__(self, detail: str = "Out of stock"):
        super().__init__(detail=detail, status_code=status.HTTP_410_GONE)

class StockIsDerivedException(AppException):
    """products.stock 是只读汇总字段，不允许直接改。

    真正的库存在 warehouse_stocks（库存域）。直接改汇总字段会让
    汇总值与明细值分叉，对账永远对不平。
    """

    def __init__(self, detail: str = "库存由库存服务维护，不能直接修改"):
        super().__init__(detail=detail, status_code=status.HTTP_409_CONFLICT)
