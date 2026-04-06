# server.py
import os
import json
from fastapi import FastAPI, WebSocket, Request
from fastapi.middleware.cors import CORSMiddleware
import asyncio
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
# DATABASE_URL
# -------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан! Проверь переменные окружения на Railway")

# -------------------------
# Подключение к базе
# -------------------------
async def get_conn():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"[ERROR] Не удалось подключиться к базе: {e}")
        raise

# -------------------------
# Подключенные клиенты
# -------------------------
connected_clients = set()  # (websocket, user_id, username)
online_users = {}  # user_id: username

async def broadcast_online():
    """Рассылает всем клиентам список онлайн-пользователей"""
    users_list = [{"user_id": uid, "username": uname} for uid, uname in online_users.items()]
    message = json.dumps({"online": users_list})
    dead_clients = []
    for client_ws, client_uid, client_uname in connected_clients:
        try:
            await client_ws.send(message)
        except Exception:
            dead_clients.append((client_ws, client_uid, client_uname))
    for dead in dead_clients:
        connected_clients.discard(dead)

# -------------------------
# WebSocket
# -------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    conn = await get_conn()
    user_id = None
    username = None

    try:
        # Авторизация клиента
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

        # Отправляем историю сообщений после подключения
        rows = await conn.fetch(
            "SELECT users.username, messages.text, messages.created_at "
            "FROM messages JOIN users ON messages.user_id = users.id "
            "ORDER BY messages.id ASC"
        )
        for row in rows:
            await websocket.send(f"{row['username']}: {row['text']}")

        # Основной цикл
        while True:
            try:
                msg_raw = await websocket.receive_text()
                msg = json.loads(msg_raw)
                # -------------------------
                # Текстовое сообщение
                # -------------------------
                if "text" in msg:
                    await conn.execute(
                        "INSERT INTO messages(user_id, text, created_at) "
                        "VALUES($1, $2, EXTRACT(EPOCH FROM NOW())::int)",
                        user_id, msg["text"]
                    )
                    # Рассылаем всем
                    for client_ws, _, _ in connected_clients:
                        await client_ws.send(f"{username}: {msg['text']}")
                # -------------------------
                # Ping
                # -------------------------
                elif msg.get("ping"):
                    for client_ws, client_uid, _ in connected_clients:
                        if client_uid != user_id:
                            await client_ws.send(json.dumps(msg))
            except Exception as e:
                print(f"[WebSocket ERROR] {e}")

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
# HTTP: регистрация и логин
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
        # Получаем ID нового пользователя
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

        user = await conn.fetchrow("SELECT id, username FROM users WHERE username=$1 AND password=$2", username, password)
        if not user:
            return {"success": False, "message": "Неверный логин или пароль"}

        return {"success": True, "user_id": user["id"], "username": user["username"]}
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        await conn.close()
