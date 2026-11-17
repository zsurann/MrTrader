"""Minimal stdlib WebSocket client (text frames, ping/pong, close). Client frames are masked per RFC 6455."""
import os, ssl, socket, struct, base64, json, urllib.parse

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
        self.buf = b""
    def _send(self, op, payload=b""):
        m = os.urandom(4); n = len(payload)
        h = bytes([0x80 | op])
        if n < 126: h += bytes([0x80 | n])
        elif n < 65536: h += bytes([0x80 | 126]) + struct.pack(">H", n)
        else: h += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(h + m + bytes(b ^ m[i % 4] for i, b in enumerate(payload)))
    def send(self, obj): self._send(0x1, json.dumps(obj).encode())
    def _read(self, n):
        while len(self.buf) < n:
            ch = self.sock.recv(65536)
            if not ch: raise ConnectionError("socket closed")
            self.buf += ch
        out, self.buf = self.buf[:n], self.buf[n:]
        return out
    def recv(self):
        """Next complete text/binary message (str). Handles ping/pong/close and fragmentation."""
        parts = []
        while True:
            b0, b1 = self._read(2)
            fin, op, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126: n = struct.unpack(">H", self._read(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._read(8))[0]
            if n > 8 * 1024 * 1024: raise ConnectionError("frame too large")
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask: data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x9: self._send(0xA, data); continue
            if op == 0xA: continue
            if op == 0x8: raise ConnectionError("server close")
            parts.append(data)
            if fin: return b"".join(parts).decode(errors="replace")
    def close(self):
        try: self._send(0x8, b"")
        except Exception: pass
        try: self.sock.close()
        except Exception: pass
