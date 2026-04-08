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
            "SELECT * FROM users WHERE username=$1",
            data.username
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
        for ws, uid, uname in connected_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead_clients.append((ws, uid, uname))

        for dead in dead_clients:
            connected_clients.discard(dead)

    finally:
        await conn.close()


async def broadcast_system_message(text: str):
    payload = json.dumps({
        "type": "system_message",
        "text": text,
        "created_at": int(time.time())
    })

    dead_clients = []
    for ws, uid, uname in connected_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead_clients.append((ws, uid, uname))

    for dead in dead_clients:
        connected_clients.discard(dead)


def find_client_by_username(target_username: str):
    for ws, uid, uname in connected_clients:
        if uname == target_username:
            return ws, uid, uname
    return None


async def send_unread_private_counts(websocket: WebSocket, conn, current_user_id: int):
    rows = await conn.fetch("""
        SELECT sender.username, COUNT(*) AS unread_count
        FROM private_messages pm
        JOIN users sender ON pm.from_user_id = sender.id
        WHERE pm.to_user_id = $1 AND pm.is_read = FALSE
        GROUP BY sender.username
    """, current_user_id)

    counts = {row["username"]: int(row["unread_count"]) for row in rows}

    await websocket.send_text(json.dumps({
        "type": "unread_private_counts",
        "counts": counts
    }))


async def mark_private_messages_as_read(conn, current_user_id: int, from_username: str):
    sender = await conn.fetchrow(
        "SELECT id FROM users WHERE username=$1",
        from_username
    )
    if not sender:
        return

    await conn.execute("""
        UPDATE private_messages
        SET is_read = TRUE
        WHERE to_user_id = $1
          AND from_user_id = $2
          AND is_read = FALSE
    """, current_user_id, sender["id"])


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

        user = await conn.fetchrow("SELECT id FROM users WHERE id=$1", user_id)
        if not user:
            await websocket.close()
            return

        connected_clients.add((websocket, user_id, username))
        print(f"User connected: {username} ({user_id})")

        await broadcast_system_message(f"{username} вошёл в чат")

        # -------------------------
        # История общего чата
        # -------------------------
        rows = await conn.fetch("""
            SELECT messages.text, messages.created_at, users.username
            FROM messages
            JOIN users ON messages.user_id = users.id
            ORDER BY messages.id DESC
            LIMIT 1000
        """)
        for row in reversed(rows):
            await websocket.send_text(json.dumps({
                "type": "chat_message",
                "username": row["username"],
                "text": row["text"],
                "created_at": row["created_at"]
            }))

        # -------------------------
        # История личных сообщений
        # -------------------------
        private_rows = await conn.fetch("""
            SELECT
                pm.text,
                pm.created_at,
                sender.username AS from_username,
                receiver.username AS to_username
            FROM private_messages pm
            JOIN users sender ON pm.from_user_id = sender.id
            JOIN users receiver ON pm.to_user_id = receiver.id
            WHERE pm.from_user_id = $1 OR pm.to_user_id = $1
            ORDER BY pm.id ASC
        """, user_id)

        for row in private_rows:
            await websocket.send_text(json.dumps({
                "type": "private_message",
                "from_username": row["from_username"],
                "to_username": row["to_username"],
                "text": row["text"],
                "created_at": row["created_at"]
            }))

        # -------------------------
        # Непрочитанные ЛС
        # -------------------------
        await send_unread_private_counts(websocket, conn, user_id)

        await broadcast_online_status()

        # -------------------------
        # Основной цикл
        # -------------------------
        while True:
            msg_raw = await websocket.receive_text()
            data = json.loads(msg_raw)

            # -------------------------
            # Отметить ЛС как прочитанные
            # -------------------------
            if data.get("type") == "mark_private_as_read":
                from_username = data.get("from_username", "").strip()
                if from_username:
                    await mark_private_messages_as_read(conn, user_id, from_username)
                    await send_unread_private_counts(websocket, conn, user_id)
                continue

            # -------------------------
            # Ping
            # -------------------------
            if data.get("ping"):
                dead_clients = []
                for ws, uid, uname in connected_clients:
                    if uid != user_id:
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

                continue

            # -------------------------
            # Текст сообщения
            # -------------------------
            text = data.get("text", "").strip()
            if not text:
                continue

            target_username = data.get("target_username")
            created_at = int(time.time())

            # -------------------------
            # Личное сообщение
            # -------------------------
            if target_username and target_username != "Общий чат":
                target_user = await conn.fetchrow(
                    "SELECT id, username FROM users WHERE username=$1",
                    target_username
                )

                if not target_user:
                    await websocket.send_text(json.dumps({
                        "type": "system_message",
                        "text": f"Пользователь {target_username} не найден",
                        "created_at": created_at
                    }))
                    continue

                target_uid = target_user["id"]
                target_uname = target_user["username"]
                target_client = find_client_by_username(target_username)

                is_self_message = (target_uid == user_id)

                await conn.execute(
                    """
                    INSERT INTO private_messages (from_user_id, to_user_id, text, created_at, is_read)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    user_id,
                    target_uid,
                    text,
                    created_at,
                    True if is_self_message else False
                )

                private_payload = json.dumps({
                    "type": "private_message",
                    "from_username": username,
                    "to_username": target_uname,
                    "text": text,
                    "created_at": created_at
                })

                dead_clients = []

                if is_self_message:
                    try:
                        await websocket.send_text(private_payload)
                    except Exception:
                        dead_clients.append((websocket, user_id, username))
                else:
                    if target_client:
                        target_ws, _, _ = target_client
                        try:
                            await target_ws.send_text(private_payload)
                        except Exception:
                            dead_clients.append(target_client)

                    try:
                        await websocket.send_text(private_payload)
                    except Exception:
                        dead_clients.append((websocket, user_id, username))

                for dead in dead_clients:
                    connected_clients.discard(dead)

                continue

            # -------------------------
            # Обычное сообщение в общий чат
            # -------------------------
            await conn.execute(
                "INSERT INTO messages (user_id, text, created_at) VALUES ($1, $2, $3)",
                user_id,
                text,
                created_at
            )

            message_to_send = json.dumps({
                "type": "chat_message",
                "username": username,
                "text": text,
                "created_at": created_at
            })

            dead_clients = []
            for ws, uid, uname in connected_clients:
                try:
                    await ws.send_text(message_to_send)
                except Exception:
                    dead_clients.append((ws, uid, uname))

            for dead in dead_clients:
                connected_clients.discard(dead)

    except Exception as e:
        print("WebSocket error:", e)

    finally:
        if user_id and username:
            connected_clients.discard((websocket, user_id, username))
            print(f"User disconnected: {username} ({user_id})")

            await broadcast_system_message(f"{username} вышел из чата")
            await broadcast_online_status()

        await conn.close()
