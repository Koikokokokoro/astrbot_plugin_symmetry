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

def _make_image_component_from_path(path: str):
    """
    尝试把本地文件 path 转成 Comp.Image 的组件。
    优先使用 Comp.Image.fromFile，如果不可用，尝试用 bytes 方式创建（fromBytes/fromData/from_data/fromData）。
    若都不可用，抛出异常。
    """
    if not hasattr(Comp, "Image"):
        raise AttributeError("Comp.Image 不存在")
    Img = Comp.Image
    # 优先 fromFile
    if hasattr(Img, "fromFile"):
        return Img.fromFile(path)
    # 读 bytes
    with open(path, "rb") as f:
        b = f.read()
    # 尝试多种命名
    if hasattr(Img, "fromBytes"):
        return Img.fromBytes(b)
    if hasattr(Img, "from_data"):
        return Img.from_data(b)
    if hasattr(Img, "fromData"):
        return Img.fromData(b)
    # 若没有可用方法，抛出
    raise AttributeError("Comp.Image 没有 fromFile/fromBytes/fromData 等可用方法")

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

            # === 自定义对称实现：侧边/上半/左上->右下（替换原先的 transpose/rotate 行为） ===

        def mirror_left_to_right(img: Image.Image) -> Image.Image:
            w, h = img.size
            left_w = w // 2
            if left_w == 0:
                return img.copy()
            left = img.crop((0, 0, left_w, h))
            mirrored = left.transpose(Image.FLIP_LEFT_RIGHT)
            right_w = w - left_w
            if mirrored.size[0] != right_w or mirrored.size[1] != h:
                mirrored = mirrored.resize((right_w, h), resample=Image.LANCZOS)
            out = img.copy()
            out.paste(mirrored, (w - right_w, 0), mirrored if mirrored.mode in ("RGBA", "LA") else None)
            return out

        def mirror_top_to_bottom(img: Image.Image) -> Image.Image:
            w, h = img.size
            top_h = h // 2
            if top_h == 0:
                return img.copy()
            top = img.crop((0, 0, w, top_h))
            mirrored = top.transpose(Image.FLIP_TOP_BOTTOM)
            bottom_h = h - top_h
            if mirrored.size[1] != bottom_h or mirrored.size[0] != w:
                mirrored = mirrored.resize((w, bottom_h), resample=Image.LANCZOS)
            out = img.copy()
            out.paste(mirrored, (0, h - bottom_h), mirrored if mirrored.mode in ("RGBA", "LA") else None)
            return out

        def mirror_center_quadrant(img: Image.Image) -> Image.Image:
            w, h = img.size
            left_w = w // 2
            top_h = h // 2
            if left_w == 0 or top_h == 0:
                return img.copy()
            tl = img.crop((0, 0, left_w, top_h))
            # 左上 -> 右下 采用旋转 180 度（等价于中心对称）
            mirrored = tl.rotate(180)
            target_w = w - left_w
            target_h = h - top_h
            if mirrored.size != (target_w, target_h):
                mirrored = mirrored.resize((target_w, target_h), resample=Image.LANCZOS)
            out = img.copy()
            out.paste(mirrored, (w - target_w, h - target_h), mirrored if mirrored.mode in ("RGBA", "LA") else None)
            return out

        try:
            if mode == "lr":
                out = mirror_left_to_right(im)
            elif mode == "ud":
                out = mirror_top_to_bottom(im)
            else:
                out = mirror_center_quadrant(im)
        except Exception as e:
            logger.error(f"对称处理失败: {e}")
            yield event.plain_result("图片对称处理出错。")
            return

            # 保存到临时文件并发送（尽量兼容 Comp.Image 的多种构造器）
        tmpdir = tempfile.gettempdir()
        fname = _randname("png")
        out_path = os.path.join(tmpdir, fname)
        try:
            out.save(out_path, format="PNG")
        except Exception as e:
            logger.error(f"保存临时图片失败: {e}")
            yield event.plain_result("保存临时图片失败。")
            return

        try:
            img_comp = _make_image_component_from_path(out_path)
            chain = [img_comp]
            yield event.chain_result(chain)
        except Exception as e:
            logger.error(f"发送图片失败: {e}")
            yield event.plain_result("发送图片失败：当前运行时不支持直接以本地文件或 bytes 构造 Image 组件。")
        finally:
            try:
                os.remove(out_path)
            except Exception:
                pass