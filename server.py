from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
import os
import json
import time

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

DATABASE_URL = os.getenv("DATABASE_URL")
connected_clients = set()  # (websocket, user_id, username)


# -------------------------
# Модели
# -------------------------
class RegisterData(BaseModel):
    username: str
    password: str


class LoginData(BaseModel):
    username: str
    password: str


# -------------------------
# Подключение к БД
# -------------------------
async def get_conn():
    return await asyncpg.connect(DATABASE_URL)


# -------------------------
# Проверка сервера
# -------------------------
@app.get("/")
async def root():
    return {"status": "Server is running"}


# -------------------------
# Регистрация
# -------------------------
@app.post("/register")
async def register(data: RegisterData):
    conn = await get_conn()
    try:
        existing = await conn.fetchrow(
            "SELECT * FROM users WHERE username=$1", data.username
        )
        if existing:
            return {"success": False, "message": "Username already exists"}

        await conn.execute(
            "INSERT INTO users (username, password) VALUES ($1, $2)",
            data.username,
            data.password,
        )
        return {"success": True, "message": "Registered successfully"}

    except Exception as e:
        print("REGISTER ERROR:", e)
        return {"success": False, "message": str(e)}

    finally:
        await conn.close()


# -------------------------
# Логин
# -------------------------
@app.post("/login")
async def login(data: LoginData):
    conn = await get_conn()
    try:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE username=$1 AND password=$2",
            data.username,
            data.password,
        )
        if user:
            return {
                "success": True,
                "user_id": user["id"],
                "username": user["username"],
            }
        else:
            return {"success": False, "message": "Invalid username or password"}

    except Exception as e:
        print("LOGIN ERROR:", e)
        return {"success": False, "message": str(e)}

    finally:
        await conn.close()


# -------------------------
# Вспомогательные функции
# -------------------------
async def broadcast_online_status():
    """Отправляем всем клиентам обновленные списки онлайн/офлайн пользователей"""
    conn = await get_conn()
    try:
        rows = await conn.fetch("SELECT id, username FROM users ORDER BY username ASC")
        all_users = [{"user_id": r["id"], "username": r["username"]} for r in rows]

        online_ids = [uid for _, uid, _ in connected_clients]
        offline_users = [u for u in all_users if u["user_id"] not in online_ids]
        online_users = [u for u in all_users if u["user_id"] in online_ids]

        payload = json.dumps({
            "type": "online_status",
            "online": online_users,
            "offline": offline_users
        })

        dead_clients = []
        for ws, _, _ in connected_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead_clients.append((ws, _, _))
        for dead in dead_clients:
            connected_clients.discard(dead)
    finally:
        await conn.close()


# -------------------------
# WebSocket чат
# -------------------------
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

        # Проверяем пользователя в БД
        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            await websocket.close()
            return

        connected_clients.add((websocket, user_id, username))
        print(f"User connected: {username} ({user_id})")

        # -------------------------
        # Отправка истории сообщений
        # -------------------------
        rows = await conn.fetch("""
            SELECT messages.text, users.username
            FROM messages
            JOIN users ON messages.user_id = users.id
            ORDER BY messages.id ASC
            LIMIT 100
        """)
        for row in rows:
            await websocket.send_text(f"{row['username']}: {row['text']}")

        # -------------------------
        # Обновление онлайн/офлайн всех клиентов
        # -------------------------
        await broadcast_online_status()

        # -------------------------
        # Основной цикл
        # -------------------------
        while True:
            msg_raw = await websocket.receive_text()
            data = json.loads(msg_raw)

            # -------------------------
            # Ping
            # -------------------------
            if data.get("ping"):
                dead_clients = []
                for ws, uid, uname in connected_clients:
                    if uid != user_id:  # отправитель не получает
                        try:
                            await ws.send_text(json.dumps({
                                "type": "ping",
                                "user_id": user_id,
                                "username": username
                            }))
                        except Exception:
                            dead_clients.append((ws, uid, uname))
                for dead in dead_clients:
                    connected_clients.discard(dead)
                continue  # не добавляем в чат отправителю

            # -------------------------
            # Обычное сообщение
            # -------------------------
            text = data.get("text", "").strip()
            if not text:
                continue

            await conn.execute(
                "INSERT INTO messages (user_id, text, created_at) VALUES ($1, $2, $3)",
                user_id,
                text,
                int(time.time())
            )

            message_to_send = f"{username}: {text}"

            dead_clients = []
            for ws, _, _ in connected_clients:
                try:
                    await ws.send_text(message_to_send)
                except Exception:
                    dead_clients.append((ws, _, _))
            for dead in dead_clients:
                connected_clients.discard(dead)

    except Exception as e:
        print("WebSocket error:", e)

    finally:
        if user_id and username:
            connected_clients.discard((websocket, user_id, username))
            print(f"User disconnected: {username} ({user_id})")
            await broadcast_online_status()  # обновляем статус при выходе
        await conn.close()
