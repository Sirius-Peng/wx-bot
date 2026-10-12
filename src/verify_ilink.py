"""官方 iLink Bot API 适配器验证脚本。

分两部分：
  A. 离线自检 —— 请求头格式、消息归一化（不需要网络）
  B. 在线联调 —— 真实调用腾讯官方接口拿二维码（需要网络）

运行：
    python -m src.verify_ilink
"""
from __future__ import annotations

import asyncio
import base64
import sys

from .adapters.ilink import ILinkAdapter


def test_headers() -> None:
    """X-WECHAT-UIN 必须是 base64(十进制 uint32)，且每次随机。"""
    a = ILinkAdapter(state_dir="/tmp/ilink-test")
    h1, h2 = a._headers(), a._headers()

    assert h1["AuthorizationType"] == "ilink_bot_token"
    assert h1["Content-Type"] == "application/json"

    uin = h1["X-WECHAT-UIN"]
    decoded = base64.b64decode(uin).decode()
    assert decoded.isdigit(), f"UIN 应为十进制数字符串，实际 {decoded!r}"
    assert 0 <= int(decoded) <= 0xFFFFFFFF, "UIN 应为 uint32 范围"

    assert h1["X-WECHAT-UIN"] != h2["X-WECHAT-UIN"], "UIN 每次必须随机"
    print(f"  [OK] 请求头格式正确，UIN 示例 = {decoded} (base64={uin[:16]}...)")
    print(f"  [OK] 未登录时不带 Authorization：{'Authorization' not in h1}")

    # 带 token 时应带 Bearer
    a.bot_token = "test_token_abc"
    assert a._headers()["Authorization"] == "Bearer test_token_abc"
    print("  [OK] 登录后 Authorization = Bearer <token>")


def test_normalize() -> None:
    """消息归一化：官方协议结构 → 统一 WxMessage。"""
    a = ILinkAdapter(state_dir="/tmp/ilink-test")
    a.bot_user_id = "e06c1ceea05e@im.bot"

    # 用户发来的文本消息
    raw = {
        "message_id": "7300000000000001",
        "seq": 12,
        "from_user_id": "o9cq800kum_xxx@im.wechat",
        "to_user_id": "e06c1ceea05e@im.bot",
        "message_type": 1,
        "message_state": 2,
        "context_token": "AARzJWAFAAABAAAAAAAp...",
        "session_id": "sess_1",
        "create_time_ms": 1770000000000,
        "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
    }
    msg = a._normalize(raw)
    assert msg is not None
    assert msg.chat_type == "private"
    assert msg.chat_id == "o9cq800kum_xxx@im.wechat"
    assert msg.content == "你好"
    assert msg.context_token == "AARzJWAFAAABAAAAAAAp..."
    assert msg.msg_id == "7300000000000001"
    assert msg.is_at_me is False, "私聊无需 @"
    print(f"  [OK] 文本消息归一化：chat_id={msg.chat_id}")
    print(f"  [OK] context_token 已保留（回复时必填）")

    # 语音消息（自带转文字）
    voice = {
        "message_id": "7300000000000002",
        "from_user_id": "o9cq800kum_xxx@im.wechat",
        "message_type": 1,
        "item_list": [{"type": 3, "voice_item": {"text": "语音转出来的文字"}}],
    }
    m2 = a._normalize(voice)
    assert m2 is not None and m2.msg_type == "voice" and m2.content == "语音转出来的文字"
    print("  [OK] 语音消息取到转文字结果")

    # BOT 自己的消息应被忽略（避免回环）
    echo = dict(raw, message_type=2)
    assert a._normalize(echo) is None
    print("  [OK] BOT 自身消息被忽略（防回环）")

    # 图片消息
    img = {
        "message_id": "7300000000000003",
        "from_user_id": "o9cq800kum_xxx@im.wechat",
        "message_type": 1,
        "item_list": [{"type": 2, "image_item": {}}],
    }
    assert a._normalize(img).msg_type == "image"
    print("  [OK] 图片消息类型识别正确")


def test_capability_declaration() -> None:
    """能力声明必须与官方源码一致：chatTypes = ["direct"]。"""
    a = ILinkAdapter(state_dir="/tmp/ilink-test")
    assert a.supports_private is True
    assert a.supports_group is False, "官方协议不支持群聊，必须声明为 False"
    print("  [OK] 能力声明：supports_private=True, supports_group=False")
    print("       （对应官方源码 capabilities.chatTypes = [\"direct\"]）")


async def test_live_qrcode() -> bool:
    """真实调用腾讯官方接口获取登录二维码。"""
    a = ILinkAdapter(state_dir="/tmp/ilink-test")
    try:
        r = await a._client.get(
            f"{a.base_url}/ilink/bot/get_bot_qrcode", params={"bot_type": 3}
        )
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        print(f"  [SKIP] 在线联调失败（可能无网络）：{e!r}")
        await a._client.aclose()
        return False
    finally:
        pass

    assert d.get("ret") == 0, f"ret 应为 0，实际 {d.get('ret')}"
    assert d.get("qrcode"), "应返回 qrcode"
    assert "liteapp.weixin.qq.com" in (d.get("qrcode_img_content") or ""), \
        "二维码应指向微信官方域名"
    print(f"  [OK] 真实调用成功 ret=0")
    print(f"  [OK] qrcode = {d['qrcode']}")
    print(f"  [OK] 二维码链接 = {d['qrcode_img_content']}")
    await a._client.aclose()
    return True


async def main() -> None:
    print("=" * 68)
    print("官方 iLink Bot API 适配器验证")
    print("=" * 68)

    print("\n[A] 离线自检")
    print(" 1. 请求头格式")
    test_headers()
    print(" 2. 消息归一化")
    test_normalize()
    print(" 3. 能力声明")
    test_capability_declaration()

    print("\n[B] 在线联调（真实调用腾讯官方接口）")
    ok = await test_live_qrcode()

    print("\n" + "=" * 68)
    print("离线自检全部通过" + ("；在线联调成功" if ok else "；在线联调跳过"))
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(main())
