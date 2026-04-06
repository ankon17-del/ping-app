# server.py
import os
import json
import asyncio
from fastapi import FastAPI, WebSocket, Request
from fastapi.middleware.cors import CORSMiddleware
import asyncpg

app = FastAPI()

# -------------------------
# CORS
# -------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан!")

# -------------------------
# Подключение к базе
async def get_conn():
    return await asyncpg.connect(DATABASE_URL)

# -------------------------
connected_clients = set()  # (websocket, user_id, username)
online_users = {}          # user_id -> username

async def broadcast_online():
    users_list = [{"user_id": uid, "username": uname} for uid, uname in online_users.items()]
    message = json.dumps({"online": users_list})
    dead_clients = []
    for ws, _, _ in connected_clients:
        try:
            await ws.send(message)
        except:
            dead_clients.append((ws, _, _))
    for dead in dead_clients:
        connected_clients.discard(dead)

# -------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    conn = await get_conn()
    user_id = None
    username = None

    try:
        auth_raw = await websocket.receive_text()
        auth = json.loads(auth_raw)
        user_id = auth.get("user_id")
        username = auth.get("username")

        if not user_id or not username:
            await websocket.close()
            return

        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            await websocket.close()
            return

        connected_clients.add((websocket, user_id, username))
        online_users[user_id] = username
        await broadcast_online()
        print(f"User connected: {username} ({user_id})")

        # -------------------------
        # Отправка истории сообщений
        rows = await conn.fetch(
            "SELECT users.username, messages.text FROM messages "
            "JOIN users ON messages.user_id = users.id ORDER BY messages.id ASC"
        )
        for row in rows:
            await websocket.send(json.dumps({
                "history": True,
                "username": row["username"],
                "text": row["text"]
            }))

        # -------------------------
        # Основной цикл
        while True:
            msg_raw = await websocket.receive_text()
            try:
                msg = json.loads(msg_raw)
            except:
                continue  # если не JSON, пропускаем

            # текстовое сообщение
            if "text" in msg:
                await conn.execute(
                    "INSERT INTO messages(user_id, text, created_at) VALUES($1, $2, EXTRACT(EPOCH FROM NOW())::int)",
                    user_id, msg["text"]
                )
                for ws, _, _ in connected_clients:
                    await ws.send(json.dumps({
                        "username": username,
                        "text": msg["text"]
                    }))

            # Ping
            elif msg.get("ping"):
                for ws, uid, _ in connected_clients:
                    if uid != user_id:
                        await ws.send(json.dumps(msg))

    except Exception as e:
        print(f"[WebSocket ERROR] {e}")
    finally:
        if (websocket, user_id, username) in connected_clients:
            connected_clients.discard((websocket, user_id, username))
        if user_id in online_users:
            online_users.pop(user_id)
        await broadcast_online()
        await conn.close()
        print(f"User disconnected: {username} ({user_id})")

# -------------------------
@app.post("/register")
async def register(request: Request):
    conn = await get_conn()
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return {"success": False, "message": "Имя и пароль обязательны"}

        await conn.execute("INSERT INTO users(username, password) VALUES($1, $2)", username, password)
        user = await conn.fetchrow("SELECT id FROM users WHERE username=$1", username)
        return {"success": True, "user_id": user["id"]}
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        await conn.close()

@app.post("/login")
async def login(request: Request):
    conn = await get_conn()
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return {"success": False, "message": "Имя и пароль обязательны"}

        user = await conn.fetchrow(
            "SELECT id, username FROM users WHERE username=$1 AND password=$2",
            username, password
        )
        if not user:
            return {"success": False, "message": "Неверный логин или пароль"}

        return {"success": True, "user_id": user["id"], "username": user["username"]}
    finally:
        await conn.close()
