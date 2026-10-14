import json
import struct

LEN = struct.Struct("!I")

def encode(obj: dict) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return LEN.pack(len(payload)) + payload

async def read_frame(reader):
    try:
        header = await reader.readexactly(4)
    except Exception:
        return None
    (size,) = LEN.unpack(header)
    try:
        payload = await reader.readexactly(size)
    except Exception:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return None

async def write_frame(writer, obj: dict):
    writer.write(encode(obj))
    await writer.drain()