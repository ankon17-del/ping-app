# server.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
import asyncpg
import os
from datetime import datetime
import json

app = FastAPI()

# -------------------------
# Подключение к базе PostgreSQL
# -------------------------
DATABASE_URL = os.getenv("DATABASE_URL")

conn = None

async def get_conn():
    global conn
    if conn is None:
        conn = await asyncpg.connect(DATABASE_URL)
    return conn

# -------------------------
# HTTP endpoints
# -------------------------
@app.post("/register")
async def register_user(req: Request):
    data = await req.json()
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return JSONResponse({"error": "username or password missing"}, status_code=400)

    conn = await get_conn()

    existing = await conn.fetchrow(
        "SELECT id FROM users WHERE username=$1",
        username
    )
    if existing:
        return JSONResponse({"error": "username already exists"}, status_code=400)

    row = await conn.fetchrow(
        "INSERT INTO users(username, password) VALUES($1, $2) RETURNING id",
        username, password
    )

    return {"id": row["id"]}


@app.post("/login")
async def login_user(req: Request):
    data = await req.json()
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return JSONResponse({"error": "username or password missing"}, status_code=400)

    conn = await get_conn()

    row = await conn.fetchrow(
        "SELECT id, password FROM users WHERE username=$1",
        username
    )

    if not row or row["password"] != password:
        return JSONResponse({"error": "invalid credentials"}, status_code=400)

    return {"id": row["id"]}


# -------------------------
# WebSocket
# -------------------------
connected_clients = set()  # {(websocket, user_id, username)}

async def send_history(ws):
    conn = await get_conn()
    rows = await conn.fetch("""
        SELECT messages.text, users.username, messages.created_at
        FROM messages
        JOIN users ON messages.user_id = users.id
        ORDER BY messages.id ASC
    """)

    for r in rows:
        msg_text = f"{r['username']}: {r['text']}"
        await ws.send_text(msg_text)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    conn = await get_conn()

    user_id = None
    username = None

    try:
        # -------------------------
        # Авторизация клиента
        # -------------------------
        auth_raw = await websocket.receive_text()
        auth = json.loads(auth_raw)

        user_id = auth.get("user_id")
        username = auth.get("username")

        if not user_id or not username:
            await websocket.close()
            return

        # Проверим, что такой пользователь реально есть
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE id=$1",
            user_id
        )
        if not user:
            await websocket.close()
            return

        connected_clients.add((websocket, user_id, username))
        print(f"User connected: {username} ({user_id})")

        # -------------------------
        # Отправляем историю сообщений
        # -------------------------
        await send_history(websocket)

        # -------------------------
        # Основной цикл сообщений
        # -------------------------
        while True:
            msg_raw = await websocket.receive_text()

            try:
                data = json.loads(msg_raw)
            except:
                continue

            # -------------------------
            # Ping
            # -------------------------
            if data.get("ping"):
                for ws, uid, uname in connected_clients:
                    if ws != websocket:
                        await ws.send_text(json.dumps({
                            "ping": True,
                            "user_id": user_id
                        }))
                continue

            # -------------------------
            # Обычное сообщение
            # -------------------------
            text = data.get("text")
            if not text:
                continue

            created_at = int(datetime.utcnow().timestamp())

            # Сохраняем в БД
            await conn.execute(
                "INSERT INTO messages(user_id, text, created_at) VALUES($1, $2, $3)",
                user_id, text, created_at
            )

            # Рассылаем всем
            full_msg = f"{username}: {text}"
            for ws, uid, uname in connected_clients:
                await ws.send_text(full_msg)

    except WebSocketDisconnect:
        print(f"User disconnected: {username}")

    except Exception as e:
        print("WebSocket error:", e)

    finally:
        # Удаляем клиента из списка
        to_remove = None
        for client in connected_clients:
            if client[0] == websocket:
                to_remove = client
                break

        if to_remove:
            connected_clients.remove(to_remove)

        # Оповещаем остальных
        if username:
            for ws, uid, uname in connected_clients:
                try:
                    await ws.send_text(f"*** {username} отключился ***")
                except:
                    pass
