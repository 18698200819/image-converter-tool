"""
图片格式批量转换 — 核心模块
提供常量定义、单文件转换、文件夹扫描、批量转换器
"""

import os
import subprocess
import shutil
import tempfile
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

# Potrace 是可选的，用于 SVG 矢量化
POTRACE_PATH = shutil.which("potrace")
POTRACE_AVAILABLE = POTRACE_PATH is not None

# VTrace：纯 Python 矢量化方案（无需安装 potrace.exe）
VTRACE_AVAILABLE = False
try:
    import vtracer  # noqa: F401
    VTRACE_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
ALL_FORMATS = ["PNG", "JPEG", "TIFF", "BMP", "WEBP", "ICO", "SVG"]
if HEIC_AVAILABLE:
    ALL_FORMATS.insert(0, "HEIC")

FORMAT_EXT_MAP = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "TIFF": ".tiff",
    "BMP": ".bmp",
    "WEBP": ".webp",
    "ICO": ".ico",
    "SVG": ".svg",
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
    ".svg": "SVG",
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
    "SVG":   "矢量图形（VTrace 矢量化），适合流程图/科研图，无损缩放",
    "HEIC":  "苹果设备常用，压缩率高（需 pillow-heif）",
}


# ---------------------------------------------------------------------------
# SVG 矢量化（基于 VTrace / Potrace 双方案）
# ---------------------------------------------------------------------------
def _raster_to_svg_via_vtrace(src_path: str, dst_path: str) -> None:
    """使用 VTrace（纯 Python 库）将光栅图矢量化输出为 SVG 文件。

    Colormode 自动选择：
      - RGB / RGBA 彩色图 → colormode="color" 保留颜色
      - 灰度 / 调色板 / 二值图 → colormode="binary" 线稿模式

    无需安装任何外部 .exe 工具。
    """
    import vtracer

    img = Image.open(src_path)
    original_mode = img.mode

    # 判断颜色模式：非灰度/二值图就保留彩色
    is_color = original_mode in ("RGB", "RGBA", "P", "CMYK", "YCbCr")

    # 不需要预先灰度处理 —— 直接保存临时 PNG，由 VTrace 内部处理
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
        tmp_png_path = tmp_png.name
        # 如果原图是 RGBA 等模式，先转 RGB 避免透明度干扰矢量化颜色分层
        if is_color and original_mode != "RGB":
            img = img.convert("RGB")
        img.save(tmp_png_path, format="PNG")

    colormode = "color" if is_color else "binary"

    try:
        vtracer.convert_image_to_svg_py(
            tmp_png_path,
            dst_path,
            colormode=colormode,
            hierarchical="cutout" if is_color else "stacked",
            mode="spline",
            filter_speckle=2,           # 只忽略 2px 以下噪点，保留更多细节
            color_precision=6,          # 颜色量化精度（越大保留越多颜色层）
            layer_difference=8,         # 层差阈值（越小越能分离相近色）
            corner_threshold=60,        # 转角阈值
            length_threshold=2.0,       # 更短线段也保留
            max_iterations=20,          # 更多迭代，更精细
            splice_threshold=45,
            path_precision=1,           # 最高路径精度
        )
    finally:
        try:
            os.unlink(tmp_png_path)
        except OSError:
            pass


def _raster_to_svg_via_potrace(src_path: str, dst_path: str,
                               potrace_binary: str = "potrace") -> None:
    """将光栅图片通过 Potrace 矢量化输出为 SVG 文件。

    处理流程：
        源图 → 转灰度 → 增强对比度 → BMP → Potrace → SVG
    这个流程对流程图、科研图等线条清晰的内容效果最好。
    """
    if POTRACE_PATH is None:
        raise RuntimeError(
            "未找到 Potrace，请先下载安装：\n"
            "https://potrace.sourceforge.net/#downloading\n"
            "将 potrace.exe 放到本工具所在目录，或添加到系统 PATH。"
        )

    img = Image.open(src_path)
    original_size = img.size

    # 转灰度 → 自适应阈值二值化（大津法近似）
    if img.mode != "L":
        img = img.convert("L")

    # 增强对比度：拉伸直方图
    import PIL.ImageOps
    img = PIL.ImageOps.autocontrast(img, cutoff=5)

    # 保存为 BMP（Potrace 要求 BMP 输入）
    with tempfile.NamedTemporaryFile(suffix=".bmp", delete=False) as tmp_bmp:
        tmp_bmp_path = tmp_bmp.name
        img.save(tmp_bmp_path, format="BMP")

    try:
        # 调用 Potrace：矢量化，输出 SVG
        # --flat: 扁平的路径（非贝塞尔曲线，节点更少）
        # --blacklevel: 亮度阈值
        # --turdsize: 忽略小于此值的噪点
        # --opttolerance: 优化容差
        # --alphamax: 转角平滑度
        # --longcurve: 长曲线阈值
        cmd = [
            potrace_binary,
            "-b", "svg",           # 输出 SVG
            "--flat",              # 扁平路径，更适合流程图
            "--turdsize", "8",     # 忽略 8px² 以下噪点
            "--opttolerance", "0.2",  # 适当优化
            "--alphamax", "1.0",   # 转角平滑
            "--longcurve",         # 启用长曲线优化
            "-o", dst_path,
            tmp_bmp_path,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Potrace 失败:\n{result.stderr}")
    finally:
        # 清理临时 BMP
        try:
            os.unlink(tmp_bmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 单文件转换
# ---------------------------------------------------------------------------
def convert_image(src_path: str, dst_path: str, target_format: str) -> None:
    """将单张图片转换为指定格式，保存到 dst_path。"""
    # SVG 矢量化：优先 VTrace（纯 Python），fallback 到 Potrace
    if target_format == "SVG":
        if VTRACE_AVAILABLE:
            _raster_to_svg_via_vtrace(src_path, dst_path)
        elif POTRACE_AVAILABLE:
            _raster_to_svg_via_potrace(src_path, dst_path)
        else:
            raise RuntimeError(
                "SVG 转换需要矢量化引擎。请安装 vtrace：\n"
                "pip install vtrace\n\n"
                "或下载 Potrace 并放入工具目录：\n"
                "https://potrace.sourceforge.net/#downloading"
            )
        return

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
