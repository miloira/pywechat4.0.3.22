import binascii
import datetime
import json
import socketserver
import threading
import traceback
import typing
import uuid

import requests

from typing import Optional, Union, List

from pyee.executor import EventEmitter

from wechat.events import ALL_MESSAGE, WECHAT_CONNECT_MESSAGE
from wechat.utils import hook

from wechat.logger import logger


class ReqData:
    __response_message: typing.Optional[dict] = None
    msg_type: int = 0
    request_data: typing.Optional[dict] = None

    def __init__(self, msg_type: int, data: dict):
        self.msg_type = msg_type
        self.request_data = data
        self.__wait_event = threading.Event()

    def wait_response(self, timeout: typing.Optional[int] = None) -> dict:
        self.__wait_event.wait(timeout)
        return self.get_response_data()

    def on_response(self, message: dict) -> None:
        self.__response_message = message
        self.__wait_event.set()

    def get_response_data(self) -> typing.Union[dict, None]:
        if self.__response_message is None:
            return None
        return self.__response_message["data"]


class RequestHandler(socketserver.BaseRequestHandler):

    def handle(self) -> None:
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

            hex_data = data.split(b"\r\n\r\n")[-1]
            hex_data_bytes = binascii.unhexlify(hex_data)
            try:
                raw_data = hex_data_bytes.decode("utf-8", "replace").rstrip("\n")
            except UnicodeDecodeError:
                raw_data = hex_data_bytes.decode("gbk").rstrip("\n")
            event = json.loads(raw_data)
            event["client_id"] = int(headers["Client-Id"])
            wechat = getattr(self.server, "wechat")
            wechat.on_recv(event)
        except Exception:
            print(hex_data_bytes)
            logger.warning(traceback.format_exc())


class WeChat:

    def __init__(
            self, pid: int = 0,
            host: str = "127.0.0.1",
            port: int = 19088,
            server_host: str = "127.0.0.1",
            server_port: int = 18999,
            timeout: int = 10
    ):
        self.pid = pid
        self.host = host
        self.port = port
        self.server_host = server_host
        self.server_port = server_port
        self.timeout = timeout
        self.base_url = f"http://{self.host}:{self.port}"
        self.server_base_url = f"http://{self.server_host}:{self.server_port}"
        self.event_emitter = EventEmitter()
        self.clients = []
        self.__req_data_cache = {}
        self.login_event = threading.Event()
        self.server_thread = threading.Thread(target=self.start_server, daemon=True)
        self.server_thread.start()
        hook(self.pid, self.host, self.port, f"http://{self.server_host}:{self.server_port}")
        logger.info(f"API Server at {self.base_url}")

    def open(self) -> dict:
        return requests.post(url=f"{self.base_url}/api/open").json()

    def inject(self, pid: int) -> dict:
        return requests.post(url=f"{self.base_url}/api/inject/{pid}").json()

    def send(self, client_id: int = 0, data: dict = None) -> dict:
        return requests.post(url=f"{self.base_url}/api/client/{client_id}",
                             data=binascii.hexlify(json.dumps(data, ensure_ascii=False).encode("utf-8"))).json()

    def destory(self) -> dict:
        return requests.post(url=f"{self.base_url}/api/destory").json()

    def send_sync(self, client_id: int, data: dict, timeout: int = None) -> typing.Union[dict, None]:
        field_name = "trace"
        if data.get(field_name) is None:
            data[field_name] = str(uuid.uuid4())

        req_data = ReqData(data["type"], data)
        self.__req_data_cache[data[field_name]] = req_data

        self.send(client_id, data)
        return req_data.wait_response(timeout or self.timeout)

    def on_event(self, data: dict) -> None:
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

    def on_recv(self, data: dict) -> None:
        logger.debug(data)
        if data.get("trace") is not None:
            req_data = self.__req_data_cache[data["trace"]]
            req_data.on_response(data)
            del self.__req_data_cache[data["trace"]]
        else:
            self.on_event(data)

    def send_text(self, client_id: int, to_wxid: str, content: str) -> dict:
        """发送文本消息"""
        data = {
            "type": 11036,
            "data": {
                "to_wxid": to_wxid,
                "content": content
            }
        }
        return self.send(client_id, data)

    def send_room_at(self, client_id: int, to_wxid: str, content: str, at_list: List[str]) -> dict:
        """发送群at消息"""
        data = {
            "type": 11037,
            "data": {
                "to_wxid": to_wxid,
                "content": content,
                "at_list": at_list
            }
        }
        return self.send(client_id, data)

    def send_card(self, client_id: int, to_wxid: str, card_wxid: str) -> dict:
        """发送名片消息"""
        data = {
            "type": 11038,
            "data": {
                "to_wxid": to_wxid,
                "card_wxid": card_wxid
            }
        }
        return self.send(client_id, data)

    def send_link_card(self, client_id: int, to_wxid: str, title: str, desc: str, url: str, image_url: str) -> dict:
        """发送链接卡片消息"""
        data = {
            "type": 11039,
            "data": {
                "to_wxid": to_wxid,
                "title": title,
                "desc": desc,
                "url": url,
                "image_url": image_url
            }
        }
        return self.send(client_id, data)

    def send_image(self, client_id: int, to_wxid: str, file: str) -> dict:
        """发送图片消息"""
        data = {
            "type": 11040,
            "data": {
                "to_wxid": to_wxid,
                "file": file
            }
        }
        return self.send(client_id, data)

    def send_video(self, client_id: int, to_wxid: str, file: str) -> dict:
        """发送视频消息"""
        data = {
            "type": 11042,
            "data": {
                "to_wxid": to_wxid,
                "file": file
            }
        }
        return self.send(client_id, data)

    def send_file(self, client_id: int, to_wxid: str, file: str) -> dict:
        """发送文件消息"""
        data = {
            "type": 11041,
            "data": {
                "to_wxid": to_wxid,
                "file": file
            }
        }
        return self.send(client_id, data)

    def send_emotion(self, client_id: int, to_wxid: str, file: str) -> dict:
        """发送表情消息"""
        data = {
            "type": 11043,
            "data": {
                "to_wxid": to_wxid,
                "file": file
            }
        }
        return self.send(client_id, data)

    def send_pat(self, client_id: int, room_wxid: str, patted_wxid: str) -> dict:
        """发送拍一拍消息"""
        data = {
            "type": 11250,
            "data": {
                "room_wxid": room_wxid,
                "patted_wxid": patted_wxid
            }
        }
        return self.send(client_id, data)

    def create_room(self, client_id: int, member_list: List[str]) -> dict:
        """创建群聊"""
        data = {
            "type": 11068,
            "data": member_list
        }
        return self.send(client_id, data)

    def send_collection(self, client_id: int, to_wxid: str, local_id: int) -> dict:
        """发送收藏消息"""
        data = {
            "type": 11110,
            "data": {
                "to_wxid": to_wxid,
                "local_id": local_id
            }
        }
        return self.send(client_id, data)

    def collect(self, client_id: int, msg_id: str) -> dict:
        """收藏消息"""
        data = {
            "type": 11111,
            "data": {
                "msgid": msg_id
            }
        }
        return self.send(client_id, data)

    def confirm_receipt(self, client_id: int, transfer_id: str) -> dict:
        """确认收款"""
        data = {
            "type": 11066,
            "data": {
                "transferid": transfer_id
            }
        }
        return self.send(client_id, data)

    def modify_tag(self, client_id: int, label_id: int, label_name: str) -> dict:
        """修改标签"""
        data = {
            "type": 11139,
            "data": {
                "label_id": label_id,
                "label_name": label_name
            }
        }
        return self.send(client_id, data)

    def clear_chat_history(self, client_id: int) -> dict:
        """清除聊天记录"""
        data = {
            "type": 11108,
            "data": {}
        }
        return self.send(client_id, data)

    def pin_chat(self, client_id: int, wxid: str, status: int, timeout: Optional[int] = None) -> dict:
        """置顶/取消置顶聊天"""
        data = {
            "type": 11079,
            "data": {
                "wxid": wxid,
                "status": status
            }
        }
        return self.send_sync(client_id, data, timeout)

    def create_virtual_nickname(self, client_id: int, nickname: str, head_img_url: str) -> dict:
        """创建虚拟昵称"""
        data = {
            "type": 11194,
            "data": {
                "nickname": nickname,
                "headimg_url": head_img_url
            }
        }
        return self.send(client_id, data)

    def switch_virtual_nickname(self, client_id: int, role_type: int) -> dict:
        """切换虚拟昵称"""
        data = {
            "type": 11195,
            "data": {
                "role_type": role_type
            }
        }
        return self.send(client_id, data)

    def delete_virtual_nickname(self, client_id: int) -> dict:
        """删除虚拟昵称"""
        data = {
            "type": 11197,
            "data": {}
        }
        return self.send(client_id, data)

    def decode_image(self, client_id: int, src_file: str, dest_file: str) -> dict:
        """解密图片"""
        data = {
            "type": 10085,
            "data": {
                "src_file": src_file,
                "dest_file": dest_file
            }
        }
        return self.send(client_id, data)

    def get_self_info(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """获取当前账号信息"""
        data = {
            "type": 11028,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def get_contacts(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """获取好友列表"""
        data = {
            "type": 11030,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def get_contact(self, client_id: int, wxid: str, timeout: Optional[int] = None) -> dict:
        """获取好友信息"""
        data = {
            "type": 11029,
            "data": {
                "wxid": wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_rooms(self, client_id: int, detail: int = 1, timeout: Optional[int] = None) -> dict:
        """获取群列表"""
        data = {
            "type": 11031,
            "data": {
                "detail": detail
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_room(self, client_id: int, room_wxid: str, timeout: Optional[int] = None) -> dict:
        """获取群信息"""
        data = {
            "type": 11125,
            "data": {
                "room_wxid": room_wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_room_members(self, client_id: int, room_wxid: str, timeout: Optional[int] = None) -> dict:
        """获取群成员列表"""
        data = {
            "type": 11032,
            "data": {
                "room_wxid": room_wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_public(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """获取公众号列表"""
        data = {
            "type": 11033,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def get_contact_by_protocol(self, client_id: int, wxid: str, timeout: Optional[int] = None) -> dict:
        """获取好友简要信息（协议）"""
        data = {
            "type": 11034,
            "data": {
                "wxid": wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_room_member_by_net(self, client_id: int, room_wxid: str, wxid: str, timeout: Optional[int] = None) -> dict:
        """获取群成员信息"""
        data = {
            "type": 111035,
            "data": {
                "room_wxid": room_wxid,
                "wxid": wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_contact_detail_by_protocol(self, client_id: int, wxid: str, timeout: Optional[int] = None) -> dict:
        """获取好友详细信息（协议）"""
        data = {
            "type": 11174,
            "data": {
                "wxid": wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_contacts_by_protocol(self, client_id: int, wxids: List[str], timeout: Optional[int] = None) -> dict:
        """获取多个好友信息（协议）"""
        data = {
            "type": 11174,
            "data": {
                "username_list": wxids
            }
        }
        return self.send_sync(client_id, data, timeout)

    def modify_contact_remark(self, client_id: int, wxid: str, remark: str, timeout: Optional[int] = None) -> dict:
        """修改好友备注"""
        data = {
            "type": 11063,
            "data": {
                "wxid": wxid,
                "remark": remark
            }
        }
        return self.send_sync(client_id, data, timeout)

    def delete_friend(self, client_id: int, wxid: str, timeout: Optional[int] = None) -> dict:
        """删除好友"""
        data = {
            "type": 11064,
            "data": {
                "wxid": wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def accept_friend_request(self, client_id: int, encrypt_username: str, ticket: str, scene: int = 17,
                              timeout: Optional[int] = None) -> dict:
        """同意好友请求"""
        data = {
            "type": 11065,
            "data": {
                "encryptusername": encrypt_username,
                "ticket": ticket,
                "scene": scene
            }
        }
        return self.send_sync(client_id, data, timeout)

    def search_friend(self, client_id: int, search: str, timeout: Optional[int] = None) -> dict:
        """搜索微信好友"""
        data = {
            "type": 11096,
            "data": {
                "search": search
            }
        }
        return self.send_sync(client_id, data, timeout)

    def add_friend(self, client_id: int, v1: str, v2: str, remark: str, timeout: Optional[int] = None) -> dict:
        """添加好友"""
        data = {
            "type": 11097,
            "data": {
                "v1": v1,
                "v2": v2,
                "remark": remark
            }
        }
        return self.send_sync(client_id, data, timeout)

    def add_friend_by_card(self, client_id: int, wxid: str, ticket: str, remark: str,
                           timeout: Optional[int] = None) -> dict:
        """添加好友分享的名片"""
        data = {
            "type": 11062,
            "data": {
                "remark": remark,
                "source_type": 17,
                "wxid": wxid,
                "ticket": ticket
            }
        }
        return self.send_sync(client_id, data, timeout)

    def add_friend_by_room(self, client_id: int, room_wxid: str, wxid: str, remark: str,
                           timeout: Optional[int] = None) -> dict:
        """添加群成员为好友"""
        data = {
            "type": 11062,
            "data": {
                "remark": remark,
                "source_type": 14,
                "wxid": wxid,
                "room_wxid": room_wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def check_friend_status(self, client_id: int, wxid: str, timeout: Optional[int] = None) -> dict:
        """检查好友状态"""
        data = {
            "type": 11080,
            "data": {
                "wxid": wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_room_by_protocol(self, client_id: int, wxid: str, timeout: Optional[int] = None) -> dict:
        """获取群信息（协议）"""
        data = {
            "type": 11174,
            "data": {
                "wxid": wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_invitation_relationship(self, client_id: int, room_wxid: str, timeout: Optional[int] = None) -> dict:
        """获取群成员邀请关系"""
        data = {
            "type": 11134,
            "data": {
                "room_wxid": room_wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def create_room_by_protocol(self, client_id: int, member_list: List[str], timeout: Optional[int] = None) -> dict:
        """创建群聊（协议）"""
        data = {
            "type": 11246,
            "data": member_list
        }
        return self.send_sync(client_id, data, timeout)

    def add_room_member(self, client_id: int, room_wxid: str, member_list: List[str],
                        timeout: Optional[int] = None) -> dict:
        """添加群成员"""
        data = {
            "type": 11069,
            "data": {
                "room_wxid": room_wxid,
                "member_list": member_list
            }
        }
        return self.send_sync(client_id, data, timeout)

    def invite_room_member(self, client_id: int, room_wxid: str, member_list: List[str],
                           timeout: Optional[int] = None) -> dict:
        """邀请群成员"""
        data = {
            "type": 11070,
            "data": {
                "room_wxid": room_wxid,
                "member_list": member_list
            }
        }
        return self.send_sync(client_id, data, timeout)

    def remove_room_member(self, client_id: int, room_wxid: str, wxid: str, timeout: Optional[int] = None) -> dict:
        """移出群成员"""
        data = {
            "type": 11071,
            "data": {
                "room_wxid": room_wxid,
                "name": wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def modify_room_name(self, client_id: int, room_wxid: str, name: str, timeout: Optional[int] = None) -> dict:
        """修改群名称"""
        data = {
            "type": 11072,
            "data": {
                "room_wxid": room_wxid,
                "name": name
            }
        }
        return self.send_sync(client_id, data, timeout)

    def modify_room_notice(self, client_id: int, room_wxid: str, notice: str, timeout: Optional[int] = None) -> dict:
        """修改群公告"""
        data = {
            "type": 11073,
            "data": {
                "room_wxid": room_wxid,
                "notice": notice
            }
        }
        return self.send_sync(client_id, data, timeout)

    def modify_room_member_nickname(self, client_id: int, room_id: str, nickname: int,
                                    timeout: Optional[int] = None) -> dict:
        """修改我在本群的昵称"""
        data = {
            "type": 11074,
            "data": {
                "room_id": room_id,
                "nickname": nickname
            }
        }
        return self.send_sync(client_id, data, timeout)

    def display_room_member_nickname(self, client_id: int, room_id: str, status: int = 1,
                                     timeout: Optional[int] = None) -> dict:
        """是否显示群成员昵称"""
        data = {
            "type": 11075,
            "data": {
                "room_id": room_id,
                "status": status
            }
        }
        return self.send_sync(client_id, data, timeout)

    def edit_address_book(self, client_id: int, room_id: str, status: int = 1, timeout: Optional[int] = None) -> dict:
        """保存/移除通讯录"""
        data = {
            "type": 11076,
            "data": {
                "room_id": room_id,
                "status": status
            }
        }
        return self.send_sync(client_id, data, timeout)

    def exit_room(self, client_id: int, room_id: str, timeout: Optional[int] = None) -> dict:
        """退出群聊"""
        data = {
            "type": 11077,
            "data": {
                "room_id": room_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_corporate_contacts(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """获取企业联系人"""
        data = {
            "type": 11132,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def get_corporate_rooms(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """获取企业群"""
        data = {
            "type": 11129,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def get_corporate_room_members(self, client_id: int, room_id: str, timeout: Optional[int] = None) -> dict:
        """获取企业微信群成员"""
        data = {
            "type": 11130,
            "data": {
                "room_id": room_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def cdn_init(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """初始化CDN"""
        data = {
            "type": 11228,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def cdn_upload(self, client_id: int, file_type: int, file_path: str, timeout: Optional[int] = None) -> dict:
        """CDN上传"""
        data = {
            "type": 11229,
            "data": {
                "file_type": file_type,
                "file_path": file_path
            }
        }
        return self.send_sync(client_id, data, timeout)

    def cdn_download(self, client_id: int, file_id: str, aes_key: str, save_path: str, file_type: int,
                     timeout: Optional[int] = None) -> dict:
        """CDN下载"""
        data = {
            "type": 11230,
            "data": {
                "file_id": file_id,
                "file_type": file_type,
                "aes_key": aes_key,
                "save_path": save_path
            }
        }
        return self.send_sync(client_id, data, timeout)

    def cdn_download2(self, client_id: int, url: str, auth_key: str, aes_key: str, save_path: str,
                      timeout: Optional[int] = None) -> dict:
        """企业微信CDN下载"""
        data = {
            "type": 11253,
            "data": {
                "url": url,
                "auth_key": auth_key,
                "aes_key": aes_key,
                "save_path": save_path
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_text_by_cdn(self, client_id: int, to_wxid: str, content: str, timeout: Optional[int] = None) -> dict:
        """发送文本消息（CDN）"""
        data = {
            "type": 11237,
            "data": {
                "to_wxid": to_wxid,
                "content": content
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_room_at_by_cdn(self, client_id: int, to_wxid: str, content: str, at_list: Union[List[str], None] = None,
                            at_all: int = 0, timeout: Optional[int] = None) -> dict:
        """发送群at消息（CDN）"""
        if at_all == 0:
            if not isinstance(at_list, list):
                raise TypeError("at_list must be a list.")
            data = {
                "type": 11240,
                "data": {
                    "to_wxid": to_wxid,
                    "content": content,
                    "at_list": at_list
                }
            }
        else:
            data = {
                "type": 11240,
                "data": {
                    "to_wxid": to_wxid,
                    "content": content,
                    "at_all": 1
                }
            }
        return self.send_sync(client_id, data, timeout)

    def send_card_by_cdn(self, client_id: int, to_wxid: str, username: str, nickname: str, avatar: str,
                         timeout: Optional[int] = None) -> dict:
        """发送名片消息（CDN）"""
        data = {
            "type": 11239,
            "data": {
                "to_wxid": to_wxid,
                "username": username,
                "nickname": nickname,
                "avatar": avatar
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_link_card_by_cdn(self, client_id: int, to_wxid: str, title: str, desc: str, url: str, image_url: str,
                              timeout: Optional[int] = None) -> dict:
        """发送链接卡片消息（CDN）"""
        data = {
            "type": 11236,
            "data": {
                "to_wxid": to_wxid,
                "title": title,
                "desc": desc,
                "url": url,
                "image_url": image_url
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_image_by_cdn(self, client_id: int, to_wxid: str, file_id: str, file_md5: str, file_size: int,
                          thumb_file_size: int, crc32: int, aes_key: str, timeout: Optional[int] = None) -> dict:
        """发送图片消息（CDN）"""
        data = {
            "type": 11231,
            "data": {
                "aes_key": aes_key,
                "file_id": file_id,
                "file_md5": file_md5,
                "file_size": file_size,
                "thumb_file_size": thumb_file_size,
                "crc32": crc32,
                "to_wxid": to_wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_video_by_cdn(self, client_id: int, to_wxid: str, file_id: str, file_md5: str, file_size: int,
                          thumb_file_size: int, aes_key: str, timeout: Optional[int] = None) -> dict:
        """发送视频消息（CDN）"""
        data = {
            "type": 11233,
            "data": {
                "aes_key": aes_key,
                "file_id": file_id,
                "file_md5": file_md5,
                "file_size": file_size,
                "thumb_file_size": thumb_file_size,
                "to_wxid": to_wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_file_by_cdn(self, client_id: int, to_wxid: str, file_id: str, file_md5: str, file_size: int,
                         file_name: str, aes_key: str, timeout: Optional[int] = None) -> dict:
        """发送文件消息（CDN）"""
        data = {
            "type": 11235,
            "data": {
                "aes_key": aes_key,
                "file_id": file_id,
                "file_md5": file_md5,
                "file_name": file_name,
                "file_size": file_size,
                "to_wxid": to_wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_emotion_by_cdn(self, client_id: int, aes_key: str, file_id: str, file_md5: str, file_size: int,
                            to_wxid: str, timeout: Optional[int] = None) -> dict:
        """发送表情消息（CDN）"""
        data = {
            "type": 11241,
            "data": {
                "aes_key": aes_key,
                "file_id": file_id,
                "file_md5": file_md5,
                "file_size": file_size,
                "to_wxid": to_wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_emotion2_by_cdn(self, client_id: int, to_wxid: str, path: str, timeout: Optional[int] = None) -> dict:
        """发送表情消息2（CDN）"""
        data = {
            "type": 11254,
            "data": {
                "to_wxid": to_wxid,
                "path": path
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_mini_program_by_cdn(self, client_id: int, to_wxid: str, username: str, appid: str, appname: str,
                                 appicon: str, title: str, page_path: str, aes_key: str, file_id: str, file_md5: str,
                                 file_size: int, timeout: Optional[int] = None) -> dict:
        """发送小程序消息（CDN）"""
        data = {
            "type": 11242,
            "data": {
                "to_wxid": to_wxid,
                "username": username,
                "appid": appid,
                "appname": appname,
                "appicon": appicon,
                "title": title,
                "page_path": page_path,
                "file_id": file_id,
                "aes_key": aes_key,
                "file_md5": file_md5,
                "file_size": file_size
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_video_card_by_cdn(self, client_id: int, to_wxid: str, object_id: str, object_nonce_id: str, nickname: str,
                               username: str, avatar: str, desc: str, thumb_url: str, url: str,
                               timeout: Optional[int] = None) -> dict:
        """发送视频号消息（CDN）"""
        data = {
            "type": 11243,
            "data": {
                "to_wxid": to_wxid,
                "object_id": object_id,
                "object_nonce_id": object_nonce_id,
                "nickname": nickname,
                "username": username,
                "avatar": avatar,
                "desc": desc,
                "thumb_url": thumb_url,
                "url": url
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_location_by_cdn(self, client_id: int, to_wxid: str, address: str, latitude: float, longitude: float,
                             title: str, timeout: Optional[int] = None) -> dict:
        """发送位置消息（CDN）"""
        data = {
            "type": 11238,
            "data": {
                "to_wxid": to_wxid,
                "address": address,
                "latitude": latitude,
                "longitude": longitude,
                "title": title
            }
        }
        return self.send_sync(client_id, data, timeout)

    def revoke_msg_by_cdn(self, client_id: int, to_wxid: str, new_msg_id: str, client_msg_id: int, create_time: int,
                          timeout: Optional[int] = None) -> dict:
        """撤回消息（CDN）"""
        data = {
            "type": 11244,
            "data": {
                "to_wxid": to_wxid,
                "client_msgid": client_msg_id,
                "create_time": create_time,
                "new_msgid": new_msg_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_collections(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """获取收藏列表"""
        data = {
            "type": 11109,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def get_tags(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """获取标签列表"""
        data = {
            "type": 11142,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def add_tag(self, client_id: int, label_name: str, timeout: Optional[int] = None) -> dict:
        """添加标签"""
        data = {
            "type": 11137,
            "data": {
                "label_name": label_name
            }
        }
        return self.send_sync(client_id, data, timeout)

    def delete_tag(self, client_id: int, label_id: int, timeout: Optional[int] = None) -> dict:
        """删除标签"""
        data = {
            "type": 11138,
            "data": {
                "label_id": label_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def add_tags_to_contact(self, client_id: int, wxid: str, label_id_list: str, timeout: Optional[int] = None) -> dict:
        """批量给用户加标签"""
        data = {
            "type": 11140,
            "data": {
                "wxid": wxid,
                "labelid_list": label_id_list
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_contact_tags(self, client_id: int, wxid: str, timeout: Optional[int] = None) -> dict:
        """获取联系人所有标签"""
        data = {
            "type": 11141,
            "data": {
                "wxid": wxid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def voice_to_text(self, client_id: int, msg_id: str, timeout: Optional[int] = None) -> dict:
        """语音消息转文本"""
        data = {
            "type": 11112,
            "data": {
                "msgid": msg_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def set_disturb(self, client_id: int, wxid: str, status: int, timeout: Optional[int] = None) -> dict:
        """开启/关闭消息免打扰"""
        data = {
            "type": 11078,
            "data": {
                "wxid": wxid,
                "status": status
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_mini_program_code(self, client_id: int, appid: str, timeout: Optional[int] = None) -> dict:
        """获取小程序授权code"""
        data = {
            "type": 11136,
            "data": {
                "appid": appid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_moments(self, client_id: int, max_id: str = "0", timeout: Optional[int] = None) -> dict:
        """获取朋友圈"""
        data = {
            "type": 11145,
            "data": {
                "max_id": max_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_friend_moments(self, client_id: int, username: str, first_page_md5: str = "", max_id: str = "0",
                           timeout: Optional[int] = None) -> dict:
        """获取好友朋友圈"""
        data = {
            "type": 11150,
            "data": {
                "username": username,
                "first_page_md5": first_page_md5,
                "max_id": max_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def comment_moment(self, client_id: int, object_id: str, content: str, timeout: Optional[int] = None) -> dict:
        """评论"""
        data = {
            "type": 11146,
            "data": {
                "object_id": object_id,
                "content": content
            }
        }
        return self.send_sync(client_id, data, timeout)

    def like_moment(self, client_id: int, object_id: str, timeout: Optional[int] = None) -> dict:
        """点赞"""
        data = {
            "type": 11147,
            "data": {
                "object_id": object_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def post_moment(self, client_id: int, object_desc: str, timeout: Optional[int] = None) -> dict:
        """发朋友圈"""
        data = {
            "type": 11148,
            "data": {
                "object_desc": object_desc
            }
        }
        return self.send_sync(client_id, data, timeout)

    def upload_image(self, client_id: int, image_path: str, timeout: Optional[int] = None) -> dict:
        """上传图片"""
        data = {
            "type": 11149,
            "data": {
                "path": image_path
            }
        }
        return self.send_sync(client_id, data, timeout)

    def init_video_account(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """视频号初始化"""
        data = {
            "type": 11160,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def search_video_account(self, client_id: int, query: str, scene: int, last_buff: str = "",
                             timeout: Optional[int] = None) -> dict:
        """视频号搜索"""
        data = {
            "type": 11161,
            "data": {
                "query": query,
                "last_buff": last_buff,
                "scene": scene
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_video_account_user_page(self, client_id: int, username: str, last_buff: str = "",
                                    timeout: Optional[int] = None) -> dict:
        """视频号用户主页"""
        data = {
            "type": 11170,
            "data": {
                "username": username,
                "last_buff": last_buff
            }
        }
        return self.send_sync(client_id, data, timeout)

    def view_video_details(self, client_id: int, object_id: str, object_nonce_id: str, last_buff: str = "",
                           timeout: Optional[int] = None) -> dict:
        """查看视频详细信息(包含评论)"""
        data = {
            "type": 11169,
            "data": {
                "object_id": object_id,
                "object_nonce_id": object_nonce_id,
                "last_buff": last_buff
            }
        }
        return self.send_sync(client_id, data, timeout)

    def follow_video_blogger(self, client_id: int, username: str, timeout: Optional[int] = None) -> dict:
        """关注博主"""
        data = {
            "type": 11167,
            "data": {
                "username": username
            }
        }
        return self.send_sync(client_id, data, timeout)

    def like_video(self, client_id: int, object_id: str, object_nonce_id: str, timeout: Optional[int] = None) -> dict:
        """视频号点赞"""
        data = {
            "type": 11168,
            "data": {
                "object_id": object_id,
                "object_nonce_id": object_nonce_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_message_session_id(self, client_id: int, to_username: str, role_type: int,
                               timeout: Optional[int] = None) -> dict:
        """获取私信sessionId"""
        data = {
            "type": 11202,
            "data": {
                "to_username": to_username,
                "roleType": role_type
            }
        }
        return self.send_sync(client_id, data, timeout)

    def send_private_message(self, client_id: int, to_username: str, session_id: str, content: str,
                             timeout: Optional[int] = None) -> dict:
        """发送私信"""
        data = {
            "type": 11203,
            "data": {
                "to_username": to_username,
                "session_id": session_id,
                "content": content
            }
        }
        return self.send_sync(client_id, data, timeout)

    def enter_live_room(self, client_id: int, object_id: str, live_id: str, object_nonce_id: str,
                        timeout: Optional[int] = None) -> dict:
        """进入直播间"""
        data = {
            "type": 11162,
            "data": {
                "object_id": object_id,
                "live_id": live_id,
                "object_nonce_id": object_nonce_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_live_room_online_users(self, client_id: int, object_id: str, live_id: str, object_nonce_id: str,
                                   timeout: Optional[int] = None) -> dict:
        """获取直播间在线人员"""
        data = {
            "type": 11172,
            "data": {
                "object_id": object_id,
                "live_id": live_id,
                "object_nonce_id": object_nonce_id,
                "last_buff": ""
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_live_room_updates(self, client_id: int, timeout: Optional[int] = None) -> dict:
        """获取直播间变动信息(人气，实时发言等)"""
        data = {
            "type": 11163,
            "data": {}
        }
        return self.send_sync(client_id, data, timeout)

    def speak_in_live_room(self, client_id: int, content: str, timeout: Optional[int] = None) -> dict:
        """直播间发言"""
        data = {
            "type": 11164,
            "data": {
                "content": content
            }
        }
        return self.send_sync(client_id, data, timeout)

    def like_in_live_room(self, client_id: int, count: int, timeout: Optional[int] = None) -> dict:
        """直播间点赞"""
        data = {
            "type": 11185,
            "data": {
                "count": count
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_live_room_shelves(self, client_id: int, live_username: str, request_id: str,
                              timeout: Optional[int] = None) -> dict:
        """获取直播间货架"""
        data = {
            "type": 11186,
            "data": {
                "live_username": live_username,
                "request_id": request_id
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_shelf_product_detail(self, client_id: int, appid: str, request_id: str, product_id: str, real_appid: str,
                                 live_username: str, timeout: Optional[int] = None) -> dict:
        """获取货架商品详细信息"""
        data = {
            "type": 11187,
            "data": {
                "appid": appid,
                "request_id": request_id,
                "product_id": product_id,
                "live_username": live_username,
                "real_appid": real_appid
            }
        }
        return self.send_sync(client_id, data, timeout)

    def get_a8key(self, client_id: int, url: str, scene: int, timeout: Optional[int] = None) -> dict:
        """A8Key接口"""
        data = {
            "type": 11135,
            "data": {
                "url": url,
                "scene": scene
            }
        }
        return self.send_sync(client_id, data, timeout)

    def exec_sql(self, client_id: int, sql: str, db: int, timeout: Optional[int] = None) -> dict:
        """执行SQL命令"""
        data = {
            "type": 11027,
            "data": {
                "sql": sql,
                "db": db
            }
        }
        return self.send_sync(client_id, data, timeout)

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

    def start_server(self) -> typing.NoReturn:
        logger.info(f"Event Server at {self.server_base_url}")
        self.server = socketserver.ThreadingTCPServer((self.server_host, self.server_port), RequestHandler)
        self.server.wechat = self
        self.server.serve_forever()

    def run(self) -> typing.NoReturn:
        self.server_thread.join()
