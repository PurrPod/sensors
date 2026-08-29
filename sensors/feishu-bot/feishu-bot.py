# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "lark-oapi",
# ]
# ///

import sys
import json
import threading
import os
import io
import base64
import asyncio
import mimetypes
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateFileRequest,
    CreateFileRequestBody,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
)

_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr


def send_json_to_main(method: str, params: dict):
    _REAL_STDOUT.write(
        json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n"
    )
    _REAL_STDOUT.flush()


APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DEFAULT_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "")

client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .log_level(lark.LogLevel.WARNING)
    .build()
)


def _resolve_receive_id(kwargs: dict):
    receive_id = kwargs.get("target_id", DEFAULT_CHAT_ID)
    id_type = "chat_id"
    if receive_id.startswith("ou_"):
        id_type = "open_id"
    elif receive_id.startswith("on_"):
        id_type = "union_id"
    elif receive_id.startswith("eu_"):
        id_type = "email"
    return id_type, receive_id


def _sniff_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _handle_file_message(message) -> None:
    """处理图片/文件类消息：下载资源后以 file 类 observe 上报给网关"""
    try:
        content = json.loads(message.content)
        file_key = content.get("image_key") or content.get("file_key")
        if not file_key:
            print(f"⚠️ [Feishu] 文件消息缺少 file_key: {message.content}")
            return

        res_type = "image" if message.message_type == "image" else "file"
        req = (
            GetMessageResourceRequest.builder()
            .message_id(message.message_id)
            .file_key(file_key)
            .type(res_type)
            .build()
        )
        resp = client.im.v1.message_resource.get(req)
        if not resp.success():
            print(f"❌ [Feishu] 下载消息资源失败: {resp.msg}")
            return

        data = resp.file.read()
        if message.message_type == "image":
            mime = _sniff_image_mime(data)
            file_name = f"{file_key}{mimetypes.guess_extension(mime) or '.png'}"
        else:
            file_name = content.get("file_name") or resp.file_name or file_key
            mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        print(f"📎 [Feishu Sensor] 收到文件消息: {file_name} ({len(data)} bytes)")
        send_json_to_main(
            "observe",
            {
                "type": "file",
                "name": file_name,
                "mime": mime,
                "content_b64": base64.b64encode(data).decode("ascii"),
            },
        )
    except Exception as e:
        print(f"❌ [Feishu] 处理文件消息异常: {e}")


def _on_message_received(data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    msg_type = message.message_type

    if msg_type in ("image", "file", "media", "audio"):
        threading.Thread(target=_handle_file_message, args=(message,), daemon=True).start()
        return
    if msg_type != "text":
        print(f"⚠️ [Feishu] 收到非文本消息 ({msg_type})，暂未支持解析")
        return

    msg_content = json.loads(message.content)
    user_text = msg_content.get("text", "")

    print(f"💬 [Feishu Sensor] 收到用户消息: {user_text}")

    payload = f"[Feishu Sensor 收到用户消息] {user_text}"
    send_json_to_main("observe", {"content": payload})


def start_ws_listener():
    import time

    if not APP_ID or not APP_SECRET:
        print("⚠️ [Feishu] 缺少飞书凭证，无法启动 WebSocket")
        return

    while True:  # 无限重连循环
        try:
            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(_on_message_received)
                .build()
            )
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            import lark_oapi.ws.client as ws_client_module

            ws_client_module.loop = loop
            ws_client = lark.ws.Client(
                APP_ID,
                APP_SECRET,
                event_handler=event_handler,
                log_level=lark.LogLevel.WARNING,
            )
            print("🟢 [Feishu] WebSocket 监听已启动")
            ws_client.start()  # 如果网络断开，这里会结束或抛异常
        except Exception as e:
            print(f"❌ [Feishu] WebSocket 监听崩溃，5秒后重试: {e}")

        time.sleep(5)  # 缓冲时间，防止死循环狂刷日志


def _send_message_task(request):
    try:
        resp = client.im.v1.message.create(request)
        if not resp.success():
            print(f"❌ [Feishu] 发送失败: {resp.msg}")
    except Exception as e:
        print(f"❌ [Feishu] 请求飞书接口异常: {e}")


def _send_file_task(params: dict):
    """处理 file 类 express：base64 解码后先上传换 key，再发图片/文件消息"""
    try:
        kwargs = params.get("kwargs", {})
        id_type, receive_id = _resolve_receive_id(kwargs)
        file_name = params.get("name", "file")
        mime = params.get("mime", "application/octet-stream")
        data = base64.b64decode(params.get("content_b64", ""))
        if not data:
            print("⚠️ [Feishu] file 类 express 内容为空，忽略")
            return

        print(f"📎 [Feishu] 准备发送文件: {file_name} ({len(data)} bytes)")

        if mime.startswith("image/"):
            # 图片：上传换 image_key，发 image 消息
            resp = client.im.v1.image.create(
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(io.BytesIO(data))
                    .build()
                )
                .build()
            )
            if not resp.success():
                print(f"❌ [Feishu] 图片上传失败: {resp.msg}")
                return
            msg_type = "image"
            content = json.dumps({"image_key": resp.get_data().image_key})
        else:
            # 其他文件：上传换 file_key，发 file 消息
            resp = client.im.v1.file.create(
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type("stream")
                    .file_name(file_name)
                    .file(io.BytesIO(data))
                    .build()
                )
                .build()
            )
            if not resp.success():
                print(f"❌ [Feishu] 文件上传失败: {resp.msg}")
                return
            msg_type = "file"
            content = json.dumps({"file_key": resp.get_data().file_key})

        req_msg = (
            CreateMessageRequest.builder()
            .receive_id_type(id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        _send_message_task(req_msg)
    except Exception as e:
        print(f"❌ [Feishu] 发送文件异常: {e}")


threading.Thread(target=start_ws_listener, daemon=True).start()

for line in sys.stdin:
    if not line.strip():
        continue
    try:
        req = json.loads(line)
        if req.get("method") == "express":
            params = req["params"]

            # file 类消息：单独走文件发送通道
            if params.get("type") == "file":
                threading.Thread(target=_send_file_task, args=(params,), daemon=True).start()
                continue

            message = params.get("message", "")
            kwargs = params.get("kwargs", {})
            id_type, receive_id = _resolve_receive_id(kwargs)

            card_content = {
                "config": {"wide_screen_mode": True},
                "elements": [{"tag": "markdown", "content": message}],
            }
            req_msg = (
                CreateMessageRequest.builder()
                .receive_id_type(id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type("interactive")
                    .content(json.dumps(card_content, ensure_ascii=False))
                    .build()
                )
                .build()
            )

            threading.Thread(
                target=_send_message_task, args=(req_msg,), daemon=True
            ).start()
    except json.JSONDecodeError:
        pass
    except Exception as e:
        print(f"❌ [Feishu] 处理 express 消息时发生未知异常: {e}")
