from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.services.user_service import UserService
from app.utils.security import verify_password, create_access_token
from app.schemas.user import UserCreate, UserResponse
from app.schemas.common import APIResponse

router = APIRouter(prefix="/auth", tags=["authentication"])

@router.post("/register", response_model=APIResponse[UserResponse], status_code=status.HTTP_201_CREATED)
async def register(user_in: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    用户注册
    
    创建新的用户账户。需要提供邮箱、用户名和密码。
    
    Args:
        user_in (UserCreate): 用户注册数据，包含：
            - email (str): 用户邮箱，必须是有效的邮箱格式
            - username (str): 用户名，3-50个字符
            - password (str): 密码，至少8个字符，包含字母和数字
        db (AsyncSession): 数据库会话依赖注入
        
    Returns:
        APIResponse[UserResponse]: 注册响应，包含创建的用户信息和成功消息
        
    Raises:
        HTTPException: 400 - 当邮箱已被注册时
        
    Example:
        ```bash
        curl -X POST "http://localhost:8000/api/v1/auth/register" \
          -H "Content-Type: application/json" \
          -d '{"email": "user@example.com", "username": "testuser", "password": "Password123"}'
        ```
        
    Response Example:
        ```json
        {
          "success": true,
          "message": "User created",
          "data": {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "email": "user@example.com",
            "username": "testuser",
            "created_at": "2026-04-13T10:30:00Z",
            "updated_at": "2026-04-13T10:30:00Z"
          }
        }
        ```
    """
    service = UserService(db)
    existing = await service.get_user_by_email(user_in.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    user = await service.create_user(user_in)
    return APIResponse(data=user, message="User created")

@router.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    """
    用户登录（获取访问令牌）
    
    使用邮箱和密码进行身份验证，获取JWT访问令牌。
    令牌用于后续需要认证的API请求。
    
    Args:
        form_data (OAuth2PasswordRequestForm): OAuth2密码授权表单，包含：
            - username (str): 用户邮箱（FastAPI OAuth2使用username字段表示邮箱）
            - password (str): 用户密码
        db (AsyncSession): 数据库会话依赖注入
        
    Returns:
        dict: 包含访问令牌和令牌类型
        
    Raises:
        HTTPException: 401 - 当邮箱或密码不正确时
        
    Example:
        ```bash
        curl -X POST "http://localhost:8000/api/v1/auth/token" \
          -H "Content-Type: application/x-www-form-urlencoded" \
          -d "username=user@example.com&password=Password123"
        ```
        
    Response Example:
        ```json
        {
          "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
          "token_type": "bearer"
        }
        ```
        
    Note:
        - 访问令牌有效期为30分钟
        - 需要在需要认证的请求头中添加：`Authorization: Bearer <access_token>`
        - 令牌过期后需要重新登录获取新令牌
    """
    service = UserService(db)
    user = await service.get_user_by_email(form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password"
        )
    access_token = create_access_token(data={"sub": str(user.id)})
    return {"access_token": access_token, "token_type": "bearer"}