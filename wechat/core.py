import binascii
import datetime
import json
import socketserver
import threading
import traceback
import typing
import uuid

import requests

from pyee.executor import ExecutorEventEmitter

from wechat.events import ALL_MESSAGE, WECHAT_CONNECT_MESSAGE
from wechat.utils import hook

from wechat.logger import logger


class ReqData:
    __response_message = None
    msg_type = 0
    request_data = None

    def __init__(self, msg_type, data):
        self.msg_type = msg_type
        self.request_data = data
        self.__wait_event = threading.Event()

    def wait_response(self, timeout=None):
        self.__wait_event.wait(timeout)
        return self.get_response_data()

    def on_response(self, message):
        self.__response_message = message
        self.__wait_event.set()

    def get_response_data(self):
        if self.__response_message is None:
            return None
        return self.__response_message["data"]


class RequestHandler(socketserver.BaseRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def handle(self):
        try:
            data = b""
            while True:
                chunk = self.request.recv(1024)
                data += chunk
                if len(chunk) == 0 or chunk[-2:] == b"0A":
                    break

            self.request.sendall("HTTP/1.1 200 OK".encode("utf-8"))
            self.request.close()

            header_str = data.split(b"\r\n\r\n")[0].decode("utf-8")
            headers = {}
            for line in header_str.splitlines():
                if ": " in line:
                    key, value = line.split(": ", 1)
                    headers[key] = value

            hex_data = data.split(b"\r\n\r\n", )[-1]
            wechat = getattr(self.server, "wechat")
            try:
                raw_data = binascii.unhexlify(hex_data).decode("utf-8").rstrip("\n")
            except UnicodeDecodeError:
                raw_data = binascii.unhexlify(hex_data).decode("gbk").rstrip("\n")
            event = json.loads(raw_data)
            event["client_id"] = headers["Client-Id"]
            wechat.on_recv(event)
        except Exception:
            print(traceback.format_exc())


class WeChat:

    def __init__(self, pid=0, host="127.0.0.1", port=19001):
        self.host = host
        self.port = port
        self.pid = pid
        self.url = f"http://{self.host}:{self.port}/api/client"
        self.event_emitter = ExecutorEventEmitter()
        self.clients = []
        self.__req_data_cache = {}
        self.server_host = "127.0.0.1"
        self.server_port = 8000
        self.login_event = threading.Event()
        self.server_thread = threading.Thread(target=self.start_server, daemon=True)
        self.server_thread.start()
        hook(self.pid, self.host, self.port, f"http://{self.server_host}:{self.server_port}")
        logger.info(f"API Server at {self.host}:{self.port}")

    def send(self, client_id: int = 0, data: dict = None):
        return requests.post(
            url=self.url + f"/{client_id}",
            data=binascii.hexlify(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        ).json()

    def inject(self, pid: int):
        return requests.post(url=f"http://{self.host}:{self.port}/api/inject" + f"/{pid}").json()

    def send_sync(self, client_id: int, data: dict, timeout: int = 10):
        field_name = "trace"
        if data.get(field_name) is None:
            data[field_name] = str(uuid.uuid4())

        req_data = ReqData(data["type"], data)
        self.__req_data_cache[data[field_name]] = req_data

        self.send(client_id, data)
        return req_data.wait_response(timeout)

    def on_event(self, data: dict):
        try:
            if data.get("type") is not None:
                if data["type"] == WECHAT_CONNECT_MESSAGE:
                    self.clients.append({
                        "id": data["client_id"],
                        "pid": data["data"]["pid"],
                        "create_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                self.event_emitter.emit(str(ALL_MESSAGE), self, data)
                self.event_emitter.emit(str(data["type"]), self, data)
            else:
                if data.get("event") == "disconnected":
                    clients = []
                    for client in self.clients:
                        if client["id"] != data["client_id"]:
                            clients.append(client)
                    self.clients = clients
        except Exception:
            logger.error(traceback.format_exc())

    def on_recv(self, data: dict):
        logger.debug(data)
        if data.get("trace") is not None:
            req_data = self.__req_data_cache[data["trace"]]
            req_data.on_response(data)
            del self.__req_data_cache[data["trace"]]
        else:
            self.on_event(data)

    def handle(self, events: typing.Union[typing.List[str], str, None] = None, once: bool = False) -> typing.Callable[
        [typing.Callable], None]:
        def wrapper(func):
            listen = self.event_emitter.on if not once else self.event_emitter.once
            if not events:
                listen(str(ALL_MESSAGE), func)
            else:
                for event in events if isinstance(events, list) else [events]:
                    listen(str(event), func)

        return wrapper

    def open(self):
        return self.send()

    def start_server(self):
        logger.info(f"Listen Server at {self.server_host}:{self.server_port}")
        self.server = socketserver.ThreadingTCPServer((self.server_host, self.server_port), RequestHandler)
        self.server.wechat = self
        self.server.serve_forever()

    def run(self):
        self.server_thread.join()
