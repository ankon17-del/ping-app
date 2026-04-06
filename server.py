# server.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, List

app = FastAPI()

# websocket -> username
connected_clients: Dict[WebSocket, str] = {}

# История сообщений
message_history: List[str] = []
MAX_HISTORY = 100


def sanitize_username(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "Anonymous"

    # Убираем слишком длинные имена
    name = name[:20]

    # Запрещаем служебные названия
    if name.startswith("__"):
        name = "Anonymous"

    return name


def make_unique_username(name: str) -> str:
    existing_names = set(connected_clients.values())

    if name not in existing_names:
        return name

    counter = 2
    while f"{name}_{counter}" in existing_names:
        counter += 1

    return f"{name}_{counter}"


def add_to_history(msg: str):
    global message_history
    message_history.append(msg)
    message_history = message_history[-MAX_HISTORY:]


async def safe_send(ws: WebSocket, msg: str) -> bool:
    try:
        await ws.send_text(msg)
        return True
    except Exception:
        return False


async def broadcast(msg: str, exclude_ws: WebSocket = None):
    dead_clients = []

    for ws in list(connected_clients.keys()):
        if exclude_ws and ws == exclude_ws:
            continue

        ok = await safe_send(ws, msg)
        if not ok:
            dead_clients.append(ws)

    for dead_ws in dead_clients:
        username = connected_clients.pop(dead_ws, None)
        if username:
            print(f"Removed dead client: {username}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    username = "Anonymous"

    try:
        # Первое сообщение = имя пользователя
        raw_name = await websocket.receive_text()
        username = sanitize_username(raw_name)
        username = make_unique_username(username)

        connected_clients[websocket] = username
        print(f"New client connected: {username}")

        # Отправляем историю новому клиенту
        for msg in message_history:
            await safe_send(websocket, msg)

        # Сообщаем остальным, что пользователь вошёл
        join_msg = f"*** {username} подключился ***"
        add_to_history(join_msg)
        await broadcast(join_msg, exclude_ws=websocket)

        while True:
            msg = await websocket.receive_text()
            msg = msg.strip()

            if not msg:
                continue

            # Обработка Ping
            if msg == "__PING__":
                await broadcast(f"__PING__::{username}", exclude_ws=websocket)
                continue

            # Обычное сообщение
            full_msg = f"{username}: {msg}"
            add_to_history(full_msg)

            await broadcast(full_msg)

    except WebSocketDisconnect:
        print(f"Client {username} disconnected")

    except Exception as e:
        print(f"Error with client {username}: {e}")

    finally:
        if websocket in connected_clients:
            disconnected_user = connected_clients.pop(websocket)
            leave_msg = f"__DISCONNECT__::{disconnected_user}"
            await broadcast(leave_msg)
            print(f"Cleaned up client: {disconnected_user}")
