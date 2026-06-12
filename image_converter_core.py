"""
图片格式批量转换 — 核心模块
提供常量定义、单文件转换、文件夹扫描、批量转换器
"""

import os
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# 依赖检查
# ---------------------------------------------------------------------------
from PIL import Image

HEIC_AVAILABLE = False
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
ALL_FORMATS = ["PNG", "JPEG", "TIFF", "BMP", "WEBP", "ICO"]
if HEIC_AVAILABLE:
    ALL_FORMATS.insert(0, "HEIC")

FORMAT_EXT_MAP = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "TIFF": ".tiff",
    "BMP": ".bmp",
    "WEBP": ".webp",
    "ICO": ".ico",
    "HEIC": ".heic",
}

EXT_FORMAT_MAP = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".tiff": "TIFF",
    ".tif": "TIFF",
    ".bmp": "BMP",
    ".webp": "WEBP",
    ".ico": "ICO",
    ".heic": "HEIC",
    ".heif": "HEIC",
}

READABLE_EXTS = set(EXT_FORMAT_MAP.keys())

FORMAT_HINTS = {
    "PNG":   "无损压缩，支持透明通道，适合图标/截图",
    "JPEG":  "有损压缩（质量95），体积小，适合照片",
    "TIFF":  "LZW 无损压缩，适合印刷/存档",
    "BMP":   "无压缩，文件较大，兼容性最好",
    "WEBP":  "无损模式，体积小，支持透明通道",
    "ICO":   "Windows 图标格式，自动缩放到 256x256，支持透明",
    "HEIC":  "苹果设备常用，压缩率高（需 pillow-heif）",
}


# ---------------------------------------------------------------------------
# 单文件转换
# ---------------------------------------------------------------------------
def convert_image(src_path: str, dst_path: str, target_format: str) -> None:
    """将单张图片转换为指定格式，保存到 dst_path。"""
    img = Image.open(src_path)
    exif_data = img.info.get("exif", b"")

    # JPEG / BMP 不支持透明通道 → 合成白底
    if target_format in ("JPEG", "BMP"):
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode == "P":
            img = img.convert("RGB")

    # ICO 格式特殊处理：缩放到 256x256，需要 RGBA/RGB
    if target_format == "ICO":
        # 缩放到 ICO 标准最大尺寸 256x256
        if img.size != (256, 256):
            img.thumbnail((256, 256), Image.LANCZOS)
        if img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA")

    kwargs: dict = {}
    if target_format == "PNG":
        kwargs["compress_level"] = 9
    elif target_format == "JPEG":
        kwargs.update({"quality": 95, "optimize": True})
        if exif_data:
            kwargs["exif"] = exif_data
    elif target_format == "TIFF":
        kwargs["compression"] = "tiff_lzw"
        if exif_data:
            kwargs["exif"] = exif_data
    elif target_format == "WEBP":
        kwargs["lossless"] = True
    elif target_format == "HEIC":
        kwargs["quality"] = 95
    elif target_format == "ICO":
        # 生成多尺寸图标，兼容各种使用场景
        kwargs["sizes"] = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]

    img.save(dst_path, format=target_format, **kwargs)


# ---------------------------------------------------------------------------
# 文件夹扫描
# ---------------------------------------------------------------------------
def scan_folder(folder: str, fmt_filter: str = "全部格式") -> list[str]:
    """扫描文件夹内符合格式过滤条件的图片文件路径列表。"""
    files: list[str] = []
    for name in sorted(os.listdir(folder)):
        ext = Path(name).suffix.lower()
        if ext not in READABLE_EXTS:
            continue
        if fmt_filter != "全部格式" and EXT_FORMAT_MAP.get(ext) != fmt_filter:
            continue
        files.append(os.path.join(folder, name))
    return files


# ---------------------------------------------------------------------------
# 批量转换器（回调驱动，线程无关）
# ---------------------------------------------------------------------------
class BatchConverter:
    """批量图片格式转换器 —— 不依赖任何 GUI 框架，通过回调报告进度。

    用法::

        converter = BatchConverter(
            source_files=["a.png", "b.jpg"],
            dst_folder="/out",
            target_format="WEBP",
        )
        converter.on_log = print
        converter.on_progress = lambda idx, total, msg: ...
        converter.on_done = lambda success, errors: ...

        threading.Thread(target=converter.run, daemon=True).start()
    """

    def __init__(
        self,
        source_files: list[str],
        dst_folder: str,
        target_format: str,
    ) -> None:
        self.source_files = source_files
        self.dst_folder = dst_folder
        self.target_format = target_format
        self.stop_flag = threading.Event()

        # 回调
        self.on_log: object = None       # (msg: str) -> None
        self.on_progress: object = None  # (frac: float, text: str) -> None
        self.on_done: object = None      # (success: int, errors: list[tuple[str, str]]) -> None

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """请求停止转换（线程安全）。"""
        self.stop_flag.set()

    # ------------------------------------------------------------------
    def run(self) -> None:
        """执行批量转换（通常在后台线程中调用）。"""
        target_ext = FORMAT_EXT_MAP[self.target_format]
        total = len(self.source_files)
        success = 0
        errors: list[tuple[str, str]] = []

        for idx, src_path in enumerate(self.source_files, start=1):
            if self.stop_flag.is_set():
                self._emit_log("⛔ 用户已停止转换。")
                break

            name = Path(src_path).stem
            dst_path = os.path.join(self.dst_folder, name + target_ext)

            # 避免覆盖源文件
            if os.path.normpath(src_path) == os.path.normpath(dst_path):
                dst_path = os.path.join(self.dst_folder, name + "_converted" + target_ext)

            try:
                convert_image(src_path, dst_path, self.target_format)
                success += 1
                self._emit_log(
                    f"✅  [{idx}/{total}]  {Path(src_path).name}  →  {Path(dst_path).name}"
                )
            except Exception as exc:
                errors.append((Path(src_path).name, str(exc)))
                self._emit_log(
                    f"❌  [{idx}/{total}]  {Path(src_path).name}  失败：{exc}"
                )

            self._emit_progress(idx / total, f"{idx} / {total}  正在处理…")

        # 汇总
        self._emit_log("━" * 50)
        done_text = f"完成！成功 {success} 个，失败 {len(errors)} 个"
        if self.stop_flag.is_set():
            done_text += "（已停止）"
        self._emit_log(done_text)
        self._emit_progress(
            1.0 if not self.stop_flag.is_set() else idx / total,
            done_text,
        )

        if self.on_done is not None:
            self.on_done(success, errors)  # type: ignore[operator]

    # ------------------------------------------------------------------
    def _emit_log(self, msg: str) -> None:
        if self.on_log is not None:
            self.on_log(msg)  # type: ignore[operator]

    def _emit_progress(self, frac: float, text: str) -> None:
        if self.on_progress is not None:
            self.on_progress(frac, text)  # type: ignore[operator]
