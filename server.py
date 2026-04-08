from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
import os
import json
import time
import bcrypt

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


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def looks_like_bcrypt_hash(value: str) -> bool:
    if not isinstance(value, str):
        return False
    return value.startswith("$2a$") or value.startswith("$2b$") or value.startswith("$2y$")


def verify_password(password: str, stored_password: str) -> bool:
    if not stored_password:
        return False

    if looks_like_bcrypt_hash(stored_password):
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                stored_password.encode("utf-8")
            )
        except Exception:
            return False

    return password == stored_password



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
        username = data.username.strip()
        password = data.password

        if not username or not password:
            return {"success": False, "message": "Username and password are required"}

        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE username=$1",
            username
        )
        if existing:
            return {"success": False, "message": "Username already exists"}

        password_hash = hash_password(password)

        await conn.execute(
            "INSERT INTO users (username, password) VALUES ($1, $2)",
            username,
            password_hash,
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
        username = data.username.strip()
        password = data.password

        if not username or not password:
            return {"success": False, "message": "Invalid username or password"}

        user = await conn.fetchrow(
            "SELECT * FROM users WHERE username=$1",
            username,
        )

        if not user:
            return {"success": False, "message": "Invalid username or password"}

        stored_password = user["password"]

        if not verify_password(password, stored_password):
            return {"success": False, "message": "Invalid username or password"}

        if not looks_like_bcrypt_hash(stored_password):
            try:
                upgraded_hash = hash_password(password)
                await conn.execute(
                    "UPDATE users SET password=$1 WHERE id=$2",
                    upgraded_hash,
                    user["id"]
                )
            except Exception as migrate_error:
                print("PASSWORD MIGRATION ERROR:", migrate_error)

        return {
            "success": True,
            "user_id": user["id"],
            "username": user["username"],
        }

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


async def clear_private_dialog(conn, current_user_id: int, peer_username: str):
    peer = await conn.fetchrow(
        "SELECT id, username FROM users WHERE username=$1",
        peer_username
    )
    if not peer:
        return False

    peer_id = peer["id"]

    if peer_id == current_user_id:
        await conn.execute("""
            DELETE FROM private_messages
            WHERE from_user_id = $1 AND to_user_id = $1
        """, current_user_id)
        return True

    await conn.execute("""
        DELETE FROM private_messages
        WHERE (from_user_id = $1 AND to_user_id = $2)
           OR (from_user_id = $2 AND to_user_id = $1)
    """, current_user_id, peer_id)
    return True


async def delete_favorites_message(conn, current_user_id: int, message_id: int):
    result = await conn.execute("""
        DELETE FROM private_messages
        WHERE id = $1
          AND from_user_id = $2
          AND to_user_id = $2
    """, message_id, current_user_id)
    return result.endswith("1")


async def delete_private_message(conn, current_user_id: int, peer_username: str, message_id: int):
    peer = await conn.fetchrow(
        "SELECT id FROM users WHERE username=$1",
        peer_username
    )
    if not peer:
        return False

    peer_id = peer["id"]

    result = await conn.execute("""
        DELETE FROM private_messages
        WHERE id = $1
          AND from_user_id = $2
          AND to_user_id = $3
    """, message_id, current_user_id, peer_id)

    return result.endswith("1")


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

        private_rows = await conn.fetch("""
            SELECT
                pm.id,
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
                "message_id": row["id"],
                "from_username": row["from_username"],
                "to_username": row["to_username"],
                "text": row["text"],
                "created_at": row["created_at"]
            }))

        await send_unread_private_counts(websocket, conn, user_id)
        await broadcast_online_status()

        while True:
            msg_raw = await websocket.receive_text()
            data = json.loads(msg_raw)

            if data.get("type") == "delete_favorites_message":
                message_id = data.get("message_id")
                success = False
                if message_id is not None:
                    try:
                        success = await delete_favorites_message(conn, user_id, int(message_id))
                    except Exception:
                        success = False

                await websocket.send_text(json.dumps({
                    "type": "favorites_message_deleted",
                    "message_id": message_id,
                    "success": success
                }))
                continue

            if data.get("type") == "delete_private_message":
                message_id = data.get("message_id")
                peer_username = data.get("peer_username", "").strip()
                success = False

                if message_id is not None and peer_username:
                    try:
                        success = await delete_private_message(
                            conn,
                            user_id,
                            peer_username,
                            int(message_id)
                        )
                    except Exception:
                        success = False

                delete_payload = json.dumps({
                    "type": "private_message_deleted",
                    "message_id": message_id,
                    "peer_username": peer_username,
                    "success": success
                })

                await websocket.send_text(delete_payload)

                if success:
                    target_client = find_client_by_username(peer_username)
                    if target_client:
                        target_ws, target_uid, target_uname = target_client
                        try:
                            await target_ws.send_text(delete_payload)
                        except Exception:
                            connected_clients.discard((target_ws, target_uid, target_uname))

                continue

            if data.get("type") == "clear_private_dialog":
                peer_username = data.get("peer_username", "").strip()
                if peer_username:
                    success = await clear_private_dialog(conn, user_id, peer_username)

                    requester_payload = json.dumps({
                        "type": "private_dialog_cleared",
                        "peer_username": peer_username,
                        "success": success
                    })
                    await websocket.send_text(requester_payload)
                    await send_unread_private_counts(websocket, conn, user_id)

                    if success and peer_username != username:
                        target_client = find_client_by_username(peer_username)
                        if target_client:
                            target_ws, target_uid, target_uname = target_client
                            target_payload = json.dumps({
                                "type": "private_dialog_cleared",
                                "peer_username": username,
                                "success": True
                            })
                            try:
                                await target_ws.send_text(target_payload)
                                await send_unread_private_counts(target_ws, conn, target_uid)
                            except Exception:
                                connected_clients.discard((target_ws, target_uid, target_uname))
                continue

            if data.get("type") == "mark_private_as_read":
                from_username = data.get("from_username", "").strip()
                if from_username:
                    await mark_private_messages_as_read(conn, user_id, from_username)
                    await send_unread_private_counts(websocket, conn, user_id)
                continue

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

            text = data.get("text", "").strip()
            if not text:
                continue

            target_username = data.get("target_username")
            created_at = int(time.time())

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

                inserted = await conn.fetchrow(
                    """
                    INSERT INTO private_messages (from_user_id, to_user_id, text, created_at, is_read)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    user_id,
                    target_uid,
                    text,
                    created_at,
                    True if is_self_message else False
                )
                message_id = inserted["id"]

                private_payload = json.dumps({
                    "type": "private_message",
                    "message_id": message_id,
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
