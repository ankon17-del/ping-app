import os
import json
from fastapi import FastAPI, WebSocket, Request
import asyncpg

app = FastAPI()

DATABASE_URL = os.environ.get("DATABASE_URL")

async def get_conn():
    return await asyncpg.connect(DATABASE_URL)

@app.post("/register")
async def register(request: Request):
    conn = await get_conn()
    data = await request.json()
    await conn.execute("INSERT INTO users(username, password) VALUES($1,$2)", data["username"], data["password"])
    user = await conn.fetchrow("SELECT id FROM users WHERE username=$1", data["username"])
    await conn.close()
    return {"id": user["id"]}

@app.post("/login")
async def login(request: Request):
    conn = await get_conn()
    data = await request.json()
    user = await conn.fetchrow("SELECT id FROM users WHERE username=$1 AND password=$2", data["username"], data["password"])
    await conn.close()
    if user:
        return {"id": user["id"]}
    return {"error": "Неверный логин или пароль"}

connected_clients = set()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    conn = await get_conn()
    connected_clients.add(websocket)
    try:
        while True:
            msg_raw = await websocket.receive_text()
            msg = json.loads(msg_raw)
            if "text" in msg and "user_id" in msg:
                await conn.execute(
                    "INSERT INTO messages(user_id, text, created_at) VALUES($1,$2,EXTRACT(EPOCH FROM NOW())::int)",
                    msg["user_id"], msg["text"]
                )
                # Рассылаем всем клиентам
                for client in connected_clients:
                    await client.send_text(f"{msg['text']}")
    except:
        pass
    finally:
        connected_clients.discard(websocket)
        await conn.close()
