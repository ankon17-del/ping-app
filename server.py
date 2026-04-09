
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg
import base64
import hashlib
import hmac
import json
import os
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

PASSWORD_HASH_PREFIX = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 260000
PASSWORD_SALT_BYTES = 16


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
# Хеширование паролей
# -------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(PASSWORD_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    dk_b64 = base64.b64encode(dk).decode("ascii")
    return f"{PASSWORD_HASH_PREFIX}${PASSWORD_HASH_ITERATIONS}${salt_b64}${dk_b64}"


def is_password_hash(value: str | None) -> bool:
    if not value or not isinstance(value, str):
        return False
    return value.startswith(f"{PASSWORD_HASH_PREFIX}$")


def verify_password(password: str, stored_value: str | None) -> bool:
    if not stored_value:
        return False

    if not is_password_hash(stored_value):
        return hmac.compare_digest(stored_value, password)

    try:
        _, iterations_str, salt_b64, dk_b64 = stored_value.split("$", 3)
        iterations = int(iterations_str)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected_dk = base64.b64decode(dk_b64.encode("ascii"))
    except Exception:
        return False

    actual_dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual_dk, expected_dk)


# -------------------------
# Схема БД
# -------------------------
async def ensure_schema():
    conn = await get_conn()
    try:
        await conn.execute("""
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS edited_at BIGINT
        """)
        await conn.execute("""
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT
        """)
        await conn.execute("""
            ALTER TABLE private_messages
            ADD COLUMN IF NOT EXISTS edited_at BIGINT
        """)
        await conn.execute("""
            ALTER TABLE private_messages
            ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT
        """)

        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'messages_reply_to_message_id_fkey'
                ) THEN
                    ALTER TABLE messages
                    ADD CONSTRAINT messages_reply_to_message_id_fkey
                    FOREIGN KEY (reply_to_message_id)
                    REFERENCES messages(id)
                    ON DELETE SET NULL;
                END IF;
            END
            $$;
        """)

        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'private_messages_reply_to_message_id_fkey'
                ) THEN
                    ALTER TABLE private_messages
                    ADD CONSTRAINT private_messages_reply_to_message_id_fkey
                    FOREIGN KEY (reply_to_message_id)
                    REFERENCES private_messages(id)
                    ON DELETE SET NULL;
                END IF;
            END
            $$;
        """)
    finally:
        await conn.close()


@app.on_event("startup")
async def startup_event():
    await ensure_schema()


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
        username_value = data.username.strip()
        password_value = data.password

        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE username=$1",
            username_value
        )
        if existing:
            return {"success": False, "message": "Username already exists"}

        hashed_password = hash_password(password_value)

        await conn.execute(
            "INSERT INTO users (username, password) VALUES ($1, $2)",
            username_value,
            hashed_password,
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
        username_value = data.username.strip()
        password_value = data.password

        user = await conn.fetchrow(
            "SELECT id, username, password FROM users WHERE username=$1",
            username_value,
        )

        if not user:
            return {"success": False, "message": "Invalid username or password"}

        stored_password = user["password"]

        if verify_password(password_value, stored_password):
            if is_username_online(user["username"]):
                return {"success": False, "message": "Пользователь уже авторизован"}

            if not is_password_hash(stored_password):
                new_hash = hash_password(password_value)
                await conn.execute(
                    "UPDATE users SET password=$1 WHERE id=$2",
                    new_hash,
                    user["id"]
                )

            return {
                "success": True,
                "user_id": user["id"],
                "username": user["username"],
            }

        return {"success": False, "message": "Invalid username or password"}

    except Exception as e:
        print("LOGIN ERROR:", e)
        return {"success": False, "message": str(e)}

    finally:
        await conn.close()


# -------------------------
# Вспомогательные функции
# -------------------------
def build_reply_preview(reply_username, reply_text, reply_created_at):
    if not reply_text:
        return None

    preview_text = str(reply_text).replace("\n", " ").strip()
    if len(preview_text) > 120:
        preview_text = preview_text[:117] + "..."

    return {
        "username": reply_username or "",
        "text": preview_text,
        "created_at": reply_created_at,
    }


def build_chat_message_payload(
    *,
    message_id,
    username,
    text,
    created_at,
    edited_at=None,
    reply_to_message_id=None,
    reply_preview=None,
):
    return {
        "type": "chat_message",
        "message_id": message_id,
        "username": username,
        "text": text,
        "created_at": created_at,
        "edited_at": edited_at,
        "reply_to_message_id": reply_to_message_id,
        "reply_preview": reply_preview,
    }


def build_private_message_payload(
    *,
    message_id,
    from_username,
    to_username,
    text,
    created_at,
    edited_at=None,
    reply_to_message_id=None,
    reply_preview=None,
):
    return {
        "type": "private_message",
        "message_id": message_id,
        "from_username": from_username,
        "to_username": to_username,
        "text": text,
        "created_at": created_at,
        "edited_at": edited_at,
        "reply_to_message_id": reply_to_message_id,
        "reply_preview": reply_preview,
    }


def find_client_by_username(target_username: str):
    for ws, uid, uname in connected_clients:
        if uname == target_username:
            return ws, uid, uname
    return None


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


async def get_public_reply_row(conn, reply_to_message_id: int):
    return await conn.fetchrow("""
        SELECT
            m.id,
            m.text,
            m.created_at,
            u.username
        FROM messages m
        JOIN users u ON m.user_id = u.id
        WHERE m.id = $1
    """, reply_to_message_id)


async def get_private_reply_row(conn, current_user_id: int, target_user_id: int, reply_to_message_id: int):
    return await conn.fetchrow("""
        SELECT
            pm.id,
            pm.text,
            pm.created_at,
            sender.username AS from_username,
            pm.from_user_id,
            pm.to_user_id
        FROM private_messages pm
        JOIN users sender ON pm.from_user_id = sender.id
        WHERE pm.id = $1
          AND (
                (pm.from_user_id = $2 AND pm.to_user_id = $3)
             OR (pm.from_user_id = $3 AND pm.to_user_id = $2)
          )
    """, reply_to_message_id, current_user_id, target_user_id)


async def send_public_history(websocket: WebSocket, conn):
    rows = await conn.fetch("""
        SELECT
            m.id,
            m.text,
            m.created_at,
            m.edited_at,
            m.reply_to_message_id,
            u.username,
            rm.text AS reply_text,
            rm.created_at AS reply_created_at,
            ru.username AS reply_username
        FROM messages m
        JOIN users u ON m.user_id = u.id
        LEFT JOIN messages rm ON m.reply_to_message_id = rm.id
        LEFT JOIN users ru ON rm.user_id = ru.id
        ORDER BY m.id DESC
        LIMIT 1000
    """)

    for row in reversed(rows):
        reply_preview = build_reply_preview(
            row["reply_username"],
            row["reply_text"],
            row["reply_created_at"],
        )
        payload = build_chat_message_payload(
            message_id=row["id"],
            username=row["username"],
            text=row["text"],
            created_at=row["created_at"],
            edited_at=row["edited_at"],
            reply_to_message_id=row["reply_to_message_id"],
            reply_preview=reply_preview,
        )
        await websocket.send_text(json.dumps(payload))


async def send_private_history(websocket: WebSocket, conn, user_id: int):
    private_rows = await conn.fetch("""
        SELECT
            pm.id,
            pm.text,
            pm.created_at,
            pm.edited_at,
            pm.reply_to_message_id,
            sender.username AS from_username,
            receiver.username AS to_username,
            reply_sender.username AS reply_username,
            rpm.text AS reply_text,
            rpm.created_at AS reply_created_at
        FROM private_messages pm
        JOIN users sender ON pm.from_user_id = sender.id
        JOIN users receiver ON pm.to_user_id = receiver.id
        LEFT JOIN private_messages rpm ON pm.reply_to_message_id = rpm.id
        LEFT JOIN users reply_sender ON rpm.from_user_id = reply_sender.id
        WHERE pm.from_user_id = $1 OR pm.to_user_id = $1
        ORDER BY pm.id ASC
    """, user_id)

    for row in private_rows:
        reply_preview = build_reply_preview(
            row["reply_username"],
            row["reply_text"],
            row["reply_created_at"],
        )
        payload = build_private_message_payload(
            message_id=row["id"],
            from_username=row["from_username"],
            to_username=row["to_username"],
            text=row["text"],
            created_at=row["created_at"],
            edited_at=row["edited_at"],
            reply_to_message_id=row["reply_to_message_id"],
            reply_preview=reply_preview,
        )
        await websocket.send_text(json.dumps(payload))


async def broadcast_to_all(payload: dict):
    payload_text = json.dumps(payload)
    dead_clients = []

    for ws, uid, uname in connected_clients:
        try:
            await ws.send_text(payload_text)
        except Exception:
            dead_clients.append((ws, uid, uname))

    for dead in dead_clients:
        connected_clients.discard(dead)


async def send_private_payload_to_participants(
    payload: dict,
    sender_ws: WebSocket,
    sender_user_id: int,
    sender_username: str,
    target_username: str | None,
):
    payload_text = json.dumps(payload)
    dead_clients = []

    try:
        await sender_ws.send_text(payload_text)
    except Exception:
        dead_clients.append((sender_ws, sender_user_id, sender_username))

    if target_username and target_username != sender_username:
        target_client = find_client_by_username(target_username)
        if target_client:
            target_ws, target_uid, target_uname = target_client
            try:
                await target_ws.send_text(payload_text)
            except Exception:
                dead_clients.append((target_ws, target_uid, target_uname))

    for dead in dead_clients:
        connected_clients.discard(dead)


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

        if is_username_online(username):
            await websocket.close()
            return

        connected_clients.add((websocket, user_id, username))
        print(f"User connected: {username} ({user_id})")

        await broadcast_system_message(f"{username} вошёл в чат")

        await send_public_history(websocket, conn)
        await send_private_history(websocket, conn, user_id)
        await send_unread_private_counts(websocket, conn, user_id)
        await broadcast_online_status()

        while True:
            msg_raw = await websocket.receive_text()
            data = json.loads(msg_raw)
            msg_type = data.get("type")

            if msg_type == "delete_favorites_message":
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

            if msg_type == "delete_private_message":
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

                delete_payload = {
                    "type": "private_message_deleted",
                    "message_id": message_id,
                    "peer_username": peer_username,
                    "success": success
                }

                await websocket.send_text(json.dumps(delete_payload))

                if success:
                    target_client = find_client_by_username(peer_username)
                    if target_client:
                        target_ws, target_uid, target_uname = target_client
                        try:
                            await target_ws.send_text(json.dumps(delete_payload))
                        except Exception:
                            connected_clients.discard((target_ws, target_uid, target_uname))
                continue

            if msg_type == "clear_private_dialog":
                peer_username = data.get("peer_username", "").strip()
                if peer_username:
                    success = await clear_private_dialog(conn, user_id, peer_username)

                    requester_payload = {
                        "type": "private_dialog_cleared",
                        "peer_username": peer_username,
                        "success": success
                    }
                    await websocket.send_text(json.dumps(requester_payload))
                    await send_unread_private_counts(websocket, conn, user_id)

                    if success and peer_username != username:
                        target_client = find_client_by_username(peer_username)
                        if target_client:
                            target_ws, target_uid, target_uname = target_client
                            target_payload = {
                                "type": "private_dialog_cleared",
                                "peer_username": username,
                                "success": True
                            }
                            try:
                                await target_ws.send_text(json.dumps(target_payload))
                                await send_unread_private_counts(target_ws, conn, target_uid)
                            except Exception:
                                connected_clients.discard((target_ws, target_uid, target_uname))
                continue

            if msg_type == "mark_private_as_read":
                from_username = data.get("from_username", "").strip()
                if from_username:
                    await mark_private_messages_as_read(conn, user_id, from_username)
                    await send_unread_private_counts(websocket, conn, user_id)
                continue

            if msg_type == "edit_chat_message":
                message_id = data.get("message_id")
                new_text = str(data.get("text", "")).strip()

                if message_id is None or not new_text:
                    await websocket.send_text(json.dumps({
                        "type": "chat_message_edited",
                        "message_id": message_id,
                        "success": False
                    }))
                    continue

                try:
                    message_id = int(message_id)
                except Exception:
                    await websocket.send_text(json.dumps({
                        "type": "chat_message_edited",
                        "message_id": data.get("message_id"),
                        "success": False
                    }))
                    continue

                edited_at = int(time.time())
                row = await conn.fetchrow("""
                    UPDATE messages
                    SET text = $1,
                        edited_at = $2
                    WHERE id = $3
                      AND user_id = $4
                    RETURNING id, text, created_at, edited_at, reply_to_message_id
                """, new_text, edited_at, message_id, user_id)

                if not row:
                    await websocket.send_text(json.dumps({
                        "type": "chat_message_edited",
                        "message_id": message_id,
                        "success": False
                    }))
                    continue

                reply_preview = None
                if row["reply_to_message_id"] is not None:
                    reply_row = await get_public_reply_row(conn, int(row["reply_to_message_id"]))
                    if reply_row:
                        reply_preview = build_reply_preview(
                            reply_row["username"],
                            reply_row["text"],
                            reply_row["created_at"],
                        )

                payload = {
                    "type": "chat_message_edited",
                    "success": True,
                    "message_id": row["id"],
                    "username": username,
                    "text": row["text"],
                    "created_at": row["created_at"],
                    "edited_at": row["edited_at"],
                    "reply_to_message_id": row["reply_to_message_id"],
                    "reply_preview": reply_preview,
                }
                await broadcast_to_all(payload)
                continue

            if msg_type == "edit_private_message":
                message_id = data.get("message_id")
                peer_username = str(data.get("peer_username", "")).strip()
                new_text = str(data.get("text", "")).strip()

                if message_id is None or not peer_username or not new_text:
                    await websocket.send_text(json.dumps({
                        "type": "private_message_edited",
                        "message_id": message_id,
                        "peer_username": peer_username,
                        "success": False
                    }))
                    continue

                try:
                    message_id = int(message_id)
                except Exception:
                    await websocket.send_text(json.dumps({
                        "type": "private_message_edited",
                        "message_id": data.get("message_id"),
                        "peer_username": peer_username,
                        "success": False
                    }))
                    continue

                if peer_username == username:
                    target_user = {"id": user_id, "username": username}
                else:
                    target_user = await conn.fetchrow(
                        "SELECT id, username FROM users WHERE username=$1",
                        peer_username
                    )
                if not target_user:
                    await websocket.send_text(json.dumps({
                        "type": "private_message_edited",
                        "message_id": message_id,
                        "peer_username": peer_username,
                        "success": False
                    }))
                    continue

                edited_at = int(time.time())
                row = await conn.fetchrow("""
                    UPDATE private_messages
                    SET text = $1,
                        edited_at = $2
                    WHERE id = $3
                      AND from_user_id = $4
                      AND to_user_id = $5
                    RETURNING id, text, created_at, edited_at, reply_to_message_id
                """, new_text, edited_at, message_id, user_id, target_user["id"])

                if not row:
                    await websocket.send_text(json.dumps({
                        "type": "private_message_edited",
                        "message_id": message_id,
                        "peer_username": peer_username,
                        "success": False
                    }))
                    continue

                reply_preview = None
                if row["reply_to_message_id"] is not None:
                    reply_row = await get_private_reply_row(
                        conn,
                        user_id,
                        target_user["id"],
                        int(row["reply_to_message_id"])
                    )
                    if reply_row:
                        reply_preview = build_reply_preview(
                            reply_row["from_username"],
                            reply_row["text"],
                            reply_row["created_at"],
                        )

                payload = {
                    "type": "private_message_edited",
                    "success": True,
                    "message_id": row["id"],
                    "peer_username": peer_username,
                    "from_username": username,
                    "to_username": target_user["username"],
                    "text": row["text"],
                    "created_at": row["created_at"],
                    "edited_at": row["edited_at"],
                    "reply_to_message_id": row["reply_to_message_id"],
                    "reply_preview": reply_preview,
                }
                await send_private_payload_to_participants(
                    payload,
                    websocket,
                    user_id,
                    username,
                    target_user["username"],
                )
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

            text = str(data.get("text", "")).strip()
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
                reply_to_message_id = data.get("reply_to_message_id")

                valid_reply_message_id = None
                reply_preview = None

                if reply_to_message_id is not None:
                    try:
                        reply_to_message_id = int(reply_to_message_id)
                        reply_row = await get_private_reply_row(
                            conn,
                            user_id,
                            target_uid,
                            reply_to_message_id
                        )
                        if reply_row:
                            valid_reply_message_id = reply_row["id"]
                            reply_preview = build_reply_preview(
                                reply_row["from_username"],
                                reply_row["text"],
                                reply_row["created_at"],
                            )
                    except Exception:
                        valid_reply_message_id = None
                        reply_preview = None

                target_client = find_client_by_username(target_username)
                is_self_message = (target_uid == user_id)

                inserted = await conn.fetchrow(
                    """
                    INSERT INTO private_messages (
                        from_user_id,
                        to_user_id,
                        text,
                        created_at,
                        is_read,
                        reply_to_message_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id, edited_at, reply_to_message_id
                    """,
                    user_id,
                    target_uid,
                    text,
                    created_at,
                    True if is_self_message else False,
                    valid_reply_message_id
                )
                message_id = inserted["id"]

                private_payload = build_private_message_payload(
                    message_id=message_id,
                    from_username=username,
                    to_username=target_uname,
                    text=text,
                    created_at=created_at,
                    edited_at=inserted["edited_at"],
                    reply_to_message_id=inserted["reply_to_message_id"],
                    reply_preview=reply_preview,
                )

                await send_private_payload_to_participants(
                    private_payload,
                    websocket,
                    user_id,
                    username,
                    None if is_self_message else target_uname,
                )
                continue

            reply_to_message_id = data.get("reply_to_message_id")
            valid_reply_message_id = None
            reply_preview = None

            if reply_to_message_id is not None:
                try:
                    reply_to_message_id = int(reply_to_message_id)
                    reply_row = await get_public_reply_row(conn, reply_to_message_id)
                    if reply_row:
                        valid_reply_message_id = reply_row["id"]
                        reply_preview = build_reply_preview(
                            reply_row["username"],
                            reply_row["text"],
                            reply_row["created_at"],
                        )
                except Exception:
                    valid_reply_message_id = None
                    reply_preview = None

            inserted = await conn.fetchrow(
                """
                INSERT INTO messages (user_id, text, created_at, reply_to_message_id)
                VALUES ($1, $2, $3, $4)
                RETURNING id, edited_at, reply_to_message_id
                """,
                user_id,
                text,
                created_at,
                valid_reply_message_id
            )

            message_to_send = build_chat_message_payload(
                message_id=inserted["id"],
                username=username,
                text=text,
                created_at=created_at,
                edited_at=inserted["edited_at"],
                reply_to_message_id=inserted["reply_to_message_id"],
                reply_preview=reply_preview,
            )

            await broadcast_to_all(message_to_send)

    except Exception as e:
        print("WebSocket error:", e)

    finally:
        if user_id and username:
            connected_clients.discard((websocket, user_id, username))
            print(f"User disconnected: {username} ({user_id})")

            await broadcast_system_message(f"{username} вышел из чата")
            await broadcast_online_status()

        await conn.close()
