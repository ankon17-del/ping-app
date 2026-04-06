from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse
import asyncio
import asyncpg
import os
import json
import time

app = FastAPI()

DATABASE_URL = os.environ.get("DATABASE_URL")  # Переменная на Railway

# --------------------------
# Подключение к PostgreSQL
# --------------------------
async def get_db_pool():
    if not hasattr(app.state, "db_pool"):
        app.state.db_pool = await asyncpg.create_pool(DATABASE_URL)
    return app.state.db_pool

# --------------------------
# Пользователи
# --------------------------
@app.post("/register")
async def register_user(data: dict):
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Проверяем, есть ли уже пользователь
        existing = await conn.fetchrow("SELECT * FROM users WHERE username=$1", username)
        if existing:
            raise HTTPException(status_code=400, detail="User already exists")
        # Создаём пользователя
        row = await conn.fetchrow(
            "INSERT INTO users(username, password) VALUES($1,$2) RETURNING id",
            username, password
        )
        return {"user_id": str(row["id"])}

@app.post("/login")
async def login_user(data: dict):
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE username=$1 AND password=$2", username, password)
        if not user:
            raise HTTPException(status_code=400, detail="Invalid credentials")
        return {"user_id": str(user["id"])}

# --------------------------
# WebSocket
# --------------------------
connected_clients = set()  # {(websocket, username, user_id)}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        auth_raw = await websocket.receive_text()
        auth = json.loads(auth_raw)
        username = auth.get("username")
        user_id = auth.get("user_id")
        if not username or not user_id:
            await websocket.close()
            return

        # Проверяем, что user_id существует в БД
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            user = await conn.fetchrow("SELECT * FROM users WHERE id=$1", int(user_id))
            if not user:
                await websocket.close()
                return

        connected_clients.add((websocket, username, user_id))
        print(f"User connected: {username} ({user_id})")

        # Отправляем историю сообщений
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT m.text, u.username FROM messages m JOIN users u ON m.user_id = u.id ORDER BY m.created_at ASC"
            )
            for row in rows:
                await websocket.send_text(json.dumps({"type": "message", "user": row["username"], "text": row["text"]}))

        # Основной цикл
        while True:
            msg = await websocket.receive_text()
            if msg.startswith("__PING__"):
                # Отправляем Ping всем остальным
                for ws, uname, uid in connected_clients:
                    if ws != websocket:
                        await ws.send_text(json.dumps({"type": "ping", "user": username}))
                continue

            # Сохраняем сообщение в БД
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO messages(user_id, text, created_at) VALUES($1, $2, $3)",
                    int(user_id), msg, int(time.time())
                )

            # Отправляем всем подключённым
            for ws, uname, uid in connected_clients:
                await ws.send_text(json.dumps({"type": "message", "user": username, "text": msg}))

    except WebSocketDisconnect:
        connected_clients.remove((websocket, username, user_id))
        print(f"User disconnected: {username}")
        for ws, uname, uid in connected_clients:
            await ws.send_text(json.dumps({"type": "system", "text": f"{username} disconnected"}))
