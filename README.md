# LAN Chat — Python Chat Platform

A modern LAN chat application built with Python's `asyncio` (server) and `Tkinter + asyncio` (client).
One machine runs the **server** (which also opens the **Admin GUI**), and any number of machines run the **client**.

Works on:
- **Local Wi-Fi / LAN** (e.g., `192.168.1.135`)
- **Tailscale VPN** (e.g., `100.85.143.54`) — chat across the internet
- **Localhost** for testing (`127.0.0.1`)

Ships as two standalone Windows `.exe` files — students don't need Python installed.

---

## Features

### Students
- **Modern two-pane UI** (dark theme):
  - Left: chat log with timestamps and colour coding
  - Right: live list of everyone online — click a name to start a DM
- **Public room chat** — everyone sees it
- **Direct Messages (DMs)** — student ↔ student, private, server-relayed
- **Private help channel** — student ↔ Admin only (for when they're stuck)
- **Notifications** — beep, unread badge in title bar, optional desktop popup (via `plyer`)
- **Auto-reconnect** with exponential backoff if the server blips
- **▲ / ▼ buttons** and mouse-wheel scrolling on the user list

### Admin (you)
- **Built-in Admin GUI** inside `server.py` — no separate app
- **Live student list** with status badges:
  - 🔔 = unread / waiting for help
  - `(help)` = student in private mode with you
  - `(DM)` = student currently DMing someone
- **Active DMs panel** — see every student↔student DM in real time
- **Three send options**:
  - **Public** → broadcast to everyone
  - **Private** → reply only to the selected student
  - **Into DM** → inject a message into a specific student↔student DM
- **Audit trail** — every public message, private message, and DM is mirrored in your log
- **Rotating server log** — `server.log` (5 MB × 5 backups)

### Server-side
- **asyncio** — one event loop, one coroutine per client (scales to thousands)
- **Length-prefixed JSON protocol** — safe for any message size
- **Rate limiting** — token bucket per client (20 burst, 5/sec refill)
- **Nickname collision handling** — auto-suffixes with a timestamp
- **Graceful error isolation** — one bad client can't crash the server
- **Reserved `file_send` frame** — architecture ready for future file sharing

---

## Project Structure
