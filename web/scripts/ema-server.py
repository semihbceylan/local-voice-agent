"""Loopback-only EMA bridge using the existing shared EMA installation.

Run with ~/ai-services/ema-tts/.venv/bin/python scripts/ema-server.py
No dependencies are installed by this project. Model loads lazily and stays warm.
"""
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

model = None
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Never log conversation text.

    def do_GET(self):
        if self.path != '/health':
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'ok': True, 'loaded': model is not None}).encode())

    def do_POST(self):
        global model
        if self.path != '/speak' or self.headers.get('Origin'):
            self.send_error(403)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 6000:
                raise ValueError()
            text = json.loads(self.rfile.read(size)).get('text')
            if not isinstance(text, str) or not 0 < len(text.strip()) <= 1500:
                raise ValueError()
        except (ValueError, TypeError):
            self.send_error(400)
            return
        try:
            with lock:
                from ema_lightning import EMA
                import numpy as np
                if model is None:
                    model = EMA(device='cpu')
                speech = model.say([text], speed=1, seed=42, sample_rate=48000)[0]
                samples = np.asarray(speech.audio, dtype=np.float32).reshape(-1)
                if not len(samples):
                    raise ValueError('Empty speech')
                output = io.BytesIO()
                with wave.open(output, 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(speech.sample_rate)
                    wav.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())
            self.send_response(200)
            self.send_header('Content-Type', 'audio/wav')
            self.send_header('Content-Length', str(len(output.getvalue())))
            self.end_headers()
            self.wfile.write(output.getvalue())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            print(f'EMA generation failed: {type(error).__name__}', flush=True)
            self.send_error(503, 'EMA unavailable')


if __name__ == '__main__':
    print('EMA bridge: http://127.0.0.1:8765 (CPU, lazy model loading)', flush=True)
    HTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
