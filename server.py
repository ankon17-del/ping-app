from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import asyncpg
import os
import json
import time
import hashlib
import hmac
import base64
import secrets
import uuid

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL")
connected_clients = set()  # (websocket, user_id, username)

AUDIO_STORAGE_DIR = os.getenv("AUDIO_STORAGE_DIR", "private_audio_files")
os.makedirs(AUDIO_STORAGE_DIR, exist_ok=True)


class RegisterData(BaseModel):
    username: str
    password: str


class LoginData(BaseModel):
    username: str
    password: str


class PrivateAudioUploadData(BaseModel):
    sender_user_id: int
    sender_username: str
    target_username: str
    duration_seconds: int = 0
    audio_data_base64: str
    original_file_name: str | None = None


async def get_conn():
    return await asyncpg.connect(DATABASE_URL)


@app.get("/")
async def root():
    return {"status": "Server is running"}


# -------------------------
# Password hashing
# -------------------------
PBKDF2_PREFIX = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000


def make_password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS
    )
    return (
        f"{PBKDF2_PREFIX}$"
        f"{PBKDF2_ITERATIONS}$"
        f"{base64.b64encode(salt).decode('utf-8')}$"
        f"{base64.b64encode(digest).decode('utf-8')}"
    )


def is_password_hash(value: str) -> bool:
    return isinstance(value, str) and value.startswith(f"{PBKDF2_PREFIX}$")


def verify_password(password: str, stored_value: str) -> bool:
    if not stored_value:
        return False

    if not is_password_hash(stored_value):
        return password == stored_value

    try:
        _, iterations_str, salt_b64, digest_b64 = stored_value.split("$", 3)
        iterations = int(iterations_str)
        salt = base64.b64decode(salt_b64.encode("utf-8"))
        expected_digest = base64.b64decode(digest_b64.encode("utf-8"))

        actual_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations
        )
        return hmac.compare_digest(actual_digest, expected_digest)
    except Exception:
        return False


def is_username_online(target_username: str) -> bool:
    for _, _, uname in connected_clients:
        if uname == target_username:
            return True
    return False


# -------------------------
# DB preparation
# -------------------------
async def ensure_schema():
    conn = await get_conn()
    try:
        async with conn.transaction():
            await conn.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS edited_at BIGINT NULL
            """)
            await conn.execute("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT NULL
            """)
            await conn.execute("""
                ALTER TABLE private_messages
                ADD COLUMN IF NOT EXISTS edited_at BIGINT NULL
            """)
            await conn.execute("""
                ALTER TABLE private_messages
                ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT NULL
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS private_audio_messages (
                    id BIGSERIAL PRIMARY KEY,
                    from_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    to_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    file_name TEXT NOT NULL,
                    original_file_name TEXT NULL,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    created_at BIGINT NOT NULL,
                    is_read BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)

        exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'private_audio_messages'
            )
        """)
        print(f"[DB] private_audio_messages exists: {exists}")

    finally:
        await conn.close()


@app.on_event("startup")
async def on_startup():
    print("[STARTUP] ensure_schema begin")
    await ensure_schema()
    print("[STARTUP] ensure_schema done")


# -------------------------
# Auth
# -------------------------
@app.post("/register")
async def register(data: RegisterData):
    conn = await get_conn()
    try:
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE username=$1",
            data.username
        )
        if existing:
            return {"success": False, "message": "Username already exists"}

        password_hash = make_password_hash(data.password)

        await conn.execute(
            "INSERT INTO users (username, password) VALUES ($1, $2)",
            data.username,
            password_hash
        )
        return {"success": True, "message": "Registered successfully"}

    except Exception as e:
        print("REGISTER ERROR:", e)
        return {"success": False, "message": str(e)}

    finally:
        await conn.close()


@app.post("/login")
async def login(data: LoginData):
    conn = await get_conn()
    try:
        user = await conn.fetchrow(
            "SELECT id, username, password FROM users WHERE username=$1",
            data.username,
        )
        if not user:
            return {"success": False, "message": "Invalid username or password"}

        if is_username_online(user["username"]):
            return {"success": False, "message": "Пользователь уже авторизован"}

        stored_password = user["password"]
        if not verify_password(data.password, stored_password):
            return {"success": False, "message": "Invalid username or password"}

        if not is_password_hash(stored_password):
            new_hash = make_password_hash(data.password)
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

    except Exception as e:
        print("LOGIN ERROR:", e)
        return {"success": False, "message": str(e)}

    finally:
        await conn.close()


# -------------------------
# Helper functions
# -------------------------
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

    audio_rows = await conn.fetch("""
        SELECT sender.username, COUNT(*) AS unread_count
        FROM private_audio_messages pam
        JOIN users sender ON pam.from_user_id = sender.id
        WHERE pam.to_user_id = $1 AND pam.is_read = FALSE
        GROUP BY sender.username
    """, current_user_id)

    counts = {row["username"]: int(row["unread_count"]) for row in rows}
    for row in audio_rows:
        counts[row["username"]] = counts.get(row["username"], 0) + int(row["unread_count"])

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

    await conn.execute("""
        UPDATE private_audio_messages
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
        await conn.execute("""
            DELETE FROM private_audio_messages
            WHERE from_user_id = $1 AND to_user_id = $1
        """, current_user_id)
        return True

    await conn.execute("""
        DELETE FROM private_messages
        WHERE (from_user_id = $1 AND to_user_id = $2)
           OR (from_user_id = $2 AND to_user_id = $1)
    """, current_user_id, peer_id)

    await conn.execute("""
        DELETE FROM private_audio_messages
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


async def get_chat_message_reply_preview(conn, reply_to_message_id):
    if not reply_to_message_id:
        return None

    row = await conn.fetchrow("""
        SELECT
            m.id,
            m.text,
            m.created_at,
            m.edited_at,
            u.username
        FROM messages m
        JOIN users u ON m.user_id = u.id
        WHERE m.id = $1
    """, int(reply_to_message_id))

    if not row:
        return None

    return {
        "message_id": row["id"],
        "username": row["username"],
        "text": row["text"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
    }


async def get_private_message_reply_preview(conn, reply_to_message_id):
    if not reply_to_message_id:
        return None

    row = await conn.fetchrow("""
        SELECT
            pm.id,
            pm.text,
            pm.created_at,
            pm.edited_at,
            sender.username AS from_username,
            receiver.username AS to_username
        FROM private_messages pm
        JOIN users sender ON pm.from_user_id = sender.id
        JOIN users receiver ON pm.to_user_id = receiver.id
        WHERE pm.id = $1
    """, int(reply_to_message_id))

    if not row:
        return None

    return {
        "message_id": row["id"],
        "from_username": row["from_username"],
        "to_username": row["to_username"],
        "text": row["text"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
    }


def build_private_audio_payload(
    *,
    audio_id: int,
    from_username: str,
    to_username: str,
    file_name: str,
    original_file_name: str | None,
    duration_seconds: int,
    created_at: int,
):
    return {
        "type": "private_audio_message",
        "audio_id": audio_id,
        "from_username": from_username,
        "to_username": to_username,
        "file_name": file_name,
        "original_file_name": original_file_name,
        "duration_seconds": duration_seconds,
        "created_at": created_at,
        "audio_url": f"/private-audio/{file_name}",
    }


# -------------------------
# Audio endpoints (no multipart dependency)
# -------------------------
@app.post("/upload_private_audio")
async def upload_private_audio(data: PrivateAudioUploadData):
    conn = await get_conn()
    try:
        sender = await conn.fetchrow(
            "SELECT id, username FROM users WHERE id=$1",
            data.sender_user_id
        )
        if not sender or sender["username"] != data.sender_username:
            raise HTTPException(status_code=400, detail="Invalid sender")

        target = await conn.fetchrow(
            "SELECT id, username FROM users WHERE username=$1",
            data.target_username
        )
        if not target:
            raise HTTPException(status_code=404, detail="Target user not found")

        target_user_id = target["id"]
        target_user_name = target["username"]
        is_self_message = target_user_id == data.sender_user_id

        original_name = data.original_file_name or "audio_message.webm"
        ext = os.path.splitext(original_name)[1].lower()
        if not ext:
            ext = ".webm"

        safe_file_name = f"{uuid.uuid4().hex}{ext}"
        file_path = os.path.join(AUDIO_STORAGE_DIR, safe_file_name)

        try:
            audio_bytes = base64.b64decode(data.audio_data_base64.encode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid audio_data_base64")

        with open(file_path, "wb") as f:
            f.write(audio_bytes)

        created_at = int(time.time())

        inserted = await conn.fetchrow("""
            INSERT INTO private_audio_messages (
                from_user_id,
                to_user_id,
                file_name,
                original_file_name,
                duration_seconds,
                created_at,
                is_read
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, file_name, original_file_name, duration_seconds, created_at
        """,
            data.sender_user_id,
            target_user_id,
            safe_file_name,
            original_name,
            max(0, int(data.duration_seconds)),
            created_at,
            True if is_self_message else False
        )

        payload_dict = build_private_audio_payload(
            audio_id=inserted["id"],
            from_username=data.sender_username,
            to_username=target_user_name,
            file_name=inserted["file_name"],
            original_file_name=inserted["original_file_name"],
            duration_seconds=int(inserted["duration_seconds"]),
            created_at=int(inserted["created_at"]),
        )
        payload = json.dumps(payload_dict)

        dead_clients = []

        sender_client = find_client_by_username(data.sender_username)
        if sender_client:
            try:
                sender_ws, sender_uid, sender_uname = sender_client
                await sender_ws.send_text(payload)
            except Exception:
                dead_clients.append(sender_client)

        if not is_self_message:
            target_client = find_client_by_username(target_user_name)
            if target_client:
                try:
                    target_ws, target_uid, target_uname = target_client
                    await target_ws.send_text(payload)
                    await send_unread_private_counts(target_ws, conn, target_uid)
                except Exception:
                    dead_clients.append(target_client)

        for dead in dead_clients:
            connected_clients.discard(dead)

        return {
            "success": True,
            **payload_dict
        }

    except HTTPException:
        raise
    except Exception as e:
        print("UPLOAD PRIVATE AUDIO ERROR:", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@app.get("/private-audio/{file_name}")
async def get_private_audio(file_name: str):
    safe_name = os.path.basename(file_name)
    file_path = os.path.join(AUDIO_STORAGE_DIR, safe_name)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(file_path)


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
        auth_raw = await websocket.receive_text()
        auth = json.loads(auth_raw)

        user_id = auth.get("user_id")
        username = auth.get("username")

        if not user_id or not username:
            await websocket.close()
            return

        user = await conn.fetchrow("SELECT id, username FROM users WHERE id=$1", user_id)
        if not user or user["username"] != username:
            await websocket.close()
            return

        existing_client = find_client_by_username(username)
        if existing_client:
            await websocket.send_text(json.dumps({
                "type": "auth_error",
                "message": "Пользователь уже авторизован"
            }))
            await websocket.close()
            return

        connected_clients.add((websocket, user_id, username))
        print(f"User connected: {username} ({user_id})")

        await broadcast_system_message(f"{username} вошёл в чат")

        rows = await conn.fetch("""
            SELECT
                messages.id,
                messages.text,
                messages.created_at,
                messages.edited_at,
                messages.reply_to_message_id,
                users.username
            FROM messages
            JOIN users ON messages.user_id = users.id
            ORDER BY messages.id DESC
            LIMIT 1000
        """)
        for row in reversed(rows):
            reply_preview = await get_chat_message_reply_preview(conn, row["reply_to_message_id"])
            await websocket.send_text(json.dumps({
                "type": "chat_message",
                "message_id": row["id"],
                "username": row["username"],
                "text": row["text"],
                "created_at": row["created_at"],
                "edited_at": row["edited_at"],
                "reply_to_message_id": row["reply_to_message_id"],
                "reply_to_message": reply_preview,
                "reply_preview": reply_preview,
            }))

        private_rows = await conn.fetch("""
            SELECT
                pm.id,
                pm.text,
                pm.created_at,
                pm.edited_at,
                pm.reply_to_message_id,
                sender.username AS from_username,
                receiver.username AS to_username
            FROM private_messages pm
            JOIN users sender ON pm.from_user_id = sender.id
            JOIN users receiver ON pm.to_user_id = receiver.id
            WHERE pm.from_user_id = $1 OR pm.to_user_id = $1
            ORDER BY pm.id ASC
        """, user_id)

        for row in private_rows:
            reply_preview = await get_private_message_reply_preview(conn, row["reply_to_message_id"])
            await websocket.send_text(json.dumps({
                "type": "private_message",
                "message_id": row["id"],
                "from_username": row["from_username"],
                "to_username": row["to_username"],
                "text": row["text"],
                "created_at": row["created_at"],
                "edited_at": row["edited_at"],
                "reply_to_message_id": row["reply_to_message_id"],
                "reply_to_message": reply_preview,
                "reply_preview": reply_preview,
            }))

        private_audio_rows = await conn.fetch("""
            SELECT
                pam.id,
                pam.file_name,
                pam.original_file_name,
                pam.duration_seconds,
                pam.created_at,
                sender.username AS from_username,
                receiver.username AS to_username
            FROM private_audio_messages pam
            JOIN users sender ON pam.from_user_id = sender.id
            JOIN users receiver ON pam.to_user_id = receiver.id
            WHERE pam.from_user_id = $1 OR pam.to_user_id = $1
            ORDER BY pam.id ASC
        """, user_id)

        for row in private_audio_rows:
            await websocket.send_text(json.dumps({
                "type": "private_audio_message",
                "audio_id": row["id"],
                "from_username": row["from_username"],
                "to_username": row["to_username"],
                "file_name": row["file_name"],
                "original_file_name": row["original_file_name"],
                "duration_seconds": row["duration_seconds"],
                "created_at": row["created_at"],
                "audio_url": f"/private-audio/{row['file_name']}",
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

            if data.get("type") == "edit_chat_message":
                message_id = data.get("message_id")
                new_text = data.get("text", "").strip()

                if not message_id:
                    await websocket.send_text(json.dumps({
                        "type": "chat_message_edited",
                        "success": False,
                        "message": "Message ID is required"
                    }))
                    continue

                if not new_text:
                    await websocket.send_text(json.dumps({
                        "type": "chat_message_edited",
                        "success": False,
                        "message": "Message text cannot be empty"
                    }))
                    continue

                edited_at = int(time.time())

                updated = await conn.fetchrow("""
                    UPDATE messages
                    SET text = $1, edited_at = $2
                    WHERE id = $3 AND user_id = $4
                    RETURNING id, text, created_at, edited_at, reply_to_message_id
                """, new_text, edited_at, int(message_id), user_id)

                if not updated:
                    await websocket.send_text(json.dumps({
                        "type": "chat_message_edited",
                        "success": False,
                        "message": "Не удалось отредактировать сообщение"
                    }))
                    continue

                reply_preview = await get_chat_message_reply_preview(conn, updated["reply_to_message_id"])

                payload = json.dumps({
                    "type": "chat_message_edited",
                    "success": True,
                    "message_id": updated["id"],
                    "username": username,
                    "text": updated["text"],
                    "created_at": updated["created_at"],
                    "edited_at": updated["edited_at"],
                    "reply_to_message_id": updated["reply_to_message_id"],
                    "reply_to_message": reply_preview,
                    "reply_preview": reply_preview,
                })

                dead_clients = []
                for ws, uid, uname in connected_clients:
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead_clients.append((ws, uid, uname))
                for dead in dead_clients:
                    connected_clients.discard(dead)
                continue

            if data.get("type") == "edit_private_message":
                message_id = data.get("message_id")
                peer_username = data.get("peer_username", "").strip()
                new_text = data.get("text", "").strip()

                if not message_id or not new_text or not peer_username:
                    await websocket.send_text(json.dumps({
                        "type": "private_message_edited",
                        "success": False,
                        "message": "Не удалось отредактировать личное сообщение"
                    }))
                    continue

                if peer_username == username or peer_username == "Избранное":
                    peer_id = user_id
                else:
                    peer = await conn.fetchrow(
                        "SELECT id FROM users WHERE username=$1",
                        peer_username
                    )
                    if not peer:
                        await websocket.send_text(json.dumps({
                            "type": "private_message_edited",
                            "success": False,
                            "message": "Не удалось отредактировать личное сообщение"
                        }))
                        continue
                    peer_id = peer["id"]

                edited_at = int(time.time())

                updated = await conn.fetchrow("""
                    UPDATE private_messages
                    SET text = $1, edited_at = $2
                    WHERE id = $3
                      AND from_user_id = $4
                      AND to_user_id = $5
                    RETURNING id, text, created_at, edited_at, reply_to_message_id
                """, new_text, edited_at, int(message_id), user_id, peer_id)

                if not updated:
                    await websocket.send_text(json.dumps({
                        "type": "private_message_edited",
                        "success": False,
                        "message": "Не удалось отредактировать личное сообщение"
                    }))
                    continue

                reply_preview = await get_private_message_reply_preview(conn, updated["reply_to_message_id"])

                payload = json.dumps({
                    "type": "private_message_edited",
                    "success": True,
                    "message_id": updated["id"],
                    "from_username": username,
                    "to_username": peer_username,
                    "text": updated["text"],
                    "created_at": updated["created_at"],
                    "edited_at": updated["edited_at"],
                    "reply_to_message_id": updated["reply_to_message_id"],
                    "reply_to_message": reply_preview,
                    "reply_preview": reply_preview,
                })

                try:
                    await websocket.send_text(payload)
                except Exception:
                    connected_clients.discard((websocket, user_id, username))

                if peer_username != username:
                    target_client = find_client_by_username(peer_username)
                    if target_client:
                        target_ws, target_uid, target_uname = target_client
                        try:
                            await target_ws.send_text(payload)
                        except Exception:
                            connected_clients.discard((target_ws, target_uid, target_uname))
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

            created_at = int(time.time())
            reply_to_message_id = data.get("reply_to_message_id")
            target_username = data.get("target_username")

            if target_username:
                target_username = target_username.strip()
                target_user = await conn.fetchrow(
                    "SELECT id, username FROM users WHERE username=$1",
                    target_username
                )
                if not target_user:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Пользователь не найден"
                    }))
                    continue

                target_uid = target_user["id"]
                target_uname = target_user["username"]
                target_client = find_client_by_username(target_uname)
                is_self_message = target_uid == user_id

                inserted = await conn.fetchrow("""
                    INSERT INTO private_messages (
                        from_user_id,
                        to_user_id,
                        text,
                        created_at,
                        is_read,
                        reply_to_message_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id, text, created_at, edited_at, reply_to_message_id
                """,
                    user_id,
                    target_uid,
                    text,
                    created_at,
                    True if is_self_message else False,
                    int(reply_to_message_id) if reply_to_message_id is not None else None
                )

                reply_preview = await get_private_message_reply_preview(conn, inserted["reply_to_message_id"])

                private_payload = json.dumps({
                    "type": "private_message",
                    "message_id": inserted["id"],
                    "from_username": username,
                    "to_username": target_uname,
                    "text": inserted["text"],
                    "created_at": inserted["created_at"],
                    "edited_at": inserted["edited_at"],
                    "reply_to_message_id": inserted["reply_to_message_id"],
                    "reply_to_message": reply_preview,
                    "reply_preview": reply_preview,
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

            inserted = await conn.fetchrow("""
                INSERT INTO messages (user_id, text, created_at, reply_to_message_id)
                VALUES ($1, $2, $3, $4)
                RETURNING id, text, created_at, edited_at, reply_to_message_id
            """,
                user_id,
                text,
                created_at,
                int(reply_to_message_id) if reply_to_message_id is not None else None
            )

            reply_preview = await get_chat_message_reply_preview(conn, inserted["reply_to_message_id"])

            message_to_send = json.dumps({
                "type": "chat_message",
                "message_id": inserted["id"],
                "username": username,
                "text": inserted["text"],
                "created_at": inserted["created_at"],
                "edited_at": inserted["edited_at"],
                "reply_to_message_id": inserted["reply_to_message_id"],
                "reply_to_message": reply_preview,
                "reply_preview": reply_preview,
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
