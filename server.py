"""
LAN Chat Server with built-in Admin GUI.

- asyncio chat relay runs in a background thread
- Tkinter admin GUI runs on the main thread
- They communicate through a queue.Queue (no threads touching Tk directly)

Features:
  - Public room chat (everyone)
  - Private help: student <-> Admin
  - Direct messages: student <-> student (server-relayed)
  - Admin sees ALL DMs in the log and can inject messages
  - Live user list broadcast to every connected client
  - Unread badges + beep + optional desktop notifications
  - Rate limiting, nickname collision handling
  - Rotating log: server.log
  - Reserved 'file_send' frame for future file sharing

Requires protocol.py in the same folder.
Optional: pip install plyer

Run:
    python server.py
"""

import asyncio
import logging
import queue
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import messagebox, scrolledtext

from protocol import read_frame, write_frame

# ------------------------------------------------------------------ config
HOST = "0.0.0.0"
PORT = 5555
ADMIN_NAME = "Admin"
RATE_BURST = 20.0
RATE_REFILL = 5.0
MAX_NICK_LEN = 20

# ------------------------------------------------------------------ logging
logger = logging.getLogger("chat")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

_fh = RotatingFileHandler("server.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

try:
    from plyer import notification as desktop_notify
    HAS_NOTIFY = True
except Exception:
    HAS_NOTIFY = False


# ================================================================== SERVER
class Client:
    __slots__ = ("reader", "writer", "nick", "private", "dm_with",
                 "tokens", "last_refill")

    def __init__(self, reader, writer, nick):
        self.reader = reader
        self.writer = writer
        self.nick = nick
        self.private = False           # True = in admin-help mode
        self.dm_with: str | None = None    # nick of current DM partner
        self.tokens = RATE_BURST
        self.last_refill = time.monotonic()

    async def send(self, obj: dict):
        try:
            await write_frame(self.writer, obj)
        except Exception:
            await self.close()

    async def close(self):
        try:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        except Exception:
            pass


class ChatServer:
    def __init__(self, gui_q: queue.Queue):
        self.clients: set[Client] = set()
        self.by_nick: dict[str, Client] = {}
        self.lock = asyncio.Lock()
        self.gui_q = gui_q
        self._loop = None

    # ---------------- helpers ----------------
    def allow(self, c: Client) -> bool:
        now = time.monotonic()
        c.tokens = min(RATE_BURST, c.tokens + (now - c.last_refill) * RATE_REFILL)
        c.last_refill = now
        if c.tokens < 1.0:
            return False
        c.tokens -= 1.0
        return True

    async def broadcast(self, obj: dict, exclude: Client | None = None):
        targets = [c for c in self.clients if c is not exclude]
        if targets:
            await asyncio.gather(*(c.send(obj) for c in targets), return_exceptions=True)
        self.gui_q.put(("event", obj))

    async def send_user_list(self):
        async with self.lock:
            users = [
                {"nick": c.nick, "private": c.private, "dm_with": c.dm_with}
                for c in self.clients
            ]
        # update the admin GUI panel
        self.gui_q.put(("user_list", users))
        # also broadcast to every student client
        payload = {"type": "user_list", "users": users}
        for c in list(self.clients):
            await c.send(payload)

    # ---------------- per-client ----------------
    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        logger.info("NEW %s", peer)

        try:
            hello = await asyncio.wait_for(read_frame(reader), timeout=5.0)
        except asyncio.TimeoutError:
            writer.close()
            return
        if not hello or hello.get("type") != "hello":
            writer.close()
            return

        raw_nick = (hello.get("nick") or "Anonymous").strip()[:MAX_NICK_LEN] or "Anonymous"

        async with self.lock:
            nick = raw_nick
            if nick in self.by_nick:
                nick = f"{raw_nick}_{int(time.time()) % 10000}"
            c = Client(reader, writer, nick)
            self.clients.add(c)
            self.by_nick[nick] = c

        await c.send({"type": "welcome", "nick": nick})
        await self.broadcast({"type": "system", "text": f"{nick} joined"}, exclude=c)
        await self.send_user_list()
        logger.info("NICK %s -> %s", peer, nick)

        try:
            while True:
                frame = await read_frame(reader)
                if frame is None:
                    break
                if not self.allow(c):
                    await c.send({"type": "error", "text": "rate limit exceeded"})
                    continue
                await self._route(c, frame)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            logger.exception("client error: %s", e)
        finally:
            async with self.lock:
                self.clients.discard(c)
                if self.by_nick.get(c.nick) is c:
                    del self.by_nick[c.nick]
                # clear any dm_with pointing to this user
                for other in self.clients:
                    if other.dm_with == c.nick:
                        other.dm_with = None
            await c.close()
            await self.broadcast({"type": "system", "text": f"{nick} left"})
            await self.send_user_list()
            logger.info("BYE %s", nick)

    async def _route(self, c: Client, frame: dict):
        ftype = frame.get("type")
        text = (frame.get("text") or "").strip()
        ts = time.time()

        # ---------------- public ----------------
        if ftype == "chat":
            if not text:
                return
            if c.private or c.dm_with:
                await c.send({"type": "error",
                              "text": "You are in a private chat. Switch back to public first."})
                return
            await self.broadcast(
                {"type": "chat", "from": c.nick, "text": text, "ts": ts}, exclude=c
            )

        # ---------------- admin-help mode ----------------
        elif ftype == "help_open":
            c.private = True
            c.dm_with = None
            await c.send({"type": "system",
                          "text": "You are now in PRIVATE mode with the Admin."})
            self.gui_q.put(("help_event", {"from": c.nick, "text": "(entered admin help)"}))
            await self.send_user_list()

        elif ftype == "help_close":
            c.private = False
            await c.send({"type": "system", "text": "You are back in the PUBLIC chat."})
            await self.send_user_list()

        elif ftype == "help":
            if not text:
                return
            if not c.private:
                await c.send({"type": "error", "text": "Click 'Private (Admin)' first."})
                return
            await c.send({"type": "help", "from": ADMIN_NAME, "text": text, "ts": ts, "echo": True})
            self.gui_q.put(("help_in", {"from": c.nick, "text": text, "ts": ts}))

        # ---------------- DMs student <-> student ----------------
        elif ftype == "dm_open":
            to = (frame.get("to") or "").strip()
            if not to:
                await c.send({"type": "error", "text": "dm_open requires 'to'"})
                return
            if to == c.nick:
                await c.send({"type": "error", "text": "You can't DM yourself."})
                return
            async with self.lock:
                target = self.by_nick.get(to)
            if not target:
                await c.send({"type": "error", "text": f"No user named '{to}'"})
                return
            c.dm_with = to
            c.private = False
            await c.send({"type": "system",
                          "text": f"DM started with {to}. Click 'Public Room' to leave."})
            await target.send({
                "type": "system",
                "text": f"{c.nick} wants to DM you. Reply will reach only them.",
                "ts": ts,
            })
            self.gui_q.put(("dm_event", {"a": c.nick, "b": to, "kind": "open"}))
            await self.send_user_list()

        elif ftype == "dm_close":
            if c.dm_with:
                other = c.dm_with
                c.dm_with = None
                await c.send({"type": "system", "text": "DM closed."})
                async with self.lock:
                    t = self.by_nick.get(other)
                if t and t.dm_with == c.nick:
                    t.dm_with = None
                self.gui_q.put(("dm_event", {"a": c.nick, "b": other, "kind": "close"}))
                await self.send_user_list()

        elif ftype == "dm_send":
            to = (frame.get("to") or c.dm_with or "").strip()
            if not to:
                await c.send({"type": "error", "text": "No DM recipient."})
                return
            if not text:
                return
            async with self.lock:
                target = self.by_nick.get(to)
            if not target:
                await c.send({"type": "error", "text": f"No user named '{to}'"})
                return

            c.dm_with = to
            target.dm_with = c.nick
            c.private = False

            await target.send({
                "type": "dm", "from": c.nick, "to": to,
                "text": text, "ts": ts,
            })
            await c.send({
                "type": "dm", "from": c.nick, "to": to,
                "text": text, "ts": ts, "echo": True,
            })
            self.gui_q.put(("dm_in", {"from": c.nick, "to": to, "text": text, "ts": ts}))
            await self.send_user_list()

        # ---------------- future: file transfer ----------------
        elif ftype == "file_send":
            await c.send({"type": "error",
                          "text": "File sharing is not enabled yet."})

        else:
            await c.send({"type": "error", "text": f"unknown frame: {ftype}"})

    # ---------------- admin actions (called from GUI thread) ----------------
    def submit_from_gui(self, obj: dict):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._process_admin(obj), self._loop)

    async def _process_admin(self, obj: dict):
        ftype = obj.get("type")
        ts = time.time()

        if ftype == "admin_chat":
            text = (obj.get("text") or "").strip()
            if not text:
                return
            await self.broadcast({"type": "chat", "from": ADMIN_NAME, "text": text, "ts": ts})

        elif ftype == "admin_help":
            to = (obj.get("to") or "").strip()
            text = (obj.get("text") or "").strip()
            if not to or not text:
                return
            async with self.lock:
                target = self.by_nick.get(to)
            if not target:
                self.gui_q.put(("error", f"No user named '{to}'"))
                return
            await target.send({"type": "help", "from": ADMIN_NAME, "text": text, "ts": ts})
            self.gui_q.put(("sent_help", {"to": to, "text": text, "ts": ts}))

        elif ftype == "admin_dm":
            a = (obj.get("a") or "").strip()
            b = (obj.get("b") or "").strip()
            text = (obj.get("text") or "").strip()
            if not a or not b or not text:
                return
            async with self.lock:
                ca = self.by_nick.get(a)
                cb = self.by_nick.get(b)
            payload_a = {"type": "dm", "from": ADMIN_NAME, "to": a,
                         "text": f"[into DM {a}\u2194{b}] {text}", "ts": ts, "admin": True}
            payload_b = {"type": "dm", "from": ADMIN_NAME, "to": b,
                         "text": f"[into DM {a}\u2194{b}] {text}", "ts": ts, "admin": True}
            if ca:
                await ca.send(payload_a)
            if cb:
                await cb.send(payload_b)
            self.gui_q.put(("sent_dm", {"a": a, "b": b, "text": text, "ts": ts}))

    # ---------------- entrypoint ----------------
    def run(self):
        asyncio.run(self._serve())

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        srv = await asyncio.start_server(self.handle, HOST, PORT)
        addrs = ", ".join(str(s.getsockname()) for s in srv.sockets)
        logger.info("LISTENING on %s", addrs)
        self.gui_q.put(("server_ready", addrs))
        async with srv:
            await srv.serve_forever()


# ================================================================== ADMIN GUI
class AdminGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("LAN Chat \u2014 Admin")
        self.root.geometry("1020x760")
        self.root.minsize(820, 600)
        self.root.configure(bg="#1e1e1e")

        self.selected_nick: str | None = None
        self.selected_dm: tuple[str, str] | None = None
        self.unread_for: dict[str, int] = {}
        self.unread_dm: dict[tuple[str, str], int] = {}
        self.dm_pairs: set[tuple[str, str]] = set()
        self._last_users: list[dict] = []

        self.gui_q: queue.Queue = queue.Queue()
        self.server = ChatServer(self.gui_q)
        threading.Thread(target=self.server.run, daemon=True).start()

        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self.pump)

    # ------------------------------------------------------------ layout
    def build_ui(self):
        header = tk.Frame(self.root, bg="#1e1e1e")
        header.pack(fill=tk.X, padx=12, pady=(10, 0))
        tk.Label(header, text="ADMIN CONSOLE", bg="#1e1e1e", fg="#ffcc66",
                 font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT)
        self.status_lbl = tk.Label(header, text="starting\u2026", bg="#1e1e1e",
                                   fg="#888", font=("Segoe UI", 9))
        self.status_lbl.pack(side=tk.RIGHT)

        body = tk.Frame(self.root, bg="#1e1e1e")
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        # ---------- left: students ----------
        left = tk.Frame(body, bg="#1e1e1e", width=220)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        left.pack_propagate(False)
        tk.Label(left, text="Students", bg="#1e1e1e", fg="#aaa",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        lw = tk.Frame(left, bg="#252526")
        lw.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.user_list = tk.Listbox(
            lw, bg="#252526", fg="#e0e0e0",
            selectbackground="#0e639c", selectforeground="white",
            highlightthickness=0, borderwidth=0,
            font=("Consolas", 11), activestyle="none",
        )
        self.user_list.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.user_list.bind("<<ListboxSelect>>", self.on_user_select)
        tk.Label(left, text="\U0001F514 = waiting  \u2022  (DM) = in DM",
                 bg="#1e1e1e", fg="#888", font=("Segoe UI", 8)).pack(anchor="w")

        # ---------- middle: DM pairs ----------
        mid = tk.Frame(body, bg="#1e1e1e", width=220)
        mid.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        mid.pack_propagate(False)
        tk.Label(mid, text="Active DMs", bg="#1e1e1e", fg="#aaa",
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        mw = tk.Frame(mid, bg="#252526")
        mw.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.dm_list = tk.Listbox(
            mw, bg="#252526", fg="#c8c8ff",
            selectbackground="#4a2d8a", selectforeground="white",
            highlightthickness=0, borderwidth=0,
            font=("Consolas", 11), activestyle="none",
        )
        self.dm_list.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.dm_list.bind("<<ListboxSelect>>", self.on_dm_select)
        tk.Label(mid, text="click a pair to inject a message",
                 bg="#1e1e1e", fg="#888", font=("Segoe UI", 8)).pack(anchor="w")

        # ---------- right: chat + inputs ----------
        right = tk.Frame(body, bg="#1e1e1e")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.chat = scrolledtext.ScrolledText(
            right, wrap=tk.WORD, state="disabled",
            bg="#252526", fg="#e0e0e0", font=("Consolas", 11),
            insertbackground="white", borderwidth=0,
        )
        self.chat.pack(fill=tk.BOTH, expand=True)

        self.chat.tag_config("system",  foreground="#66ccff", font=("Consolas", 10, "italic"))
        self.chat.tag_config("public",  foreground="#e0e0e0")
        self.chat.tag_config("private", foreground="#ffcc66", font=("Consolas", 11, "bold"))
        self.chat.tag_config("dm",      foreground="#c8c8ff", font=("Consolas", 11))
        self.chat.tag_config("dm_self", foreground="#8aff8a", font=("Consolas", 11, "bold"))
        self.chat.tag_config("me",      foreground="#8aff8a")
        self.chat.tag_config("ts",      foreground="#666")

        # public input
        pub = tk.Frame(right, bg="#1e1e1e")
        pub.pack(fill=tk.X, pady=(8, 0))
        tk.Label(pub, text="Public:", bg="#1e1e1e", fg="#7ec87e",
                 font=("Segoe UI", 10, "bold"), width=8, anchor="w").pack(side=tk.LEFT)
        self.pub_entry = tk.Entry(pub, bg="#3c3c3c", fg="white",
                                  insertbackground="white", font=("Consolas", 11))
        self.pub_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(4, 6))
        self.pub_entry.bind("<Return>", lambda e: self.send_public())
        tk.Button(pub, text="Broadcast", command=self.send_public,
                  bg="#2d7d2d", fg="white", activebackground="#3aa03a",
                  relief=tk.FLAT, padx=14).pack(side=tk.RIGHT, ipady=4)

        # private to a student
        priv = tk.Frame(right, bg="#1e1e1e")
        priv.pack(fill=tk.X, pady=(6, 0))
        tk.Label(priv, text="Private:", bg="#1e1e1e", fg="#ffcc66",
                 font=("Segoe UI", 10, "bold"), width=8, anchor="w").pack(side=tk.LEFT)
        self.priv_entry = tk.Entry(priv, bg="#3c3c3c", fg="white",
                                   insertbackground="white", font=("Consolas", 11))
        self.priv_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(4, 6))
        self.priv_entry.bind("<Return>", lambda e: self.send_private())
        self.priv_entry.insert(0, "select a student \u2192")
        self.priv_entry.config(state="disabled")
        tk.Button(priv, text="Reply", command=self.send_private,
                  bg="#8a2d2d", fg="white", activebackground="#a03a3a",
                  relief=tk.FLAT, padx=14).pack(side=tk.RIGHT, ipady=4)

        # inject into DM
        inj = tk.Frame(right, bg="#1e1e1e")
        inj.pack(fill=tk.X, pady=(6, 0))
        tk.Label(inj, text="Into DM:", bg="#1e1e1e", fg="#c8c8ff",
                 font=("Segoe UI", 10, "bold"), width=8, anchor="w").pack(side=tk.LEFT)
        self.dm_entry = tk.Entry(inj, bg="#3c3c3c", fg="white",
                                 insertbackground="white", font=("Consolas", 11))
        self.dm_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=(4, 6))
        self.dm_entry.bind("<Return>", lambda e: self.send_into_dm())
        self.dm_entry.insert(0, "select a DM pair \u2192")
        self.dm_entry.config(state="disabled")
        tk.Button(inj, text="Inject", command=self.send_into_dm,
                  bg="#4a2d8a", fg="white", activebackground="#5f3aad",
                  relief=tk.FLAT, padx=14).pack(side=tk.RIGHT, ipady=4)

        self.append(time.time(), "system", "[SYSTEM] Server starting\u2026")

    # ------------------------------------------------------------ selection
    def on_user_select(self, _evt=None):
        sel = self.user_list.curselection()
        if not sel:
            return
        raw = self.user_list.get(sel[0])
        nick = raw.replace("\U0001F514", "").strip()
        for suffix in (" (help)", " (DM)"):
            if nick.endswith(suffix):
                nick = nick[: -len(suffix)].strip()
        self.selected_nick = nick
        if nick in self.unread_for:
            del self.unread_for[nick]
            self.refresh_user_list()
        self.priv_entry.config(state="normal")
        self.priv_entry.delete(0, tk.END)
        self.priv_entry.focus_set()

    def on_dm_select(self, _evt=None):
        sel = self.dm_list.curselection()
        if not sel:
            return
        raw = self.dm_list.get(sel[0]).replace("\U0001F514", "").strip()
        if "\u2194" in raw:
            a, b = [p.strip() for p in raw.split("\u2194", 1)]
            key = tuple(sorted((a, b)))
            self.selected_dm = key
            if key in self.unread_dm:
                del self.unread_dm[key]
                self.refresh_dm_list()
            self.dm_entry.config(state="normal")
            self.dm_entry.delete(0, tk.END)
            self.dm_entry.focus_set()

    # ------------------------------------------------------------ sending
    def send_public(self):
        text = self.pub_entry.get().strip()
        if not text:
            return
        self.server.submit_from_gui({"type": "admin_chat", "text": text})
        self.append(time.time(), "me", f"you (public): {text}")
        self.pub_entry.delete(0, tk.END)

    def send_private(self):
        if not self.selected_nick:
            messagebox.showinfo("No recipient", "Click a student on the left first.")
            return
        text = self.priv_entry.get().strip()
        if not text:
            return
        self.server.submit_from_gui(
            {"type": "admin_help", "to": self.selected_nick, "text": text}
        )
        self.priv_entry.delete(0, tk.END)

    def send_into_dm(self):
        if not self.selected_dm:
            messagebox.showinfo("No DM", "Click an active DM pair first.")
            return
        text = self.dm_entry.get().strip()
        if not text:
            return
        a, b = self.selected_dm
        self.server.submit_from_gui({"type": "admin_dm", "a": a, "b": b, "text": text})
        self.dm_entry.delete(0, tk.END)

    # ------------------------------------------------------------ display
    def append(self, ts, tag, text):
        clock = time.strftime("%H:%M", time.localtime(ts))
        self.chat.config(state="normal")
        self.chat.insert(tk.END, f"[{clock}] ", "ts")
        self.chat.insert(tk.END, text + "\n", tag)
        self.chat.see(tk.END)
        self.chat.config(state="disabled")

    def refresh_user_list(self, users=None):
        if users is None:
            users = self._last_users
        self._last_users = users

        current_sel = self.selected_nick
        self.user_list.delete(0, tk.END)

        for u in users:
            nick = u["nick"]
            waiting = u.get("private")
            dm = u.get("dm_with")
            unread = self.unread_for.get(nick, 0)

            bell = "\U0001F514 " if (waiting or unread) else "   "
            suffix = ""
            if waiting:
                suffix = " (help)"
            elif dm:
                suffix = " (DM)"
            self.user_list.insert(tk.END, f"{bell}{nick}{suffix}")

        if current_sel:
            for i, u in enumerate(users):
                if u["nick"] == current_sel:
                    self.user_list.selection_set(i)
                    self.user_list.activate(i)
                    break

    def refresh_dm_list(self):
        current = self.selected_dm
        self.dm_list.delete(0, tk.END)
        for a, b in sorted(self.dm_pairs):
            unread = self.unread_dm.get((a, b), 0)
            bell = "\U0001F514 " if unread else "   "
            self.dm_list.insert(tk.END, f"{bell}{a} \u2194 {b}")
        if current:
            for i, pair in enumerate(sorted(self.dm_pairs)):
                if pair == current:
                    self.dm_list.selection_set(i)
                    self.dm_list.activate(i)
                    break

    # ------------------------------------------------------------ pump
    def pump(self):
        try:
            while True:
                kind, payload = self.gui_q.get_nowait()

                if kind == "server_ready":
                    self.status_lbl.config(text=f"listening on {payload}", fg="#7ec87e")
                    self.append(time.time(), "system", f"[SYSTEM] Server ready \u2014 {payload}")

                elif kind == "user_list":
                    self.refresh_user_list(payload)

                elif kind == "event":
                    self.on_event(payload)

                elif kind == "help_in":
                    self.on_help_in(payload)

                elif kind == "help_event":
                    self.append(time.time(), "system",
                                f"[HELP] {payload['from']} {payload['text']}")

                elif kind == "sent_help":
                    self.append(payload["ts"], "private",
                                f"[you \u2192 {payload['to']}] {payload['text']}")

                elif kind == "dm_event":
                    a, b = sorted((payload["a"], payload["b"]))
                    key = (a, b)
                    if payload["kind"] == "open":
                        self.dm_pairs.add(key)
                        self.append(time.time(), "system",
                                    f"[DM] {payload['a']} started a DM with {payload['b']}")
                    elif payload["kind"] == "close":
                        self.dm_pairs.discard(key)
                        self.append(time.time(), "system",
                                    f"[DM] {payload['a']} closed DM with {payload['b']}")
                        if self.selected_dm == key:
                            self.selected_dm = None
                            self.dm_entry.config(state="disabled")
                    self.refresh_dm_list()

                elif kind == "dm_in":
                    self.on_dm_in(payload)

                elif kind == "sent_dm":
                    a, b = payload["a"], payload["b"]
                    self.append(payload["ts"], "dm_self",
                                f"[you \u2192 {a}\u2194{b}] {payload['text']}")

                elif kind == "error":
                    self.append(time.time(), "system", f"[ERROR] {payload}")

        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    def on_event(self, f: dict):
        t = f.get("type")
        ts = f.get("ts", time.time())
        if t == "system":
            self.append(ts, "system", f"[SERVER] {f['text']}")
        elif t == "chat":
            who = f.get("from", "?")
            tag = "me" if who == ADMIN_NAME else "public"
            prefix = "you (public)" if who == ADMIN_NAME else who
            self.append(ts, tag, f"{prefix}: {f['text']}")

    def on_help_in(self, payload: dict):
        who = payload["from"]
        text = payload["text"]
        ts = payload.get("ts", time.time())
        self.append(ts, "private", f"[PRIVATE {who}] {text}")
        if self.selected_nick != who:
            self.unread_for[who] = self.unread_for.get(who, 0) + 1
            self.refresh_user_list()
            self.notify(f"Help from {who}")

    def on_dm_in(self, payload: dict):
        a, b = sorted((payload["from"], payload["to"]))
        key = (a, b)
        text = payload["text"]
        ts = payload.get("ts", time.time())

        self.dm_pairs.add(key)
        self.append(ts, "dm", f"[DM {a} \u2194 {b}] {payload['from']}: {text}")

        if self.selected_dm != key:
            self.unread_dm[key] = self.unread_dm.get(key, 0) + 1
            self.refresh_dm_list()
            self.notify(f"DM {payload['from']} \u2192 {payload['to']}")
        else:
            self.refresh_dm_list()

    def notify(self, title: str):
        try:
            self.root.bell()
        except Exception:
            pass
        if HAS_NOTIFY:
            try:
                desktop_notify.notify(
                    title=title,
                    message="Open the Admin console to read it.",
                    timeout=5,
                )
            except Exception:
                pass

    # ------------------------------------------------------------ close
    def on_close(self):
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ================================================================== main
if __name__ == "__main__":
    try:
        AdminGUI().run()
    except Exception as e:
        try:
            messagebox.showerror("Fatal", str(e))
        except Exception:
            print("Fatal:", e)