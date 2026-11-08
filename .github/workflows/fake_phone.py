"""
Stand-in for the Android app: streams a synthetic drive (gentle sways and braking) to the overlay,
so the PC side can be tested without a phone.

  python fake_phone.py                    # to 127.0.0.1:8765 at 60 Hz
  python fake_phone.py 192.168.42.10 8765 100
"""
import math
import socket
import struct
import sys
import time

# Same layout as overlay.py and StreamService.kt.
HEADER = struct.Struct("<2sBBBBI")
FRAME = struct.Struct("<Q10f")

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
rate = int(sys.argv[3]) if len(sys.argv) > 3 else 60
FRAMES_PER_DATAGRAM = 2  # exercises the batched path the app uses

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.connect((host, port))
seq, t0 = 0, time.perf_counter()
period = 1.0 / rate
print(f"sending to {host}:{port} at {rate} Hz, Ctrl+C to stop")

try:
    while True:
        frames = b""
        for _ in range(FRAMES_PER_DATAGRAM):
            time.sleep(period)
            t = time.perf_counter() - t0
            lateral = 2.5 * math.sin(2 * math.pi * 0.25 * t)   # swaying left and right
            forward = 2.0 * math.sin(2 * math.pi * 0.13 * t)   # speeding up and braking
            # Phone lying flat, screen up: x is lateral, y is forward, gravity on +z, identity orientation.
            frames += FRAME.pack(int(t * 1e9), lateral, forward, 9.80665, 0, 0, 0, 0, 0, 0, 1)
        sock.send(HEADER.pack(b"MO", 1, 0b11, FRAMES_PER_DATAGRAM, 0, seq) + frames)
        seq = (seq + 1) & 0xFFFFFFFF
except (KeyboardInterrupt, ConnectionError):
    pass
