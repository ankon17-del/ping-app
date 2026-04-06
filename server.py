from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import uuid
import os
import hashlib

app = FastAPI()

# --------------------------
# CORS (для возможного web-клиента в будущем)
# --------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------
# Данные пользователей
# --------------------------
USERS_FILE = "users.json"
if os.path.exists(USERS_FILE):
    with open(USERS_FILE, "r") as f:
        users_db = json.load(f)
else:
    users_db = {}  # {username: {"password": hashed, "user_id": str}}

connected_clients = set()  # {(websocket, username, user_id)}
message_history = []
MAX_HISTORY = 100

# --------------------------
# Вспомогательные функции
# --------------------------
def save_users():
    with open(USERS_FILE, "w") as f:
        json.dump(users_db, f)

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

async def broadcast(message: dict, exclude=[]):
    to_remove = []
    for ws, _, _ in connected_clients:
        if ws in exclude:
            continue
        try:
            await ws.send_text(json.dumps(message))
        except:
            to_remove.append(ws)
    for ws in to_remove:
        for c in connected_clients.copy():
            if c[0] == ws:
                connected_clients.remove(c)

# --------------------------
# HTTP Endpoints для регистрации и логина
# --------------------------
@app.post("/register")
async def register(data: dict):
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username и password обязательны")
    if username in users_db:
        raise HTTPException(status_code=400, detail="Username уже существует")
    user_id = str(uuid.uuid4())
    users_db[username] = {"password": hash_password(password), "user_id": user_id}
    save_users()
    return {"status": "ok", "user_id": user_id}

@app.post("/login")
async def login(data: dict):
    username = data.get("username")
    password = data.get("password")
    if username not in users_db or users_db[username]["password"] != hash_password(password):
        raise HTTPException(status_code=400, detail="Неверный логин или пароль")
    return {"status": "ok", "user_id": users_db[username]["user_id"]}

# --------------------------
# WebSocket для чата
# --------------------------
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

        connected_clients.add((websocket, username, user_id))
        print(f"{username} ({user_id}) connected")

        # Отправка истории сообщений
        for msg in message_history:
            await websocket.send_text(json.dumps(msg))

        # Системное уведомление о подключении
        system_msg = {"type": "system", "text": f"{username} подключился", "user": username}
        await broadcast(system_msg, exclude=[websocket])

        while True:
            raw_msg = await websocket.receive_text()
            if raw_msg.startswith("__PING__"):
                ping_msg = {"type": "ping", "user": username, "text": "Ping!"}
                await broadcast(ping_msg, exclude=[websocket])
                continue

            chat_msg = {"type": "message", "user": username, "user_id": user_id, "text": raw_msg}
            message_history.append(chat_msg)
            if len(message_history) > MAX_HISTORY:
                message_history.pop(0)
            await broadcast(chat_msg)
    except WebSocketDisconnect:
        connected_clients.remove((websocket, username, user_id))
        disc_msg = {"type": "system", "text": f"{username} отключился", "user": username}
        await broadcast(disc_msg)
