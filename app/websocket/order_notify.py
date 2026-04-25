from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Dict

router = APIRouter()

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: str):
        self.active_connections.pop(user_id, None)

    async def send_order_status(self, user_id: str, order_id: str, status: str):
        if user_id in self.active_connections:
            await self.active_connections[user_id].send_json({
                "event": "order_status",
                "order_id": order_id,
                "status": status
            })

manager = ConnectionManager()

@router.websocket("/ws/orders/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(websocket, user_id)
    try:
        while True:
            data = await websocket.receive_text()
            # 可处理客户端心跳
    except WebSocketDisconnect:
        manager.disconnect(user_id)