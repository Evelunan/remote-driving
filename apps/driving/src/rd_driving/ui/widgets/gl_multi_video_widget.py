import time

from OpenGL import GL
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

CAMERA_NAMES = ["front", "back", "left", "right"]


class GLMultiVideoWidget(QOpenGLWidget):
    def __init__(self, framebuffer, parent=None, *, stale_frame_ms=500):
        super().__init__(parent)

        self.fb = framebuffer
        if stale_frame_ms <= 0:
            raise ValueError("stale_frame_ms must be positive")
        self._stale_after_s = stale_frame_ms / 1000

        # ===== 渲染配置 =====
        self.layout_mode = "triple"  # grid / horizontal / vertical / triple
        self.scale_mode = "fit"  # fit / fill

        # ===== UI配置=====
        self.padding = 6  # 🔥 视频间距
        self.border_color = QColor(80, 80, 80)
        self.text_bg = QColor(0, 0, 0, 120)

        # ===== OpenGL =====
        self.textures = {}
        self._texture_sizes = {}
        self._frame_ids = {}

        # ===== 数据统计 =====
        self.meta = {name: {"size": None, "fps": 0} for name in CAMERA_NAMES}

        self._fps_counter = {
            name: {"last": time.monotonic(), "count": 0, "fps": 0}
            for name in CAMERA_NAMES
        }

    # ---------------------------
    # 初始化 OpenGL
    # ---------------------------
    def initializeGL(self):
        GL.glEnable(GL.GL_TEXTURE_2D)
        GL.glClearColor(0.08, 0.08, 0.1, 1.0)
        for name in CAMERA_NAMES:
            tex = GL.glGenTextures(1)
            self.textures[name] = tex

            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)

    # ---------------------------
    # FPS统计
    # ---------------------------
    def _update_fps(self, name):
        c = self._fps_counter[name]
        c["count"] += 1

        now = time.monotonic()
        if now - c["last"] >= 1.0:
            c["fps"] = c["count"] / (now - c["last"])
            c["count"] = 0
            c["last"] = now

        self.meta[name]["fps"] = c["fps"]

    # ---------------------------
    # 更新纹理（核心优化）
    # ---------------------------
    def _update_texture(self, name, frame):
        if frame is None:
            return

        h, w, _ = frame.shape

        GL.glBindTexture(GL.GL_TEXTURE_2D, self.textures[name])

        # BGR rows can have any byte width; avoid a reversed-stride RGB copy.
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)

        if self._texture_sizes.get(name) != (w, h):
            GL.glTexImage2D(
                GL.GL_TEXTURE_2D,
                0,
                GL.GL_RGB,
                w,
                h,
                0,
                GL.GL_BGR,
                GL.GL_UNSIGNED_BYTE,
                frame,
            )
            self._texture_sizes[name] = (w, h)
        else:
            GL.glTexSubImage2D(
                GL.GL_TEXTURE_2D, 0, 0, 0, w, h, GL.GL_BGR, GL.GL_UNSIGNED_BYTE, frame
            )

    # ---------------------------
    # 计算缩放（保持比例）
    # ---------------------------
    def _calc_rect(self, view_w, view_h, img_w, img_h):
        if self.scale_mode == "fill":
            scale = max(view_w / img_w, view_h / img_h)
        else:
            scale = min(view_w / img_w, view_h / img_h)

        w = img_w * scale
        h = img_h * scale

        x = (view_w - w) / 2
        y = (view_h - h) / 2

        return x, y, w, h

    def _to_gl(self, x, y, w, h, view_w, view_h):
        x1 = (x / view_w) * 2 - 1
        y1 = 1 - (y / view_h) * 2

        x2 = ((x + w) / view_w) * 2 - 1
        y2 = 1 - ((y + h) / view_h) * 2

        return x1, y2, x2 - x1, y1 - y2

    # ---------------------------
    # 绘制矩形
    # ---------------------------
    def _draw(self, tex, x, y, w, h):
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)

        GL.glBegin(GL.GL_QUADS)

        GL.glTexCoord2f(0, 1)
        GL.glVertex2f(x, y)

        GL.glTexCoord2f(1, 1)
        GL.glVertex2f(x + w, y)

        GL.glTexCoord2f(1, 0)
        GL.glVertex2f(x + w, y + h)

        GL.glTexCoord2f(0, 0)
        GL.glVertex2f(x, y + h)

        GL.glEnd()

    # ---------------------------
    # 布局计算
    # ---------------------------
    def _get_layout_rects(self, W, H):
        if self.layout_mode == "grid":
            return {
                "front": (0, 0, W / 2, H / 2),
                "back": (W / 2, 0, W / 2, H / 2),
                "left": (0, H / 2, W / 2, H / 2),
                "right": (W / 2, H / 2, W / 2, H / 2),
            }

        elif self.layout_mode == "horizontal":
            w = W / 4
            return {name: (i * w, 0, w, H) for i, name in enumerate(CAMERA_NAMES)}

        elif self.layout_mode == "vertical":
            h = H / 4
            return {name: (0, i * h, W, h) for i, name in enumerate(CAMERA_NAMES)}

        elif self.layout_mode == "triple":
            top_h = H * 0.3  # 上面占30%
            bottom_h = H * 0.7  # 下面占70%

            w = W / 3

            return {
                # 上排：左 后 右
                "left": (0, 0, w, top_h),
                "back": (w, 0, w, top_h),
                "right": (2 * w, 0, w, top_h),
                # 下排：前（主画面）
                "front": (0, top_h, W, bottom_h),
            }

    # ---------------------------
    # 主渲染
    # ---------------------------
    def paintGL(self):
        GL.glClearColor(0.08, 0.08, 0.1, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)

        W = self.width()
        H = self.height()

        if W <= 0 or H <= 0:
            return
        now = time.monotonic()
        frames = dict(zip(self.fb.positions, self.fb.all_frames))
        rects = self._get_layout_rects(W, H)

        for name in CAMERA_NAMES:
            snapshot = frames.get(name)
            if snapshot is None or not snapshot.fresh(self._stale_after_s, now=now):
                self.meta[name] = {"size": None, "fps": 0}
                self._fps_counter[name] = {"last": now, "count": 0, "fps": 0}
                self._frame_ids.pop(name, None)
                continue
            frame = snapshot.frame
            h, w, _ = frame.shape
            self.meta[name]["size"] = (w, h)

            if self._frame_ids.get(name) != snapshot.id:
                self._update_texture(name, frame)
                self._update_fps(name)
                self._frame_ids[name] = snapshot.id

            vx, vy, vw, vh = rects[name]
            x, y, rw, rh = self._calc_rect(vw, vh, w, h)

            gx, gy, gw, gh = self._to_gl(vx + x, vy + y, rw, rh, W, H)

            self._draw(self.textures[name], gx, gy, gw, gh)

        # ===== 叠加UI =====
        self._draw_overlay(rects)

    # ---------------------------
    # 叠加层（时间戳+信息）
    # ---------------------------
    def _draw_overlay(self, rects):
        p = QPainter(self)

        # ===== 分隔线 =====
        pen = QPen(self.border_color)
        pen.setWidth(1)
        p.setPen(pen)

        for x, y, w, h in rects.values():
            p.drawRect(int(x), int(y), int(w), int(h))

        p.end()
