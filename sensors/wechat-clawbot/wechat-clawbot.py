# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pycryptodome",
# ]
# ///

"""wechat-clawbot sensor: 微信 iLink Bot <-> PurrCat 双向通道

协议逆向自 @tencent-weixin/openclaw-weixin 2.4.6
(完整逆向笔记见 /agent_vm/weixin_bot/WEIXIN_ILINK_REVERSE.md)

- observe: getupdates 长轮询收微信消息 -> 推给 Agent
- express: Agent 回复 -> sendmessage 发回微信
- 凭证: env WECHAT_BOT_TOKEN (bot_token, 扫码登录获得)
"""
import sys
import json
import threading
import time
import os
import secrets
import base64
import urllib.request
import urllib.error

_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr  # 铁律: stdout 只走协议 JSON

BASE = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
CLIENT_VER = str((2 << 16) | (4 << 8) | 6)  # 插件 2.4.6 -> 0x00MMNNPP
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_state.json")

_TOKEN = ""            # 全局 bot_token (env 注入)
_REPLY_CTX = {}        # to_user_id -> context_token (express 路由兜底)
_LAST_USER = ""        # 最近收到消息的用户 (express 无路由时兜底)


def send_json_to_main(method, params):
    _REAL_STDOUT.write(json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n")
    _REAL_STDOUT.flush()


def log(msg):
    print(f"[wechat-clawbot] {msg}")


def load_state():
    """读取持久化状态: updates_buf"""
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(s):
    try:
        json.dump(s, open(STATE_FILE, "w"))
    except Exception:
        pass


def load_buf():
    return load_state().get("updates_buf", "")


def save_buf(buf):
    st = load_state()
    st["updates_buf"] = buf
    save_state(st)


def hdrs(token=None):
    uin = base64.b64encode(str(secrets.randbelow(2**32)).encode()).decode()
    h = {"Content-Type": "application/json", "iLink-App-Id": APP_ID,
         "iLink-App-ClientVersion": CLIENT_VER, "X-WECHAT-UIN": uin}
    if token:
        h["Authorization"] = f"Bearer {token}"
        h["AuthorizationType"] = "ilink_bot_token"
    return h


def post(path, payload, token=None, timeout=35):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers=hdrs(token), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ret": -1, "errmsg": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"ret": -1, "errmsg": str(e)}


def get_json(path, timeout=32):
    """GET 请求返回 JSON (扫码状态轮询用)"""
    req = urllib.request.Request(BASE + path, headers=hdrs(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ret": -1, "errmsg": f"HTTP {e.code}"}
    except Exception as e:
        return {"ret": -1, "errmsg": str(e)}


def base_info():
    return {"channel_version": "2.4.6", "bot_agent": "OpenClaw"}


def fetch_bytes(url, timeout=30):
    """GET 下载 CDN 原始字节"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密 + PKCS7 unpad (微信 CDN 媒体)"""
    from Crypto.Cipher import AES
    cipher = AES.new(key, AES.MODE_ECB)
    out = cipher.decrypt(data)
    pad = out[-1] if out else 0
    if pad and 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
        return out[:-pad]
    return out


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 加密 + PKCS7 pad (微信 CDN 上传)"""
    from Crypto.Cipher import AES
    pad = 16 - len(data) % 16
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.encrypt(data + bytes([pad]) * pad)


def parse_aes_key(aeskey: str) -> bytes:
    """解析 AES 密钥，兼容三种格式:
    - image_item.aeskey: 32 字符 hex 字符串（优先）
    - media.aes_key: base64(原始16字节)
    - media.aes_key: base64(hex32字符)
    """
    key = (aeskey or "").strip()
    if len(key) == 32 and all(c in "0123456789abcdefABCDEF" for c in key):
        return bytes.fromhex(key)
    try:
        raw = base64.b64decode(key)
    except Exception:
        log(f"parse_aes_key 无法解析: len={len(key)} head={key[:12]}")
        return b""
    if len(raw) == 16:
        return raw
    if len(raw) == 32 and all(c in "0123456789abcdefABCDEF" for c in raw.decode("ascii", "ignore")):
        return bytes.fromhex(raw.decode("ascii"))
    log(f"parse_aes_key base64 解码异常: decoded={len(raw)} bytes")
    return raw


def sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def check_auth():
    """token 来源: env > state 文件；都没有则自动发起扫码登录"""
    global _TOKEN
    _TOKEN = os.environ.get("WECHAT_BOT_TOKEN", "").strip()
    if not _TOKEN:
        _TOKEN = (load_state().get("token") or "").strip()
    if not _TOKEN:
        start_qr_login()
        return False
    return True


def start_qr_login():
    """首次启动无 token: 获取登录二维码 observe 推给老板, 后台轮询扫码结果"""
    try:
        r = post("/ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []}, timeout=20)
        if r.get("ret") != 0:
            send_json_to_main("observe", {"content":
                "[Wechat Sensor 求助] 获取微信登录二维码失败，请稍后重启。手动获取 token 可 POST "
                "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3 取二维码，"
                "手机微信扫码确认后轮询 get_qrcode_status 拿 bot_token，填入配置中心。"})
            return
        qr = r["qrcode"]
        qr_url = r["qrcode_img_content"]
        # qrcode_img_content 是微信官方二维码网页(JS渲染)，优先发 URL；若返回真图片才发文件事件
        sent_file = False
        try:
            img = fetch_bytes(qr_url, timeout=20)
            if sniff_mime(img) != "application/octet-stream":
                import base64 as _b64
                send_json_to_main("observe", {"type": "file", "name": "wx_login_qr.png",
                                              "mime": sniff_mime(img),
                                              "content_b64": _b64.b64encode(img).decode()})
                sent_file = True
        except Exception:
            pass
        if not sent_file:
            send_json_to_main("observe", {"content":
                f"[Wechat Sensor 求助] 首次使用需扫码登录微信 bot（5分钟有效）。请在浏览器打开下面的链接显示二维码，"
                f"再用手机微信扫一扫确认: {qr_url}"})
        threading.Thread(target=poll_qr_login, args=(qr,), daemon=True).start()
    except Exception as e:
        send_json_to_main("observe", {"content": f"[Wechat Sensor 求助] 扫码登录初始化失败: {e}"})


def poll_qr_login(qr):
    """后台轮询扫码状态, confirmed 后保存 token 并启动监听"""
    global _TOKEN
    verify = None
    deadline = time.time() + 280
    notified = set()
    while time.time() < deadline:
        path = f"/ilink/bot/get_qrcode_status?qrcode={qr}"
        if verify:
            path += f"&verify_code={verify}"
        s = get_json(path)
        stt = s.get("status", "wait")
        if stt == "confirmed":
            tok = s.get("bot_token")
            if not tok:
                log("❌ [Wechat Sensor] 扫码确认但未返回 bot_token")
                return
            _TOKEN = tok
            st = load_state()
            st["token"] = tok
            st["bot_id"] = s.get("ilink_bot_id", "")
            save_state(st)
            send_json_to_main("observe", {"content": "[Wechat Sensor] ✅ 微信扫码登录成功，已自动连接。"})
            threading.Thread(target=event_listener, daemon=True).start()
            return
        if stt == "scaned" and "scaned" not in notified:
            send_json_to_main("observe", {"content": "[Wechat Sensor] 已扫码，请在手机微信上确认登录。"})
            notified.add("scaned")
        if stt == "need_verifycode":
            send_json_to_main("observe", {"content":
                "[Wechat Sensor 求助] 扫码需要验证码（手机微信显示数字），请在配置中心直接填 bot_token 或稍后重启重新扫码。"})
            return
        if stt in ("expired", "verify_code_blocked"):
            send_json_to_main("observe", {"content": "[Wechat Sensor] 二维码已过期，请重启 sensor 获取新二维码。"})
            return
        time.sleep(1)
    send_json_to_main("observe", {"content": "[Wechat Sensor] 扫码超时（5分钟），请重启 sensor 获取新二维码。"})


def handle_msg(m):
    global _LAST_USER
    frm = m.get("from_user_id", "")
    ctx = m.get("context_token", "")
    if frm and ctx:
        _REPLY_CTX[frm] = ctx
        _LAST_USER = frm
    for it in (m.get("item_list") or []):
        t = it.get("type")
        if t == 1 and it.get("text_item"):
            text = (it["text_item"].get("text") or "").strip()
            if not text:
                continue
            log(f"💬 [Wechat Sensor] 收到用户消息: {text}")
            send_json_to_main("observe", {"content": f"[Wechat Sensor 收到用户消息] {text}",
                                          "kwargs": {"wechat_to_user": frm,
                                                     "wechat_context_token": ctx}})
        elif t == 2 and it.get("image_item"):
            ii = it["image_item"]
            media = ii.get("media") or {}
            url = media.get("full_url", "") or ii.get("url", "")
            aeskey = ii.get("aeskey") or media.get("aes_key") or ""
            if url and aeskey:
                # 下载 + AES-ECB 解密 -> observe 文件事件 (网关自动落盘)
                try:
                    raw = fetch_bytes(url)
                    plain = aes_ecb_decrypt(raw, parse_aes_key(aeskey))
                    mime = sniff_mime(plain)
                    fname = f"wx_img_{int(time.time())}.{mime.split('/')[1] or 'bin'}"
                    import base64 as _b64
                    log(f"📎 [Wechat Sensor] 收到文件消息: {fname} ({len(plain)} bytes)")
                    send_json_to_main("observe", {"type": "file", "name": fname,
                                                  "mime": mime, "content_b64": _b64.b64encode(plain).decode(),
                                                  "kwargs": {"wechat_to_user": frm, "wechat_context_token": ctx}})
                    continue
                except Exception as e:
                    log(f"❌ [Wechat Sensor] 图片下载/解密失败: {e}")
            # 兜底: 无法解密时把 url 文本推给 Agent
            send_json_to_main("observe", {"content": f"[Wechat Sensor 收到用户图片] url={url}",
                                          "kwargs": {"wechat_to_user": frm,
                                                     "wechat_context_token": ctx}})
        else:
            log(f"未处理消息项 type={t} (微信->Agent)")


def event_listener():
    buf = load_buf()
    while True:
        try:
            r = post("/ilink/bot/getupdates",
                     {"get_updates_buf": buf, "base_info": base_info()}, _TOKEN, timeout=35)
            if r.get("ret") not in (0, None):
                log(f"getupdates 异常: {r.get('errmsg', r)}")
                if r.get("errcode") == -14:  # token 过期, 暂停 1h 并求助
                    send_json_to_main("observe", {"content":
                        "[wechat-clawbot 求助] 微信 bot_token 已过期（errcode -14），我暂停监听。"
                        "请老板重新扫码登录（/agent_vm/weixin_bot 下 python3 weixin_bot.py login）拿到新 token，"
                        "更新到 Sensor 设置后热重启我。"})
                    time.sleep(3600)
                else:
                    time.sleep(2)
                continue
            if r.get("get_updates_buf"):
                buf = r["get_updates_buf"]
                save_buf(buf)
            for m in r.get("msgs", []):
                handle_msg(m)
        except Exception as e:
            log(f"监听崩溃，5秒后重试: {e}")
            time.sleep(5)


def send_image_to_weixin(to_user, ctx, data: bytes):
    """微信发图: getuploadurl -> AES加密上传CDN -> sendmessage 带 image_item"""
    import hashlib
    aeskey = secrets.token_bytes(16)
    filekey = secrets.token_hex(16)
    ct = aes_ecb_encrypt(data, aeskey)
    rawsize, filesize = len(data), len(ct)
    rawfilemd5 = hashlib.md5(data).hexdigest()
    r = post("/ilink/bot/getuploadurl", {"filekey": filekey, "media_type": 1,
             "to_user_id": to_user, "rawsize": rawsize, "rawfilemd5": rawfilemd5,
             "filesize": filesize, "no_need_thumb": True, "aeskey": aeskey.hex(),
             "base_info": base_info()}, _TOKEN, timeout=20)
    url = (r.get("upload_full_url") or "").strip() or (r.get("upload_param") or "")
    if not url:
        raise Exception(f"getuploadurl 无上传URL: {json.dumps(r, ensure_ascii=False)[:200]}")
    req = urllib.request.Request(url, data=ct, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        download_param = resp.headers.get("x-encrypted-param", "")
    if not download_param:
        raise Exception("CDN 上传响应缺少 x-encrypted-param")
    payload = {"msg": {"from_user_id": "", "to_user_id": to_user,
                       "client_id": secrets.token_hex(8),
                       "message_type": 2, "message_state": 2,
                       "item_list": [{"type": 2, "image_item": {
                           "media": {"encrypt_query_param": download_param,
                                     "aes_key": base64.b64encode(aeskey.hex().encode()).decode(),
                                     "encrypt_type": 1},
                           "mid_size": filesize}}],
                       "context_token": ctx, "run_id": ""},
               "base_info": base_info()}
    sr = post("/ilink/bot/sendmessage", payload, _TOKEN, timeout=15)
    ok = sr.get("ret") in (0, None)
    if not ok:
        raise Exception(f"sendmessage ret={sr.get('ret')} errmsg={sr.get('errmsg')}")


def handle_express(params):
    kwargs = params.get("kwargs", {})
    to_user = kwargs.get("wechat_to_user") or _LAST_USER
    ctx = kwargs.get("wechat_context_token") or _REPLY_CTX.get(to_user, "")
    if params.get("type") == "file":
        # Agent 发文件/图片 -> 上传微信
        if not to_user or not ctx:
            log(f"⚠️ [Wechat Sensor] 无路由上下文，丢弃文件: {params.get('name', '')[:30]}")
            return
        import base64 as _b64
        name = params.get("name", "file")
        data = _b64.b64decode(params.get("content_b64", ""))
        if not data:
            log(f"⚠️ [Wechat Sensor] 文件内容为空: {name}")
            return
        log(f"📎 [Wechat Sensor] 准备发送文件: {name} ({len(data)} bytes)")

        def _send_file():
            try:
                mime = params.get("mime", "") or ""
                if mime.startswith("image/") or name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                    send_image_to_weixin(to_user, ctx, data)
                    log(f"✅ [Wechat Sensor] 图片已发送: {name} ({len(data)} bytes)")
                else:
                    log(f"⚠️ [Wechat Sensor] 非图片文件 {name} ({mime}) 发送暂未实现(v2)")
            except Exception as e:
                log(f"❌ [Wechat Sensor] 发送文件失败 {name}: {e}")
        threading.Thread(target=_send_file, daemon=True).start()
        return
    message = params.get("message", "")
    if not message:
        return
    if not to_user or not ctx:
        log(f"⚠️ [Wechat Sensor] 无路由上下文，丢弃回复: {message[:30]}")
        return

    def _send():
        payload = {"msg": {"from_user_id": "", "to_user_id": to_user,
                           "client_id": secrets.token_hex(8),
                           "message_type": 2, "message_state": 2,
                           "item_list": [{"type": 1, "text_item": {"text": message}}],
                           "context_token": ctx, "run_id": ""},
                   "base_info": base_info()}
        sr = post("/ilink/bot/sendmessage", payload, _TOKEN, timeout=15)
        ok = sr.get("ret") in (0, None)
        if ok:
            log(f"✅ [Wechat Sensor] 回复成功 -> {to_user}: {message[:30]}")
        else:
            log(f"❌ [Wechat Sensor] 回复失败 -> {to_user}: {message[:30]} ({sr.get('errmsg')})")
    threading.Thread(target=_send, daemon=True).start()


if __name__ == "__main__":
    if check_auth():
        threading.Thread(target=event_listener, daemon=True).start()
    # 无 token 时: check_auth 已发起扫码登录, poll_qr_login 扫码成功后自动启动监听
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            if req.get("method") == "express":
                handle_express(req["params"])
        except json.JSONDecodeError:
            pass
        except Exception as e:
            log(f"处理 express 异常: {e}")
