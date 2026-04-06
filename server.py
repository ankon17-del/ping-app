import os
import json
import uuid
import hashlib
import time
import asyncio
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# --------------------------
# CORS
# --------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------
# Подключение к PostgreSQL
# --------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не задана в переменных окружения")

conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
conn.autocommit = True

# --------------------------
# Вспомогательные функции
# --------------------------
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

async def broadcast(message: dict, exclude=set()):
    remove = []
    for ws, _ in connected_clients:
        if ws in exclude:
            continue
        try:
            await ws.send_text(json.dumps(message))
        except:
            remove.append(ws)
    for ws in remove:
        for c in connected_clients.copy():
            if c[0] == ws:
                connected_clients.remove(c)

# --------------------------
# Регистрация / Логин
# --------------------------
@app.post("/register")
async def register(data: dict):
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        raise HTTPException(400, "Username и password обязательны")

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            raise HTTPException(400, "Username уже существует")
        user_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO users (user_id, username, password) VALUES (%s,%s,%s)",
            (user_id, username, hash_password(password)),
        )
    return {"status": "ok", "user_id": user_id}

@app.post("/login")
async def login(data: dict):
    username = data.get("username")
    password = data.get("password")
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        if not row or row["password"] != hash_password(password):
            raise HTTPException(400, "Неверный логин или пароль")
    return {"status": "ok", "user_id": row["user_id"]}

# --------------------------
# WebSocket
# --------------------------
connected_clients = set()  # {(ws, username)}
MAX_HISTORY = 100

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        auth_data = await websocket.receive_text()
        auth = json.loads(auth_data)
        username = auth.get("username")
        user_id = auth.get("user_id")
        if not username or not user_id:
            await websocket.close()
            return

        connected_clients.add((websocket, username))

        # Отправка последних 100 сообщений из базы
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, text, type, created_at FROM messages ORDER BY created_at DESC LIMIT %s",
                (MAX_HISTORY,),
            )
            messages = cur.fetchall()
            for msg in reversed(messages):
                await websocket.send_text(json.dumps(msg))

        # Системное уведомление
        system_msg = {"type": "system", "text": f"{username} подключился", "user": username}
        await broadcast(system_msg, exclude={websocket})

        while True:
            raw_msg = await websocket.receive_text()
            if raw_msg.startswith("__PING__"):
                ping_msg = {"type": "ping", "user": username, "text": "Ping!"}
                await broadcast(ping_msg, exclude={websocket})
                continue

            created_at = int(time.time())
            chat_msg = {"type": "message", "user": username, "text": raw_msg, "created_at": created_at}
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (user_id, username, text, type, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (user_id, username, raw_msg, "message", created_at),
                )
            await broadcast(chat_msg)

    except WebSocketDisconnect:
        connected_clients.remove((websocket, username))
        disc_msg = {"type": "system", "text": f"{username} отключился", "user": username}
        await broadcast(disc_msg)
