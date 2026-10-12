"""NapCat WebUI 客户端 —— 让 QQ 网关的部署与扫码登录也能"一键完成"。

═══════════════════════════════════════════════════════════════════
为什么需要这个模块？

GeWe 那边 Docker 一起来，用户点「获取二维码」就完事了；
NapCat 不一样：

  · 二维码要通过 **WebUI 的 HTTP API** 拿，而 WebUI 需要鉴权；
  · NapCat 的 OneBot 配置有 "Not Login" 前置检查，
    必须**在登录前**把配置写好（这正是 `DockerManager.seed_napcat_config()` 的原因）；
  · 每条扫码链接是一段文本，得自己转成二维码图片
    （用 `src/core/qr.py`，离线生成，不依赖第三方接口）。

鉴权算法（已对真实 NapCat 容器核验）：
    hash = sha256_hex(WEBUI_TOKEN + ".napcat")
    POST /api/auth/login  {"hash": hash}
    → {"code":0, "data":{"Credential": "<base64 json>"}}
    之后请求带 `Authorization: Bearer <Credential>`，有效期 1 小时。

登录相关接口：
    POST /api/QQLogin/GetQQLoginQrcode  → {code:0, data:{qrcode:"<URL>"}}
    POST /api/QQLogin/CheckLoginStatus  → {code:0, data:{isLogin, loginPhase, ...}}

失败响应统一是 {code:非0, message:"..."}，例如 "QQ Is Logined"、"Unauthorized"。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from .http import explain_error, make_client
from .qr import QRDataTooLong, data_url

log = logging.getLogger(__name__)

WEBUI_PORT = 6099


class NapCatWebUI:
    """NapCat WebUI 的轻量客户端（只用到登录与扫码那几个接口）。"""

    def __init__(self, token: str = "", port: int = WEBUI_PORT,
                 host: str = "127.0.0.1"):
        self.token = (token or "").strip()
        self.base = f"http://{host}:{port}"
        self._credential = ""
        self._credential_at = 0.0

    # ── 鉴权 ──────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @staticmethod
    def hash_token(token: str) -> str:
        """`sha256_hex(WEBUI_TOKEN + ".napcat")` —— 算法来自 NapCat 源码。"""
        return hashlib.sha256(f"{token}.napcat".encode("utf-8")).hexdigest()

    async def login(self) -> dict:
        """用 WEBUI_TOKEN 换 Credential（1 小时有效，过期自动重取）。"""
        if not self.token:
            return {"ok": False, "message": "还没有 WebUI Token"}
        if self._credential and time.time() - self._credential_at < 3000:
            return {"ok": True, "message": "已登录"}

        try:
            async with make_client(self.base, timeout=10.0) as c:
                r = await c.post(f"{self.base}/api/auth/login",
                                 json={"hash": self.hash_token(self.token)})
                body = r.json() if r.status_code < 400 else {}
        except Exception as e:
            return {"ok": False, "message": explain_error(e, self.base),
                    "stage": "connect"}

        if r.status_code >= 400 or body.get("code") != 0:
            return {
                "ok": False, "stage": "auth",
                "message": "NapCat 拒绝了 WebUI Token",
                "detail": f"HTTP {r.status_code} {body.get('message') or ''}".strip(),
                "hint": "Token 是我们自动生成并写进容器的，出现这个提示通常说明"
                        "容器里的配置被改过。可点「重新部署」重建容器。",
            }

        cred = ((body.get("data") or {}).get("Credential") or "")
        if not cred:
            return {"ok": False, "stage": "auth", "message": "登录成功但没拿到凭证"}

        self._credential = cred
        self._credential_at = time.time()
        return {"ok": True, "message": "已登录"}

    async def _post(self, path: str, body: Optional[dict] = None) -> dict:
        """带 Bearer 的 POST，返回网关的 JSON（失败也返回，不抛）。"""
        ok = await self.login()
        if not ok.get("ok"):
            err = dict(ok)
            # 连不上时给"先部署"的指引，别让用户以为是自己填错了 Token
            if err.get("stage") == "connect":
                err["hint"] = "NapCat 网关好像没在运行。请先点「一键部署 QQ 网关」。"
            return {"code": -1, "message": ok.get("message", ""), "_err": err}

        try:
            async with make_client(self.base, timeout=20.0) as c:
                r = await c.post(
                    f"{self.base}{path}", json=body or {},
                    headers={"Authorization": f"Bearer {self._credential}"},
                )
                if r.status_code >= 400:
                    return {"code": r.status_code, "message": f"HTTP {r.status_code}"}
                return r.json()
        except Exception as e:
            return {"code": -1, "message": explain_error(e, self.base)}

    # ── 扫码登录 ──────────────────────────────────────────────

    async def qrcode(self) -> dict:
        """取登录二维码。

        返回 `{ok, image, url, message, hint}`；
        `image` 是 SVG data URL，前端可直接塞进 `<img src>`。
        """
        if not self.configured:
            return {"ok": False, "message": "还没有 NapCat WebUI Token，请先完成部署"}

        body = await self._post("/api/QQLogin/GetQQLoginQrcode")
        if body.get("code") != 0:
            err = body.get("_err")
            if err:                       # 连不上 / 鉴权失败，把原因原样带出去
                return {"ok": False, "message": err.get("message", "获取二维码失败"),
                        "detail": err.get("detail", ""), "hint": err.get("hint", "")}
            msg = body.get("message") or "获取二维码失败"
            hint = ""
            if "Logined" in msg or "已登录" in msg:
                hint = "这个 QQ 已经登录过了，直接启动机器人即可。"
            return {"ok": False, "message": msg, "hint": hint}

        url = ((body.get("data") or {}).get("qrcode") or "").strip()
        if not url:
            return {"ok": False, "message": "NapCat 没有返回二维码内容"}

        # 二维码内容是一段 URL，**离线**转成 SVG（不走任何第三方接口）
        try:
            # 静默区取 4 个模块（规范下限），缩放 7 —— 前端缩放到 240px 仍清晰
            image = data_url(url, ec="M", scale=6, border=4)
        except QRDataTooLong:
            return {"ok": False, "message": "二维码内容过长，无法生成"}
        except Exception:
            log.exception("生成二维码失败")
            return {"ok": False, "message": "生成二维码失败"}

        return {
            "ok": True, "image": image, "url": url,
            "message": "请用手机 QQ 扫码登录",
            "hint": "二维码有效期约 2 分钟，过期后重新点「获取二维码」即可。",
        }

    async def check_login(self) -> dict:
        """查询登录状态。"""
        if not self.configured:
            return {"ok": False, "loggedIn": False, "message": "还没有 WebUI Token"}

        body = await self._post("/api/QQLogin/CheckLoginStatus")
        if body.get("code") != 0:
            err = body.get("_err") or {}
            return {"ok": False, "loggedIn": False,
                    "message": err.get("message") or body.get("message") or "查询失败"}

        data = body.get("data") or {}
        logged_in = bool(data.get("isLogin"))
        phase = data.get("loginPhase") or ""
        phase_text = {
            "waiting_qrcode": "等待扫码",
            "waiting_confirm": "已扫码，请在手机上确认",
            "confirmed": "登录成功",
            "login_error": "登录出错",
        }.get(phase, phase)

        return {
            "ok": True,
            "loggedIn": logged_in,
            "offline": bool(data.get("isOffline")),
            "phase": phase,
            "detail": data.get("loginError") or "",
            "message": "登录成功" if logged_in else (phase_text or "等待扫码…"),
        }
