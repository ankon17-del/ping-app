# client.py
import asyncio
import json
import threading
from datetime import datetime
from io import BytesIO

import pygame
import requests
import tkinter as tk
import websockets
from PIL import Image, ImageTk
from tkinter import messagebox, scrolledtext

# -------------------------
# Настройки
# -------------------------
SERVER_URL = "wss://ping-app-production-8557.up.railway.app/ws"
HTTP_URL = "https://ping-app-production-8557.up.railway.app"
PING_SOUND_URL = "https://raw.githubusercontent.com/ankon17-del/ping-app/main/assets/ping_sound.mp3"
POPUP_IMAGE_URL = "https://raw.githubusercontent.com/ankon17-del/ping-app/main/assets/popup_image.png"
PING_COOLDOWN_SECONDS = 10
RECONNECT_DELAY_SECONDS = 3

# -------------------------
# Скачиваем ресурсы
# -------------------------
def download_file(url):
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.content


ping_sound_data = download_file(PING_SOUND_URL)
popup_image_data = download_file(POPUP_IMAGE_URL)

pygame.mixer.init()
ping_sound = pygame.mixer.Sound(BytesIO(ping_sound_data))

# -------------------------
# Tkinter GUI
# -------------------------
root = tk.Tk()
root.title("Ping Chat")
root.geometry("860x600")
root.minsize(860, 600)

main_frame = tk.Frame(root)
main_frame.pack(fill="both", expand=True, padx=10, pady=10)

# Левая часть: чат
left_frame = tk.Frame(main_frame)
left_frame.pack(side="left", fill="both", expand=True)

chat_area = scrolledtext.ScrolledText(
    left_frame,
    width=60,
    height=24,
    state="disabled"
)
chat_area.pack(fill="both", expand=True)

# Теги оформления
chat_area.tag_config("system", foreground="green")

# Блок выбора получателя
recipient_frame = tk.Frame(left_frame)
recipient_frame.pack(fill="x", pady=(8, 4))

recipient_label = tk.Label(recipient_frame, text="Кому:")
recipient_label.pack(side="left")

recipient_var = tk.StringVar(value="Общий чат")
recipient_menu = tk.OptionMenu(recipient_frame, recipient_var, "Общий чат")
recipient_menu.config(width=20)
recipient_menu.pack(side="left", padx=(8, 0))

bottom_chat_frame = tk.Frame(left_frame)
bottom_chat_frame.pack(fill="x", pady=(4, 0))

entry = tk.Entry(bottom_chat_frame, width=50, state="disabled")
entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

send_button = tk.Button(bottom_chat_frame, text="Отправить", state="disabled")
send_button.pack(side="left", padx=(0, 6))

ping_button = tk.Button(bottom_chat_frame, text="Ping", state="disabled")
ping_button.pack(side="left")

# Правая часть: статус, пользователи, логин
right_frame = tk.Frame(main_frame, width=250)
right_frame.pack(side="left", fill="y", padx=(12, 0))
right_frame.pack_propagate(False)

status_label = tk.Label(right_frame, text="Статус: не подключен", anchor="w")
status_label.pack(fill="x", pady=(0, 10))

online_block = tk.LabelFrame(right_frame, text="Онлайн")
online_block.pack(fill="x", pady=(0, 10))

online_listbox = tk.Listbox(online_block, height=10)
online_listbox.pack(fill="both", expand=True, padx=6, pady=6)

offline_block = tk.LabelFrame(right_frame, text="Офлайн")
offline_block.pack(fill="x", pady=(0, 10))

offline_listbox = tk.Listbox(offline_block, height=10)
offline_listbox.pack(fill="both", expand=True, padx=6, pady=6)

auth_block = tk.LabelFrame(right_frame, text="Вход / Регистрация")
auth_block.pack(fill="x", pady=(0, 10))

tk.Label(auth_block, text="Логин").pack(anchor="w", padx=6, pady=(6, 2))
login_username_entry = tk.Entry(auth_block)
login_username_entry.pack(fill="x", padx=6)

tk.Label(auth_block, text="Пароль").pack(anchor="w", padx=6, pady=(8, 2))
login_password_entry = tk.Entry(auth_block, show="*")
login_password_entry.pack(fill="x", padx=6)

auth_buttons_frame = tk.Frame(auth_block)
auth_buttons_frame.pack(fill="x", padx=6, pady=8)

login_button = tk.Button(auth_buttons_frame, text="Войти")
login_button.pack(side="left", fill="x", expand=True, padx=(0, 4))

register_button = tk.Button(auth_buttons_frame, text="Регистрация")
register_button.pack(side="left", fill="x", expand=True, padx=(4, 0))

# -------------------------
# Глобальные переменные
# -------------------------
ws_client = None
loop = None
loop_thread = None
user_id = None
username = None
connected = False
ping_cooldown_active = False
ping_cooldown_job = None
manual_close = False
has_logged_in = False

# защита от дублей
seen_chat_messages = set()
seen_system_messages = set()

# список доступных получателей для будущих ЛС
recipient_names = ["Общий чат"]

# -------------------------
# Вспомогательные функции UI
# -------------------------
def set_status(text):
    root.after(0, lambda: status_label.config(text=f"Статус: {text}"))


def add_chat_message(msg):
    def _add():
        chat_area.configure(state="normal")
        chat_area.insert(tk.END, msg + "\n")
        chat_area.configure(state="disabled")
        chat_area.see(tk.END)
    root.after(0, _add)


def add_system_message(msg):
    def _add():
        chat_area.configure(state="normal")
        chat_area.insert(tk.END, msg + "\n", "system")
        chat_area.configure(state="disabled")
        chat_area.see(tk.END)
    root.after(0, _add)


def format_chat_message(msg_username, msg_text, created_at):
    try:
        dt = datetime.fromtimestamp(int(created_at))
        time_str = dt.strftime("%H:%M:%S")
    except Exception:
        time_str = "??:??:??"
    return f"[{time_str}] {msg_username}: {msg_text}"


def format_system_message(msg_text, created_at):
    try:
        dt = datetime.fromtimestamp(int(created_at))
        time_str = dt.strftime("%H:%M:%S")
    except Exception:
        time_str = "??:??:??"
    return f"[{time_str}] --- {msg_text} ---"


def show_popup():
    image = Image.open(BytesIO(popup_image_data))
    photo = ImageTk.PhotoImage(image)

    popup = tk.Toplevel(root)
    popup.title("Ping!")
    popup.resizable(False, False)

    tk.Label(popup, image=photo).pack()
    popup.image = photo

    popup.after(3000, popup.destroy)
    ping_sound.play()


def set_chat_enabled(enabled: bool):
    state = "normal" if enabled else "disabled"
    root.after(0, lambda: send_button.config(state=state))
    root.after(0, lambda: entry.config(state=state))
    root.after(0, lambda: recipient_menu.config(state=state))

    def _set_ping():
        if enabled and not ping_cooldown_active:
            ping_button.config(state="normal", text="Ping")
        else:
            ping_button.config(state="disabled")
            if not enabled:
                ping_button.config(text="Ping")

    root.after(0, _set_ping)


def set_auth_enabled(enabled: bool):
    state = "normal" if enabled else "disabled"
    root.after(0, lambda: login_button.config(state=state))
    root.after(0, lambda: register_button.config(state=state))
    root.after(0, lambda: login_username_entry.config(state=state))
    root.after(0, lambda: login_password_entry.config(state=state))


def refresh_recipient_menu(names):
    def _update():
        menu = recipient_menu["menu"]
        menu.delete(0, "end")

        for name in names:
            menu.add_command(
                label=name,
                command=lambda value=name: recipient_var.set(value)
            )

        current = recipient_var.get()
        if current not in names:
            recipient_var.set("Общий чат")

    root.after(0, _update)


def update_user_lists(data):
    global recipient_names

    online_users = data.get("online", [])
    offline_users = data.get("offline", [])

    def format_name(user):
        username_value = user["username"] if isinstance(user, dict) else str(user)
        if username_value == username:
            return f"{username_value} (ты)"
        return username_value

    raw_names = []

    for user in online_users:
        username_value = user["username"] if isinstance(user, dict) else str(user)
        if username_value != username:
            raw_names.append(username_value)

    for user in offline_users:
        username_value = user["username"] if isinstance(user, dict) else str(user)
        if username_value != username and username_value not in raw_names:
            raw_names.append(username_value)

    recipient_names = ["Общий чат"] + raw_names

    def _update():
        online_listbox.delete(0, tk.END)
        offline_listbox.delete(0, tk.END)

        for user in online_users:
            online_listbox.insert(tk.END, format_name(user))

        for user in offline_users:
            offline_listbox.insert(tk.END, format_name(user))

    root.after(0, _update)
    refresh_recipient_menu(recipient_names)


def focus_message_entry():
    root.after(0, lambda: entry.focus_set())


def start_ping_cooldown():
    global ping_cooldown_active, ping_cooldown_job
    ping_cooldown_active = True

    def tick(seconds_left):
        global ping_cooldown_active, ping_cooldown_job

        if not connected:
            ping_cooldown_active = False
            ping_button.config(text="Ping", state="disabled")
            ping_cooldown_job = None
            return

        if seconds_left <= 0:
            ping_cooldown_active = False
            ping_button.config(text="Ping", state="normal")
            ping_cooldown_job = None
            return

        ping_button.config(text=f"Ping ({seconds_left})", state="disabled")
        ping_cooldown_job = root.after(1000, lambda: tick(seconds_left - 1))

    root.after(0, lambda: tick(PING_COOLDOWN_SECONDS))


def reset_ping_cooldown():
    global ping_cooldown_active, ping_cooldown_job
    ping_cooldown_active = False

    def _reset():
        global ping_cooldown_job
        if ping_cooldown_job is not None:
            root.after_cancel(ping_cooldown_job)
            ping_cooldown_job = None
        ping_button.config(text="Ping")
        ping_button.config(state="normal" if connected else "disabled")

    root.after(0, _reset)


def chat_message_key(data):
    return (
        data.get("username", ""),
        data.get("text", ""),
        int(data.get("created_at", 0)) if data.get("created_at") is not None else 0
    )


def system_message_key(data):
    return (
        data.get("text", ""),
        int(data.get("created_at", 0)) if data.get("created_at") is not None else 0
    )

# -------------------------
# Отправка сообщений и Ping
# -------------------------
def send_message():
    global ws_client, connected, user_id

    msg = entry.get().strip()
    if not msg:
        return

    if not ws_client or not connected:
        messagebox.showwarning("Нет подключения", "Сначала подключись к серверу.")
        return

    # Пока сервер ЛС не поддерживает, отправляем как раньше в общий чат.
    # Выбор получателя уже есть только как интерфейсный шаг.
    data = json.dumps({
        "user_id": user_id,
        "text": msg
    })

    future = asyncio.run_coroutine_threadsafe(ws_client.send(data), loop)

    try:
        future.result(timeout=5)
        entry.delete(0, tk.END)
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось отправить сообщение:\n{e}")


def send_ping():
    global ws_client, connected, user_id, ping_cooldown_active

    if ping_cooldown_active:
        return

    if not ws_client or not connected:
        messagebox.showwarning("Нет подключения", "Сначала подключись к серверу.")
        return

    data = json.dumps({
        "ping": True,
        "user_id": user_id
    })

    future = asyncio.run_coroutine_threadsafe(ws_client.send(data), loop)

    try:
        future.result(timeout=5)
        start_ping_cooldown()
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось отправить ping:\n{e}")


send_button.config(command=send_message)
ping_button.config(command=send_ping)
entry.bind("<Return>", lambda event: send_message())

# -------------------------
# HTTP: регистрация и логин
# -------------------------
def register_user():
    uname = login_username_entry.get().strip()
    pwd = login_password_entry.get().strip()

    if not uname or not pwd:
        messagebox.showwarning("Ошибка", "Введите логин и пароль.")
        return

    try:
        r = requests.post(
            f"{HTTP_URL}/register",
            json={"username": uname, "password": pwd},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()

        if not data.get("success"):
            messagebox.showerror("Ошибка", data.get("message", "Не удалось зарегистрироваться"))
            return

        messagebox.showinfo("Успех", "Регистрация прошла успешно!\nТеперь войди через кнопку 'Войти'.")

    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось зарегистрироваться:\n{e}")


def login_user():
    global username, user_id, has_logged_in, manual_close

    uname = login_username_entry.get().strip()
    pwd = login_password_entry.get().strip()

    if not uname or not pwd:
        messagebox.showwarning("Ошибка", "Введите логин и пароль.")
        return

    try:
        r = requests.post(
            f"{HTTP_URL}/login",
            json={"username": uname, "password": pwd},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()

        if not data.get("success"):
            messagebox.showerror("Ошибка", data.get("message", "Неверный логин или пароль"))
            return

        username = data["username"]
        user_id = data["user_id"]
        has_logged_in = True
        manual_close = False

        messagebox.showinfo("Успех", f"Вход выполнен!\nПользователь: {username}")
        start_websocket()

    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось войти:\n{e}")


login_button.config(command=login_user)
register_button.config(command=register_user)

# -------------------------
# WebSocket
# -------------------------
async def handle_connection():
    global ws_client, connected, username, user_id

    async with websockets.connect(SERVER_URL) as websocket:
        ws_client = websocket
        connected = True

        await websocket.send(json.dumps({
            "username": username,
            "user_id": user_id
        }))

        set_status(f"подключен как {username}")
        set_chat_enabled(True)
        set_auth_enabled(False)
        focus_message_entry()

        while True:
            msg = await websocket.recv()

            try:
                data = json.loads(msg)

                if isinstance(data, dict):
                    if data.get("type") == "ping":
                        if data.get("user_id") != user_id:
                            root.after(0, show_popup)
                        continue

                    if data.get("type") == "online_status":
                        update_user_lists(data)
                        continue

                    if data.get("type") == "chat_message":
                        key = chat_message_key(data)
                        if key in seen_chat_messages:
                            continue
                        seen_chat_messages.add(key)

                        formatted = format_chat_message(
                            data.get("username", "Unknown"),
                            data.get("text", ""),
                            data.get("created_at")
                        )
                        add_chat_message(formatted)
                        continue

                    if data.get("type") == "system_message":
                        key = system_message_key(data)
                        if key in seen_system_messages:
                            continue
                        seen_system_messages.add(key)

                        formatted = format_system_message(
                            data.get("text", ""),
                            data.get("created_at")
                        )
                        add_system_message(formatted)
                        continue

            except Exception:
                pass

            add_chat_message(msg)


async def listen():
    global connected, ws_client

    first_attempt = True

    while not manual_close and has_logged_in:
        try:
            if first_attempt:
                set_status("подключение...")
            else:
                set_status(f"переподключение через {RECONNECT_DELAY_SECONDS} сек...")
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                if manual_close:
                    break
                set_status("переподключение...")

            await handle_connection()
            break

        except Exception:
            connected = False
            ws_client = None
            reset_ping_cooldown()
            set_chat_enabled(False)

            if manual_close or not has_logged_in:
                break

            first_attempt = False
            continue

    if not manual_close and not connected and has_logged_in:
        set_status("соединение потеряно")
        set_auth_enabled(True)


def start_loop():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(listen())


def start_websocket():
    global loop_thread, connected

    if connected:
        return

    set_status("запуск подключения...")
    set_chat_enabled(False)
    set_auth_enabled(False)

    loop_thread = threading.Thread(target=start_loop, daemon=True)
    loop_thread.start()

# -------------------------
# Закрытие приложения
# -------------------------
def on_close():
    global loop, ws_client, manual_close
    manual_close = True

    try:
        if ws_client and loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(ws_client.close(), loop)
    except Exception:
        pass

    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)

set_chat_enabled(False)
set_auth_enabled(True)

root.mainloop()
