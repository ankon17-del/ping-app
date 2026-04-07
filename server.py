from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
import os
import json
import time

app = FastAPI()

# -------------------------
# Разрешаем подключения
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
# Модели запросов
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
            "SELECT * FROM users WHERE username=$1",
            data.username
        )
        if existing:
            return {"success": False, "message": "Username already exists"}
        await conn.execute(
            "INSERT INTO users (username, password) VALUES ($1, $2)",
            data.username,
            data.password
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
            data.password
        )
        if user:
            return {
                "success": True,
                "user_id": user["id"],
                "username": user["username"]
            }
        else:
            return {"success": False, "message": "Invalid username or password"}
    except Exception as e:
        print("LOGIN ERROR:", e)
        return {"success": False, "message": str(e)}
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
        # Авторизация
        # -------------------------
        auth_raw = await websocket.receive_text()
        try:
            auth = json.loads(auth_raw)
        except Exception:
            await websocket.close()
            return

        user_id = auth.get("user_id")
        username = auth.get("username")
        if not user_id or not username:
            await websocket.close()
            return

        # Проверяем, что пользователь реально существует
        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            await websocket.close()
            return

        connected_clients.add((websocket, user_id, username))
        print(f"User connected: {username} ({user_id})")

        # -------------------------
        # Отправляем историю сообщений
        # -------------------------
        rows = await conn.fetch("""
            SELECT messages.text, users.username
            FROM messages
            JOIN users ON messages.user_id = users.id
            ORDER BY messages.id ASC
            LIMIT 100
        """)
        for row in rows:
            await websocket.send_text(json.dumps({
                "type": "message",
                "username": row["username"],
                "text": row["text"]
            }))

        # -------------------------
        # Рассылаем обновлённый список онлайн всем
        # -------------------------
        def broadcast_online():
            online_list = [u for _, _, u in connected_clients]
            online_msg = json.dumps({"type": "online", "users": online_list})
            dead = []
            for client_ws, _, _ in connected_clients:
                try:
                    asyncio.create_task(client_ws.send_text(online_msg))
                except:
                    dead.append((client_ws, _, _))
            for d in dead:
                connected_clients.discard(d)

        broadcast_online()

        # -------------------------
        # Основной цикл
        # -------------------------
        while True:
            msg_raw = await websocket.receive_text()
            try:
                data = json.loads(msg_raw)
                if not isinstance(data, dict):
                    continue
            except Exception:
                continue

            # -------------------------
            # Сообщение чата
            # -------------------------
            if data.get("type") == "message":
                text = data.get("text", "").strip()
                if not text:
                    continue

                # Сохраняем в БД
                await conn.execute(
                    "INSERT INTO messages (user_id, text, created_at) VALUES ($1, $2, $3)",
                    user_id, text, int(time.time())
                )

                # Формируем JSON для всех клиентов
                message_to_send = json.dumps({
                    "type": "message",
                    "user_id": user_id,
                    "username": username,
                    "text": text
                })

                dead_clients = []
                for client_ws, _, _ in connected_clients:
                    try:
                        await client_ws.send_text(message_to_send)
                    except:
                        dead_clients.append((client_ws, _, _))
                for dead in dead_clients:
                    connected_clients.discard(dead)

            # -------------------------
            # Ping
            # -------------------------
            elif data.get("type") == "ping":
                ping_msg = json.dumps({
                    "type": "ping",
                    "user_id": user_id,
                    "username": username
                })
                dead_clients = []
                for client_ws, client_user_id, _ in connected_clients:
                    if client_user_id == user_id:
                        continue
                    try:
                        await client_ws.send_text(ping_msg)
                    except:
                        dead_clients.append((client_ws, client_user_id, _))
                for dead in dead_clients:
                    connected_clients.discard(dead)

            # -------------------------
            # Онлайн-пользователи (по запросу)
            # -------------------------
            elif data.get("type") == "online_request":
                online_list = [u for _, _, u in connected_clients]
                await websocket.send_text(json.dumps({"type": "online", "users": online_list}))

    except Exception as e:
        print("WebSocket error:", e)

    finally:
        if user_id and username:
            connected_clients.discard((websocket, user_id, username))
            print(f"User disconnected: {username} ({user_id})")
            broadcast_online()
        await conn.close()
