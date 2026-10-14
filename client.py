"""
LAN Chat Client — modern two-pane UI with fixed layout.
Chat area always fills all remaining space, no matter the DPI or window size.

Run:
    python client.py
"""

import asyncio
import queue
import threading
import time
import tkinter as tk
from tkinter import simpledialog, messagebox

from protocol import read_frame, write_frame

# ------------------------------------------------------------------ config
SERVER_IP = "192.168.1.135"
PORT = 5555

try:
    from plyer import notification as desktop_notify
    HAS_NOTIFY = True
except Exception:
    HAS_NOTIFY = False

# ------------------------------------------------------------------ palette
BG          = "#0f0f14"
BG_PANEL    = "#16161e"
BG_INPUT    = "#1e1e2a"
BG_HOVER    = "#252533"
BG_SELECT   = "#2a2a3d"
FG          = "#e6e6f0"
FG_DIM      = "#7a7a8c"
FG_ACCENT   = "#8b9dff"
FG_GREEN    = "#6ee7a4"
FG_YELLOW   = "#f5c86b"
FG_LAVENDER = "#c4a7ff"
FG_SYSTEM   = "#5fb8d9"
FG_ADMIN    = "#ffb84d"
FG_SELF     = "#6ee7a4"
BORDER      = "#242433"


# ================================================================== network
class NetWorker(threading.Thread):
    def __init__(self, nick: str, ui_q: queue.Queue):
        super().__init__(daemon=True)
        self.nick = nick
        self.ui_q = ui_q
        self.loop = None
        self.out_q = None
        self._stop = False

    def submit(self, obj: dict):
        if self.loop and self.out_q:
            self.loop.call_soon_threadsafe(self.out_q.put_nowait, obj)

    def stop(self):
        self._stop = True
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: None)

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    async def _main(self):
        self.out_q = asyncio.Queue()
        backoff = 1.0
        while not self._stop:
            try:
                r, w = await asyncio.open_connection(SERVER_IP, PORT)
                await write_frame(w, {"type": "hello", "nick": self.nick})
                self.ui_q.put(("status", "connected"))
                backoff = 1.0

                sender = asyncio.create_task(self._sender(w))
                reader = asyncio.create_task(self._reader(r))
                await asyncio.wait({sender, reader}, return_when=asyncio.FIRST_COMPLETED)
                for t in (sender, reader):
                    t.cancel()
                try:
                    w.close()
                except Exception:
                    pass
            except Exception as e:
                self.ui_q.put(("system", f"connect failed: {e}"))

            self.ui_q.put(("status", "disconnected"))
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

    async def _reader(self, r):
        while True:
            frame = await read_frame(r)
            if frame is None:
                return
            self.ui_q.put(("frame", frame))

    async def _sender(self, w):
        while True:
            obj = await self.out_q.get()
            await write_frame(w, obj)


# ================================================================== UI
class ChatClient:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("LAN Chat")
        self.root.geometry("1000x700")
        self.root.minsize(720, 480)
        self.root.configure(bg=BG)

        self.nick = simpledialog.askstring("Nickname", "Enter your nickname:",
                                           parent=self.root) or "Anonymous"
        self.base_title = f"LAN Chat \u2014 {self.nick}"
        self.root.title(self.base_title)

        self.mode = "public"
        self.dm_with: str | None = None
        self.unread = 0
        self.users: list[dict] = []

        self.ui_q: queue.Queue = queue.Queue()
        self.net = NetWorker(self.nick, self.ui_q)
        self.net.start()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self.pump)

    # ------------------------------------------------------------ layout
    def _build_ui(self):
        # ============ 1. BOTTOM BAR FIRST (so it's always visible) ============
        bottom = tk.Frame(self.root, bg=BG_PANEL)
        bottom.pack(fill=tk.X, side=tk.BOTTOM)   # <-- pack FIRST, side=BOTTOM
        tk.Frame(bottom, bg=BORDER, height=1).pack(fill=tk.X)

        self.mode_lbl = tk.Label(bottom, text="MODE: PUBLIC ROOM",
                                 bg=BG_PANEL, fg=FG_GREEN,
                                 font=("Segoe UI", 9, "bold"),
                                 anchor="w", padx=18, pady=6)
        self.mode_lbl.pack(fill=tk.X)

        row = tk.Frame(bottom, bg=BG_PANEL)
        row.pack(fill=tk.X, padx=14, pady=(0, 12))

        self.entry = tk.Entry(row, bg=BG_INPUT, fg=FG,
                              insertbackground=FG_ACCENT,
                              font=("Consolas", 11),
                              relief=tk.FLAT, bd=0)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=9, padx=(4, 8))
        self.entry.bind("<Return>", lambda e: self.send())

        send = tk.Button(row, text="Send", command=self.send,
                         bg=FG_ACCENT, fg="#0f0f14",
                         activebackground="#a8b6ff", activeforeground="#0f0f14",
                         relief=tk.FLAT, bd=0, padx=22, pady=8,
                         font=("Segoe UI", 10, "bold"), cursor="hand2")
        send.pack(side=tk.RIGHT)

        # ============ 2. HEADER (top, fixed height) ============
        header = tk.Frame(self.root, bg=BG_PANEL, height=52)
        header.pack(fill=tk.X, side=tk.TOP)
        header.pack_propagate(False)

        tk.Label(header, text="LAN Chat", bg=BG_PANEL, fg=FG_ACCENT,
                 font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT, padx=18)

        tk.Label(header, text=f"@{self.nick}", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT)

        self.status_dot = tk.Label(header, text="\u25CF", bg=BG_PANEL, fg="#c87e7e",
                                   font=("Segoe UI", 12))
        self.status_dot.pack(side=tk.RIGHT, padx=(0, 6))
        self.status_lbl = tk.Label(header, text="connecting\u2026", bg=BG_PANEL,
                                   fg=FG_DIM, font=("Segoe UI", 9))
        self.status_lbl.pack(side=tk.RIGHT, padx=(0, 4))

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill=tk.X, side=tk.TOP)

        # ============ 3. BODY (fills everything between header and bottom) ============
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, side=tk.TOP)

        # right panel first (fixed width), so left expands
        right = tk.Frame(body, bg=BG_PANEL, width=230)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(5, 10), pady=(10, 10))
        right.pack_propagate(False)

        # left: chat area (expands to fill remaining space)
        left = tk.Frame(body, bg=BG)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 5), pady=(10, 10))

        # chat text widget — expands to fill left frame
        chat_wrap = tk.Frame(left, bg=BG_PANEL)
        chat_wrap.pack(fill=tk.BOTH, expand=True)

        self.chat = tk.Text(
            chat_wrap, wrap=tk.WORD, state="disabled",
            bg=BG_PANEL, fg=FG, font=("Consolas", 11),
            insertbackground=FG, borderwidth=0, padx=16, pady=12,
            spacing1=3, spacing3=3,
        )
        self.chat.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sb = tk.Scrollbar(chat_wrap, command=self.chat.yview, bg=BG_PANEL,
                          troughcolor=BG, bd=0, highlightthickness=0,
                          activebackground=BG_HOVER)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat.config(yscrollcommand=sb.set)

        self.chat.tag_config("system",  foreground=FG_SYSTEM,
                             font=("Consolas", 10, "italic"))
        self.chat.tag_config("ts",      foreground=FG_DIM)
        self.chat.tag_config("public",  foreground=FG)
        self.chat.tag_config("me",      foreground=FG_SELF,
                             font=("Consolas", 11, "bold"))
        self.chat.tag_config("private", foreground=FG_YELLOW,
                             font=("Consolas", 11, "bold"))
        self.chat.tag_config("dm",      foreground=FG_LAVENDER)
        self.chat.tag_config("dm_me",   foreground=FG_SELF,
                             font=("Consolas", 11, "bold"))
        self.chat.tag_config("admin",   foreground=FG_ADMIN,
                             font=("Consolas", 11, "bold"))

        # ----- right panel contents -----
        ph = tk.Frame(right, bg=BG_PANEL)
        ph.pack(fill=tk.X, padx=12, pady=(12, 6))

        tk.Label(ph, text="ONLINE NOW", bg=BG_PANEL,
                 fg=FG_DIM, font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT)
        self.users_count = tk.Label(ph, text="0", bg=BG_PANEL, fg=FG_ACCENT,
                                    font=("Segoe UI", 8, "bold"))
        self.users_count.pack(side=tk.RIGHT)

        # Public room row
        self.public_row = tk.Frame(right, bg=BG_SELECT, cursor="hand2")
        self.public_row.pack(fill=tk.X, padx=8, pady=(0, 4))
        self.public_lbl = tk.Label(self.public_row, text="\U0001F30D  Public Room",
                                   bg=BG_SELECT, fg=FG, anchor="w",
                                   font=("Segoe UI", 10, "bold"),
                                   padx=10, pady=7)
        self.public_lbl.pack(fill=tk.X)
        for w in (self.public_row, self.public_lbl):
            w.bind("<Button-1>", lambda e: self.set_mode_public())
            w.bind("<Enter>", lambda e: self._hover(self.public_row, True))
            w.bind("<Leave>", lambda e: self._hover(self.public_row, False))

        tk.Frame(right, bg=BORDER, height=1).pack(fill=tk.X, padx=12, pady=6)

        # user list container
        list_wrap = tk.Frame(right, bg=BG_PANEL)
        list_wrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.user_canvas = tk.Canvas(list_wrap, bg=BG_PANEL,
                                     highlightthickness=0, bd=0)
        self.user_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        nav = tk.Frame(list_wrap, bg=BG_PANEL)
        nav.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 0))

        self.up_btn = tk.Button(nav, text="\u25B2", command=lambda: self._scroll(-1),
                                bg=BG_PANEL, fg=FG_DIM, activebackground=BG_HOVER,
                                activeforeground=FG, relief=tk.FLAT, bd=0,
                                font=("Segoe UI", 9), width=2, cursor="hand2")
        self.up_btn.pack(pady=(0, 2))

        self.down_btn = tk.Button(nav, text="\u25BC", command=lambda: self._scroll(1),
                                  bg=BG_PANEL, fg=FG_DIM, activebackground=BG_HOVER,
                                  activeforeground=FG, relief=tk.FLAT, bd=0,
                                  font=("Segoe UI", 9), width=2, cursor="hand2")
        self.down_btn.pack()

        self.user_inner = tk.Frame(self.user_canvas, bg=BG_PANEL)
        self.user_canvas.create_window((0, 0), window=self.user_inner, anchor="nw")
        self.user_inner.bind(
            "<Configure>",
            lambda e: self.user_canvas.configure(
                scrollregion=self.user_canvas.bbox("all")))
        self.user_canvas.bind(
            "<Configure>",
            lambda e: self.user_canvas.itemconfig(
                self.user_canvas.find_all()[0], width=e.width))
        self.user_canvas.bind_all("<MouseWheel>", self._on_wheel)

        self.append(time.time(), "system",
                    f"[SYSTEM] Connecting to {SERVER_IP}:{PORT}\u2026")

    def _hover(self, widget, on):
        widget.config(bg=BG_HOVER if on else BG_SELECT)

    # ------------------------------------------------------------ user list
    def _render_users(self):
        for w in self.user_inner.winfo_children():
            w.destroy()

        ordered = sorted(self.users,
                         key=lambda u: (u["nick"] != "Admin", u["nick"].lower()))
        self.users_count.config(
            text=str(len([u for u in ordered if u["nick"] != self.nick]))
        )

        for u in ordered:
            nick = u["nick"]
            if nick == self.nick:
                continue
            self._make_user_row(nick, u.get("dm_with"), u.get("private"))

        self.user_canvas.update_idletasks()
        self.user_canvas.configure(scrollregion=self.user_canvas.bbox("all"))

    def _make_user_row(self, nick, dm_with, waiting):
        is_admin = (nick == "Admin")
        active = ((self.mode == "dm" and self.dm_with == nick) or
                  (self.mode == "help" and is_admin))

        bg = BG_SELECT if active else BG_PANEL
        fg = FG_ADMIN if is_admin else FG

        row = tk.Frame(self.user_inner, bg=bg, cursor="hand2")
        row.pack(fill=tk.X, pady=1)

        dot_color = FG_GREEN if (active or dm_with) else "#4a4a5c"
        tk.Label(row, text="\u25CF", bg=bg, fg=dot_color,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(8, 4), pady=6)

        label = f"\U0001F510 {nick}" if is_admin else nick
        tk.Label(row, text=label, bg=bg, fg=fg,
                 font=("Segoe UI", 10, "bold" if is_admin else "normal"),
                 anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)

        if waiting:
            tk.Label(row, text="\U0001F514", bg=bg, fg=FG_YELLOW,
                     font=("Segoe UI", 9)).pack(side=tk.RIGHT, padx=6)

        def enter(_e, r=row, act=active):
            if not act:
                r.config(bg=BG_HOVER)
                for child in r.winfo_children():
                    try:
                        child.config(bg=BG_HOVER)
                    except Exception:
                        pass

        def leave(_e, r=row, act=active):
            if not act:
                r.config(bg=BG_PANEL)
                for child in r.winfo_children():
                    try:
                        child.config(bg=BG_PANEL)
                    except Exception:
                        pass

        def click(_e, n=nick):
            if n == "Admin":
                self.toggle_help_mode()
            else:
                self.start_dm(n)

        for widget in [row] + list(row.winfo_children()):
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)
            widget.bind("<Button-1>", click)

    def _scroll(self, direction):
        self.user_canvas.yview_scroll(direction * 2, "units")

    def _on_wheel(self, event):
        x, y = event.x_root, event.y_root
        wx = self.user_canvas.winfo_rootx()
        wy = self.user_canvas.winfo_rooty()
        ww = self.user_canvas.winfo_width()
        wh = self.user_canvas.winfo_height()
        if wx <= x <= wx + ww and wy <= y <= wy + wh:
            self.user_canvas.yview_scroll(-1 * (event.delta // 120), "units")

    # ------------------------------------------------------------ modes
    def set_mode_public(self):
        if self.mode == "dm":
            self.net.submit({"type": "dm_close"})
        elif self.mode == "help":
            self.net.submit({"type": "help_close"})
        self.mode = "public"
        self.dm_with = None
        self.mode_lbl.config(text="MODE: PUBLIC ROOM", fg=FG_GREEN)
        self._render_users()

    def toggle_help_mode(self):
        if self.mode == "help":
            self.set_mode_public()
        else:
            if self.mode == "dm":
                self.net.submit({"type": "dm_close"})
            self.net.submit({"type": "help_open"})
            self.mode = "help"
            self.dm_with = None
            self.mode_lbl.config(text="MODE: PRIVATE CHAT WITH ADMIN", fg=FG_YELLOW)
            self._render_users()

    def start_dm(self, target: str):
        if target == self.nick:
            return
        if self.mode == "help":
            self.net.submit({"type": "help_close"})
        self.net.submit({"type": "dm_open", "to": target})
        self.mode = "dm"
        self.dm_with = target
        self.mode_lbl.config(text=f"MODE: DM WITH {target.upper()}", fg=FG_LAVENDER)
        self._render_users()
        self.entry.focus_set()

    # ------------------------------------------------------------ send
    def send(self):
        text = self.entry.get().strip()
        if not text:
            return

        if self.mode == "help":
            self.net.submit({"type": "help", "text": text})
            self.append(time.time(), "private", f"you \u2192 Admin: {text}")
        elif self.mode == "dm" and self.dm_with:
            self.net.submit({"type": "dm_send", "to": self.dm_with, "text": text})
            self.append(time.time(), "dm_me", f"you \u2192 {self.dm_with}: {text}")
        else:
            self.net.submit({"type": "chat", "text": text})
            self.append(time.time(), "me", f"you: {text}")

        self.entry.delete(0, tk.END)

    def append(self, ts, tag, text):
        clock = time.strftime("%H:%M", time.localtime(ts))
        self.chat.config(state="normal")
        self.chat.insert(tk.END, f"[{clock}] ", "ts")
        self.chat.insert(tk.END, text + "\n", tag)
        self.chat.see(tk.END)
        self.chat.config(state="disabled")

    # ------------------------------------------------------------ pump
    def pump(self):
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                if kind == "status":
                    connected = (payload == "connected")
                    self.status_lbl.config(text=payload)
                    self.status_dot.config(fg=FG_GREEN if connected else "#c87e7e")
                elif kind == "system":
                    self.append(time.time(), "system", f"[SYSTEM] {payload}")
                elif kind == "frame":
                    self.handle_frame(payload)
        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    def handle_frame(self, f: dict):
        t = f.get("type")
        ts = f.get("ts", time.time())

        if t == "welcome":
            self.append(ts, "system", f"[SYSTEM] Connected. You are '{f['nick']}'.")
        elif t == "system":
            self.append(ts, "system", f"[SERVER] {f['text']}")
        elif t == "chat":
            who = f.get("from", "?")
            tag = "admin" if who == "Admin" else "public"
            self.append(ts, tag, f"{who}: {f['text']}")
        elif t == "help":
            self.append(ts, "private", f"[PRIVATE from {f['from']}] {f['text']}")
            if not f.get("echo"):
                self.notify_private("Admin")
        elif t == "dm":
            sender = f.get("from", "?")
            to = f.get("to", "?")
            if f.get("admin"):
                self.append(ts, "admin",
                            f"[ADMIN into DM {sender}\u2194{to}] {f['text']}")
            elif f.get("echo"):
                self.append(ts, "dm_me", f"you \u2192 {to}: {f['text']}")
            else:
                self.append(ts, "dm", f"[DM from {sender}] {f['text']}")
                self.notify_private(sender)
                self.dm_with = sender
                self.mode = "dm"
                self.mode_lbl.config(
                    text=f"MODE: DM WITH {sender.upper()}", fg=FG_LAVENDER)
                self._render_users()
        elif t == "user_list":
            self.users = f.get("users", [])
            self._render_users()
        elif t == "error":
            self.append(ts, "system", f"[ERROR] {f['text']}")

    # ------------------------------------------------------------ notify
    def notify_private(self, who: str):
        self.unread += 1
        self.root.title(f"({self.unread}) \U0001F514 {self.base_title}")
        try:
            self.root.bell()
        except Exception:
            pass
        if HAS_NOTIFY:
            try:
                desktop_notify.notify(title=who,
                                      message="Open LAN Chat to read it.",
                                      timeout=5)
            except Exception:
                pass

    # ------------------------------------------------------------ close
    def on_close(self):
        try:
            self.net.stop()
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


# ================================================================== main
if __name__ == "__main__":
    try:
        ChatClient().run()
    except Exception as e:
        try:
            messagebox.showerror("Fatal", str(e))
        except Exception:
            print("Fatal:", e)