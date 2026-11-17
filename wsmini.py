"""Minimal stdlib WebSocket client (text frames, ping/pong, close). Client frames are masked per RFC 6455."""
import os, ssl, socket, struct, base64, json, urllib.parse, time

class WS:
    def __init__(self, url, timeout=30):
        u = urllib.parse.urlsplit(url)
        host, port = u.hostname, u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if u.scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: memedesk-paper/1.0\r\n\r\n")
        self.sock.sendall(req.encode())
        hdr = b""
        while b"\r\n\r\n" not in hdr:
            ch = self.sock.recv(1)
            if not ch: raise ConnectionError("handshake closed")
            hdr += ch
            if len(hdr) > 16384: raise ConnectionError("handshake too long")
        if b" 101 " not in hdr.split(b"\r\n", 1)[0]: raise ConnectionError("handshake: " + hdr.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.buf = b""; self.parts = []; self.rx_bytes = 0; self.last_rx = time.time()
    def _send(self, op, payload=b""):
        m = os.urandom(4); n = len(payload)
        h = bytes([0x80 | op])
        if n < 126: h += bytes([0x80 | n])
        elif n < 65536: h += bytes([0x80 | 126]) + struct.pack(">H", n)
        else: h += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(h + m + bytes(b ^ m[i % 4] for i, b in enumerate(payload)))
    def send(self, obj): self._send(0x1, json.dumps(obj).encode())
    def _parse(self):
        """One complete frame from the buffer, or None (buffer untouched) if it is not all here yet."""
        buf = self.buf
        if len(buf) < 2: return None
        b0, b1 = buf[0], buf[1]; n = b1 & 0x7F; i = 2
        if n == 126:
            if len(buf) < 4: return None
            n = struct.unpack(">H", buf[2:4])[0]; i = 4
        elif n == 127:
            if len(buf) < 10: return None
            n = struct.unpack(">Q", buf[2:10])[0]; i = 10
        if n > 8 * 1024 * 1024: raise ConnectionError("frame too large")
        mask = None
        if b1 & 0x80:
            if len(buf) < i + 4: return None
            mask = buf[i:i + 4]; i += 4
        if len(buf) < i + n: return None
        data = buf[i:i + n]; self.buf = buf[i + n:]
        if mask: data = bytes(b ^ mask[j % 4] for j, b in enumerate(data))
        return b0 & 0x80, b0 & 0x0F, data
    def ping(self): self._send(0x9, b"hb")
    def recv(self):
        """Next complete text/binary message (str). Handles ping/pong/close and fragmentation.
        Safe with a socket timeout: a timeout raises socket.timeout and keeps any partial frame buffered."""
        while True:
            f = self._parse()
            if f is None:
                ch = self.sock.recv(65536)
                if not ch: raise ConnectionError("socket closed")
                self.buf += ch; self.rx_bytes += len(ch); self.last_rx = time.time()
                continue
            fin, op, data = f
            if op == 0x9: self._send(0xA, data); continue
            if op == 0xA: continue
            if op == 0x8: raise ConnectionError("server close")
            self.parts.append(data)
            if fin:
                out = b"".join(self.parts); self.parts = []
                return out.decode(errors="replace")
    def close(self):
        try: self._send(0x8, b"")
        except Exception: pass
        try: self.sock.close()
        except Exception: pass
