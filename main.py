from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

import os
import io
import tempfile
import random
import string
import asyncio

# Pillow & requests
try:
    from PIL import Image
except Exception:
    Image = None

try:
    import requests
except Exception:
    requests = None


def _randname(suffix="png"):
    return "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(12)) + "." + suffix


async def load_bytes(path_or_url: str, timeout: float = 10.0) -> bytes | None:
    """
    简易异步下载/读取函数：
    若 path_or_url 以 http/https 开头，使用 requests 在线程池中下载，否则尝试作为本地路径读取
    返回 bytes 或 None
    """
    if not path_or_url:
        return None
    s = str(path_or_url)
    if s.startswith("http://") or s.startswith("https://"):
        if requests is None:
            return None
        try:
            def _get():
                r = requests.get(s, timeout=timeout)
                if r.status_code == 200:
                    return r.content
                return None
            return await asyncio.to_thread(_get)
        except Exception:
            return None
    else:
        # 本地文件路径
        try:
            if os.path.exists(s):
                def _read():
                    with open(s, "rb") as f:
                        return f.read()
                return await asyncio.to_thread(_read)
        except Exception:
            return None
    return None


async def get_first_image(event: AstrMessageEvent) -> bytes | None:
    """
    获取消息里的第一张图并以 bytes 返回（优先引用的消息，再看当前消息）。
    顺序：
      1) Reply 段中查找 Image 段（常见属性 url/file/data）
      2) 当前消息中的 Image 段
    找不到返回 None。
    """
    msgs = event.get_messages()

    # 引用
    reply_seg = next((s for s in msgs if type(s).__name__.lower() == "reply"), None)
    if reply_seg:
        # reply_seg.chain 可能存在（取决于实现）
        chain = getattr(reply_seg, "chain", None)
        if chain:
            for seg in chain:
                # 优先尝试 seg.url / seg.file / seg.data
                url = getattr(seg, "url", None)
                if url:
                    img = await load_bytes(url)
                    if img:
                        return img
                filep = getattr(seg, "file", None)
                if filep:
                    img = await load_bytes(filep)
                    if img:
                        return img
                data = getattr(seg, "data", None)
                if isinstance(data, (bytes, bytearray)):
                    return bytes(data)
                # 最后尝试类型名判断并尝试 string->url
                try:
                    s = str(seg)
                    if s.startswith("http"):
                        img = await load_bytes(s)
                        if img:
                            return img
                except Exception:
                    pass

    # 当前消息
    for seg in msgs:
        # 跳过 the reply segment itself
        if type(seg).__name__.lower() == "reply":
            continue
        # 判断是否可能为图片段（尝试常见属性）
        url = getattr(seg, "url", None)
        if url:
            img = await load_bytes(url)
            if img:
                return img
        filep = getattr(seg, "file", None)
        if filep:
            img = await load_bytes(filep)
            if img:
                return img
        data = getattr(seg, "data", None)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        # 兼容 Comp.Image.fromURL 这类表示法，fallback: str(seg)
        try:
            s = str(seg)
            if s.startswith("http"):
                img = await load_bytes(s)
                if img:
                    return img
        except Exception:
            pass

    return None


@register("sym", "Symmetry", "对图片进行对称处理", "1.0.0")
class SymmetryByReply(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("sym")
    async def sym(self, event: AstrMessageEvent):
        """
        使用方法：
        回复图片消息或在消息中加入图片并发送：/sym <参数>
        插件会读取被引用消息中的第一张图片（或当前消息中的第一张图片），处理并发送。
        """
        if Image is None:
            yield event.plain_result("图片处理依赖缺失：Pillow 未安装，无法处理图片。")
            return
        if requests is None:
            # load_bytes 依赖 requests 下载远程图片
            yield event.plain_result("图片下载依赖缺失：requests 未安装，无法获取远程图片。")
            return

        # 解析参数
        param = None
        for seg in event.get_messages():
            if isinstance(seg, Comp.Plain):
                txt = getattr(seg, "text", None)
                if txt is None:
                    try:
                        txt = str(seg)
                    except Exception:
                        txt = ""
                if txt and txt.strip():
                    parts = txt.strip().split()
                    # 支持 "/sym 左右" 或 直接 "左右"
                    if parts[0].startswith("/sym"):
                        if len(parts) >= 2:
                            param = parts[1].lower()
                            break
                        else:
                            continue
                    else:
                        param = parts[0].lower()
                        break

        if not param:
            yield event.plain_result("用法：回复一条含图片的消息，然后发送 /sym <左右|上下|中心> 。")
            return

        # 参数设置
        if param in ("左右", "lr", "left", "左右对称"):
            mode = "lr"
        elif param in ("上下", "ud", "vertical", "updown", "上下对称"):
            mode = "ud"
        elif param in ("中心", "center", "rot", "180", "中心对称"):
            mode = "center"
        else:
            yield event.plain_result("参数无效。可选：左右、上下、中心（或 lr / ud / center）。")
            return

        # 获取图片 bytes
        img_bytes = await get_first_image(event)
        if not img_bytes:
            yield event.plain_result("找不到图片：请引用（回复）一条包含图片的消息，或在当前消息中附带图片。")
            return

        # 打开并处理图片
        try:
            im = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        except Exception as e:
            logger.error(f"打开图片失败: {e}")
            yield event.plain_result("打开图片失败（格式可能不支持）。")
            return

        try:
            if mode == "lr":
                out = im.transpose(Image.FLIP_LEFT_RIGHT)
            elif mode == "ud":
                out = im.transpose(Image.FLIP_TOP_BOTTOM)
            else:
                out = im.rotate(180, expand=False)

            # 保存并发送
            tmpdir = tempfile.gettempdir()
            fname = _randname("png")
            out_path = os.path.join(tmpdir, fname)
            out.save(out_path, format="PNG")

            chain = [Comp.Image.fromFile(out_path)]
            yield event.chain_result(chain)

            # 清理临时文件
            try:
                os.remove(out_path)
            except Exception:
                pass

        except Exception as e:
            logger.error(f"图片处理失败: {e}")
            yield event.plain_result("图片处理失败。")
            return
