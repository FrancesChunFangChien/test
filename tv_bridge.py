"""古典 Android TV 遙控器 — 區域網路橋接程式 (LAN bridge)

瀏覽器網頁無法直接開啟 TCP/TLS 連線，所以由這支程式代替網頁，
用 Android TV Remote protocol v2（與 Google TV 手機 App 相同的協定）控制電視。

用法:
    pip install -r requirements.txt
    python tv_bridge.py
然後用同一個 Wi-Fi 下的手機或電腦瀏覽器開啟畫面上顯示的網址。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
from pathlib import Path

from aiohttp import web
from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "android-tv-remote.html"
CONFIG_FILE = BASE_DIR / "atv_config.json"
CERT_FILE = BASE_DIR / "atv_cert.pem"
KEY_FILE = BASE_DIR / "atv_key.pem"
TIMEOUT = 15  # 秒；IP 填錯時不要卡住太久
CLIENT_NAME = "Classic Remote"  # 會顯示在電視的配對畫面；憑證只接受英數字

# 網頁按鍵 → Android 鍵碼 (KEYCODE_*)
KEYS = {
    "power": "POWER",
    "input": "TV_INPUT",
    "assistant": "ASSIST",
    "up": "DPAD_UP",
    "down": "DPAD_DOWN",
    "left": "DPAD_LEFT",
    "right": "DPAD_RIGHT",
    "ok": "DPAD_CENTER",
    "back": "BACK",
    "home": "HOME",
    "guide": "GUIDE",
    "volup": "VOLUME_UP",
    "voldown": "VOLUME_DOWN",
    "mute": "VOLUME_MUTE",
    "settings": "SETTINGS",
    "chup": "CHANNEL_UP",
    "chdown": "CHANNEL_DOWN",
    "rew": "MEDIA_REWIND",
    "play": "MEDIA_PLAY_PAUSE",
    "ff": "MEDIA_FAST_FORWARD",
}

log = logging.getLogger("tv_bridge")
CONNECTOR = web.AppKey("connector", asyncio.Task)


class Bridge:
    def __init__(self) -> None:
        self.remote: AndroidTVRemote | None = None
        self.host: str | None = None
        self.paired = False
        self.connected = False
        self.pairing = False

    def load_config(self) -> None:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self.host = data.get("host")
            self.paired = bool(data.get("paired"))

    def save_config(self) -> None:
        CONFIG_FILE.write_text(json.dumps({"host": self.host, "paired": self.paired}), encoding="utf-8")

    async def _make_remote(self, host: str) -> AndroidTVRemote:
        if self.remote:
            self.remote.disconnect()
        self.connected = False
        self.remote = AndroidTVRemote(CLIENT_NAME, str(CERT_FILE), str(KEY_FILE), host)
        self.remote.add_is_available_updated_callback(self._on_available)
        await self.remote.async_generate_cert_if_missing()
        return self.remote

    def _on_available(self, available: bool) -> None:
        self.connected = available
        log.info("電視連線狀態: %s", "已連線" if available else "中斷，重新連線中…")

    async def connect(self) -> None:
        """用已配對的憑證連線；失敗會拋出例外。"""
        remote = await self._make_remote(self.host)
        try:
            await asyncio.wait_for(remote.async_connect(), TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise CannotConnect(f"連線逾時 ({TIMEOUT} 秒)") from exc
        remote.keep_reconnecting(self._on_invalid_auth)
        self.connected = True
        log.info("已連線到電視 %s", self.host)

    def _on_invalid_auth(self) -> None:
        log.warning("電視拒絕憑證，需要重新配對")
        self.paired = False
        self.connected = False
        self.save_config()

    async def start_pairing(self, host: str) -> None:
        self.host = host
        self.paired = False
        self.save_config()
        remote = await self._make_remote(host)
        try:
            await asyncio.wait_for(remote.async_start_pairing(), TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise CannotConnect(f"連線逾時 ({TIMEOUT} 秒)") from exc
        self.pairing = True

    async def finish_pairing(self, code: str) -> None:
        if not self.remote or not self.pairing:
            raise ConnectionClosed("尚未開始配對")
        await self.remote.async_finish_pairing(code)
        self.pairing = False
        self.paired = True
        self.save_config()
        await self.connect()

    def status(self) -> dict:
        r = self.remote if self.connected else None
        return {
            "host": self.host,
            "paired": self.paired,
            "pairing": self.pairing,
            "connected": self.connected,
            "is_on": r.is_on if r else None,
            "current_app": r.current_app if r else None,
            "volume": r.volume_info if r else None,
            "device": r.device_info if r else None,
        }


bridge = Bridge()
routes = web.RouteTableDef()


def error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


@routes.get("/")
async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(HTML_FILE)


@routes.get("/api/status")
async def status(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, **bridge.status()})


@routes.post("/api/setup")
async def setup(request: web.Request) -> web.Response:
    host = str((await request.json()).get("host", "")).strip()
    if not host:
        return error("請輸入電視的 IP 位址")
    try:
        await bridge.start_pairing(host)
    except CannotConnect:
        return error(f"連不到 {host}：請確認 IP 正確、電視已開機且在同一個 Wi-Fi")
    except (ConnectionClosed, OSError) as exc:
        return error(f"配對連線失敗：{exc}")
    except Exception as exc:  # 未預期的錯誤也回傳 JSON，網頁才能顯示原因
        log.exception("開始配對時發生錯誤")
        return error(f"配對時發生錯誤：{exc}", 500)
    return web.json_response({"ok": True, "message": "電視上應該出現 6 位數配對碼"})


@routes.post("/api/pair")
async def pair(request: web.Request) -> web.Response:
    code = str((await request.json()).get("code", "")).strip()
    if not code:
        return error("請輸入電視上顯示的配對碼")
    if not bridge.pairing:
        return error("請先按「開始配對」")
    try:
        await bridge.finish_pairing(code)
    except InvalidAuth:
        return error("配對碼錯誤，請重新開始配對")
    except ConnectionClosed:
        bridge.pairing = False
        return error("配對已中斷（可能在電視上按了取消），請重新開始配對")
    except (CannotConnect, OSError) as exc:
        return error(f"配對成功但連線失敗：{exc}")
    except Exception as exc:
        log.exception("完成配對時發生錯誤")
        return error(f"配對時發生錯誤：{exc}", 500)
    return web.json_response({"ok": True, "message": "配對完成！"})


@routes.post("/api/key")
async def key(request: web.Request) -> web.Response:
    name = str((await request.json()).get("key", ""))
    if name not in KEYS:
        return error(f"未知按鍵：{name}")
    if not bridge.remote or not bridge.connected:
        return error("尚未連線到電視", 503)
    try:
        bridge.remote.send_key_command(KEYS[name])
    except ConnectionClosed:
        return error("與電視的連線已中斷，正在重新連線", 503)
    return web.json_response({"ok": True})


@routes.post("/api/app")
async def app(request: web.Request) -> web.Response:
    link = str((await request.json()).get("link", "")).strip()
    if not link:
        return error("沒有設定 App 連結")
    if not bridge.remote or not bridge.connected:
        return error("尚未連線到電視", 503)
    try:
        bridge.remote.send_launch_app_command(link)
    except ConnectionClosed:
        return error("與電視的連線已中斷，正在重新連線", 503)
    return web.json_response({"ok": True})


@routes.post("/api/text")
async def text(request: web.Request) -> web.Response:
    value = str((await request.json()).get("text", ""))
    if not bridge.remote or not bridge.connected:
        return error("尚未連線到電視", 503)
    try:
        bridge.remote.send_text(value)
    except ConnectionClosed:
        return error("與電視的連線已中斷，正在重新連線", 503)
    return web.json_response({"ok": True})


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # 不會真的送出封包，只用來找出本機區網 IP
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def connect_until_ready() -> None:
    """開機時電視可能還沒開，持續重試直到第一次連線成功（之後由函式庫自動重連）。"""
    delay = 5
    while bridge.host and bridge.paired and not bridge.connected and not bridge.pairing:
        try:
            await bridge.connect()
            return
        except InvalidAuth:
            bridge._on_invalid_auth()
            return
        except (CannotConnect, ConnectionClosed) as exc:
            log.warning("暫時連不到電視 %s（%s），%d 秒後重試", bridge.host, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def on_startup(app: web.Application) -> None:
    bridge.load_config()
    app[CONNECTOR] = asyncio.create_task(connect_until_ready())


async def on_cleanup(app: web.Application) -> None:
    app[CONNECTOR].cancel()
    if bridge.remote:
        bridge.remote.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="古典 Android TV 遙控器橋接程式")
    parser.add_argument("--port", type=int, default=8765, help="網頁伺服器埠號 (預設 8765)")
    parser.add_argument("--debug", action="store_true", help="顯示詳細記錄")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s %(message)s")

    app = web.Application()
    app.add_routes(routes)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    print("\n  ❦ 古典 Android TV 遙控器已啟動 ❦")
    print(f"  手機／電腦請開啟： http://{lan_ip()}:{args.port}")
    print("  （按 Ctrl+C 結束）\n")
    web.run_app(app, host="0.0.0.0", port=args.port, print=None)


if __name__ == "__main__":
    main()
