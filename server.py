# server.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
import asyncio
import asyncpg
import os
from datetime import datetime
import json   # <- вот это добавляем

app = FastAPI()

# -------------------------
# Подключение к базе PostgreSQL
# -------------------------
DATABASE_URL = os.getenv("DATABASE_URL")  # переменная на Railway

conn = None  # глобальная переменная для соединения

async def get_conn():
    global conn
    if conn is None:
        conn = await asyncpg.connect(DATABASE_URL)
    return conn

# -------------------------
# Таблицы
# -------------------------
# users(id serial primary key, username text unique, password text)
# messages(id serial primary key, user_id integer, text text, created_at integer)

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
    # Проверяем, нет ли уже пользователя
    exists = await conn.fetchrow("SELECT id FROM users WHERE username=$1", username)
    if exists:
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
    row = await conn.fetchrow("SELECT id, password FROM users WHERE username=$1", username)
    if not row or row["password"] != password:
        return JSONResponse({"error": "invalid credentials"}, status_code=400)

    return {"id": row["id"]}


# -------------------------
# WebSocket
# -------------------------
connected_clients = set()  # {(websocket, user_id, username)}

async def send_history(ws, user_id):
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
    try:
        # Ждем идентификацию от клиента
        auth_raw = await websocket.receive_text()
        auth = json.loads(auth_raw)
        user_id = auth.get("user_id")
        username = auth.get("username")
        if not user_id or not username:
            await websocket.close()
            return

        connected_clients.add((websocket, user_id, username))

        # Отправляем историю сообщений
        await send_history(websocket, user_id)

        while True:
            msg_raw = await websocket.recv()
            try:
                data = json.loads(msg_raw)
            except:
                continue

            # Обработка Ping
            if data.get("ping"):
                for ws, uid, uname in connected_clients:
                    if ws != websocket:
                        await ws.send_text(json.dumps({"ping": True, "user_id": user_id}))
                continue

            # Обычное сообщение
            text = data.get("text")
            if not text:
                continue

            # Сохраняем в базе
            created_at = int(datetime.utcnow().timestamp())
            await conn.execute(
                "INSERT INTO messages(user_id, text, created_at) VALUES($1,$2,$3)",
                user_id, text, created_at
            )

            # Отправляем всем подключенным клиентам
            full_msg = f"{username}: {text}"
            for ws, uid, uname in connected_clients:
                await ws.send_text(full_msg)

    except WebSocketDisconnect:
        connected_clients.remove((websocket, user_id, username))
        # Оповещаем всех об отключении
        for ws, uid, uname in connected_clients:
            await ws.send_text(f"*** {username} отключился ***")
