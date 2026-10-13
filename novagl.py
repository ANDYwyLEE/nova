import traceback, logging, sys, ctypes, time
import numpy as np
from util import WINDOWS
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QSurfaceFormat, QOpenGLContext, QPainter, QColor

try: from OpenGL.GL import *
except ImportError:
    print("Error: PyOpenGL is required. Run 'pip install PyOpenGL PyOpenGL_accelerate'")
    sys.exit(1)

from view import Nova, OSD_LAYERS

GL_TEXTURE_MAX_ANISOTROPY_EXT = 0x84FE

# A paintGL that keeps failing (a lost context after a driver reset, a call the driver rejects) used to log a
# full traceback for every frame, about 30 a second, and leave the window black.
GL_REBUILD_AFTER = 30      # consecutive failed frames (about a second) before the GL objects are rebuilt, once
GL_GIVE_UP_AFTER = 60      # consecutive failed frames before OpenGL rendering is given up for this window
GL_LOG_INTERVAL = 60.0     # seconds between render error reports
RENDER_FAILED = "Rendering failed (OpenGL)\nsee the log"
GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT = 0x84FF

VERTEX_SHADER = """
#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec2 aTexCoord;
out vec2 TexCoord;
uniform mat4 model;
void main() {
    gl_Position = model * vec4(aPos, 1.0);
    TexCoord = aTexCoord;
}
"""

# mode 0: RGBA texture (software-converted video, OSD layers)
# mode 1: NV12   (tex0 = Y, tex1 = interleaved UV)
# mode 2: YUV420P (tex0 = Y, tex1 = U, tex2 = V)   - direct upload of software-decoded frames
FRAGMENT_SHADER = """
#version 330 core
out vec4 FragColor;
in vec2 TexCoord;
uniform sampler2D tex0, tex1, tex2;
uniform int mode;
uniform int fullRange;
uniform vec4 coef;   // (Rv, Gu, Gv, Bu) of the YUV->RGB matrix
void main() {
    if (mode == 0) {
        FragColor = texture(tex0, TexCoord);
        return;
    }
    float y = texture(tex0, TexCoord).r;
    float u, v;
    if (mode == 1) {
        vec2 uv = texture(tex1, TexCoord).rg;
        u = uv.r; v = uv.g;
    } else {
        u = texture(tex1, TexCoord).r;
        v = texture(tex2, TexCoord).r;
    }
    if (fullRange == 0) {
        y = (y - 16.0 / 255.0) * (255.0 / 219.0);
        u = (u - 128.0 / 255.0) * (255.0 / 224.0);
        v = (v - 128.0 / 255.0) * (255.0 / 224.0);
    } else {
        u -= 0.5; v -= 0.5;
    }
    vec3 rgb = vec3(y + coef.x * v, y - coef.y * u - coef.z * v, y + coef.w * u);
    FragColor = vec4(clamp(rgb, 0.0, 1.0), 1.0);
}
"""

COEF_BT601 = (1.4020, 0.3441, 0.7141, 1.7720)
COEF_BT709 = (1.5748, 0.1873, 0.4681, 1.8556)

def check_compile_status(shader, name):
    if not glGetShaderiv(shader, GL_COMPILE_STATUS):
        logging.error(f"[NovaGL] ❌ Shader Error [{name}]: {glGetShaderInfoLog(shader).decode()}")
        return False
    return True

def _enum_int(v, default):
    try:
        return int(getattr(v, "value", v))
    except Exception:
        return default


class NovaGL(Nova):
    def __init__(self, core, grid=20, border=False, geometry=None, zoom=None):
        self.shader_program = None
        self.VAO = self.VBO = self.EBO = self.VAO_static = self.VBO_static = None
        self.video_texture = None
        self.plane_tex = []
        self.osd_texture_ids = {}
        self.texture_cache = {}
        self.loc = {}
        self.max_anisotropy = 0.0
        self._gl_failures = 0          # consecutive frames that failed to render
        self._gl_unreported = 0        # failures since the last error report
        self._gl_reported_at = None    # time.monotonic() of the last error report
        self._gl_rebuilt_at = None
        self._gl_dead = False          # given up: only the OSD is drawn, with the error message
        super().__init__(core, grid, border, geometry=geometry, zoom=zoom)

    def _surface_format(self):
        # Applied by Nova.__init__ *before* show(): a 3.3 core profile is required for the
        # '#version 330 core' shaders (macOS gives a legacy 2.1 context otherwise).
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setRenderableType(QSurfaceFormat.OpenGL)
        fmt.setAlphaBufferSize(8)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        if WINDOWS: fmt.setSwapInterval(0)
        return fmt

    def initializeGL(self):
        if not self._init_gl():
            QTimer.singleShot(0, self.close)

    def _init_gl(self):
        """Create the shader program, buffers and textures in the current context. False on failure (logged)."""
        try:
            # On Wayland Qt first makes the context current without a surface; NVIDIA's EGL then
            # leaves the window's draw buffer at GL_NONE, so everything drawn was discarded and a
            # native Wayland window stayed empty. Mesa and GLX start at GL_BACK.
            if self.defaultFramebufferObject() == 0 and glGetIntegerv(GL_DRAW_BUFFER) == GL_NONE:
                glDrawBuffer(GL_BACK)
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glClearColor(0.0, 0.0, 0.0, 0.8)

            vs = glCreateShader(GL_VERTEX_SHADER)
            glShaderSource(vs, VERTEX_SHADER)
            glCompileShader(vs)
            ok = check_compile_status(vs, "VS")
            fs = glCreateShader(GL_FRAGMENT_SHADER)
            glShaderSource(fs, FRAGMENT_SHADER)
            glCompileShader(fs)
            ok = check_compile_status(fs, "FS") and ok
            if not ok:
                raise RuntimeError("shader compilation failed (OpenGL 3.3 core profile required)")
            prog = glCreateProgram()
            glAttachShader(prog, vs)
            glAttachShader(prog, fs)
            glLinkProgram(prog)
            glDeleteShader(vs)
            glDeleteShader(fs)
            if not glGetProgramiv(prog, GL_LINK_STATUS):
                raise RuntimeError(f"shader link failed: {glGetProgramInfoLog(prog)}")

            # uniform locations are looked up once, not per draw call
            for name in ("model", "tex0", "tex1", "tex2", "mode", "fullRange", "coef"):
                self.loc[name] = glGetUniformLocation(prog, name)
            glUseProgram(prog)
            glUniform1i(self.loc["tex0"], 0)
            glUniform1i(self.loc["tex1"], 1)
            glUniform1i(self.loc["tex2"], 2)
            glUseProgram(0)

            v_dyn = np.array([
                1, 1, 0, 1, 0,
                1,-1, 0, 1, 1,
               -1,-1, 0, 0, 1,
               -1, 1, 0, 0, 0
            ], dtype=np.float32)
            indices = np.array([0, 1, 3, 1, 2, 3], dtype=np.uint32)

            # Setup Main VAO
            self.VAO = glGenVertexArrays(1)
            self.VBO = glGenBuffers(1)
            self.EBO = glGenBuffers(1)
            glBindVertexArray(self.VAO)
            glBindBuffer(GL_ARRAY_BUFFER, self.VBO)
            glBufferData(GL_ARRAY_BUFFER, v_dyn.nbytes, v_dyn, GL_DYNAMIC_DRAW)
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.EBO)
            glBufferData(GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, GL_STATIC_DRAW)
            self._set_attribs()
            glBindVertexArray(0)

            # Setup Static VAO for OSD
            self.VAO_static = glGenVertexArrays(1)
            self.VBO_static = vbo_s = glGenBuffers(1)
            glBindVertexArray(self.VAO_static)
            glBindBuffer(GL_ARRAY_BUFFER, vbo_s)
            glBufferData(GL_ARRAY_BUFFER, v_dyn.nbytes, v_dyn, GL_STATIC_DRAW)
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.EBO)
            self._set_attribs()
            glBindVertexArray(0)

            self.video_texture = int(glGenTextures(1))
            self.plane_tex = [int(t) for t in glGenTextures(3)]
            self.osd_texture_ids = {k: int(glGenTextures(1)) for k in ['bg', 'txt', 'hlp', 'bt']}

            self.max_anisotropy = 0.0
            try:
                exts = [glGetStringi(GL_EXTENSIONS, i).decode() for i in range(glGetIntegerv(GL_NUM_EXTENSIONS))]
                if "GL_EXT_texture_filter_anisotropic" in exts:
                    self.max_anisotropy = float(glGetFloatv(GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT))
                    logging.debug(f"[NovaGL] Anisotropic Filtering Supported: {self.max_anisotropy}")
            except Exception:
                pass
            self.shader_program = prog
            glViewport(0, 0, self.width(), self.height())
            ver = glGetString(GL_VERSION)
            logging.debug(f"[NovaGL] initializeGL done. GL {ver.decode() if ver else '?'}")
            return True
        except Exception:
            logging.error(f"[NovaGL] initializeGL Error: {traceback.format_exc()}")
            return False

    def _set_attribs(self):
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 20, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 20, ctypes.c_void_p(12))
        glEnableVertexAttribArray(1)

    def paintGL(self):
        if not self.is_running: return
        if self._gl_dead:
            self._paint_failed()
            return
        if not self.shader_program: return
        try:
            self._paint_frame()
        except Exception as e:
            self._frame_failed(e)
            return
        if self._gl_failures:
            if self._gl_failures >= GL_REBUILD_AFTER:         # the counterpart of the rebuild warning
                logging.info(f"[NovaGL] rendering works again after {self._gl_failures} failed frames")
            self._gl_failures = 0

    def _paint_frame(self):
        glClear(GL_COLOR_BUFFER_BIT)
        glUseProgram(self.shader_program)
        df = self.curr_df
        if df is not None:
            glDisable(GL_BLEND)
            if df.kind == "yuv":
                mode = 1 if df.pix_fmt == "nv12" else 2
                self._upload_planes(df)
                self._draw_video(mode, df)
            else:
                self._upload_rgba(self.video_texture, df.qimg, downscaled=self.zoomer.scale() < 1)
                glActiveTexture(GL_TEXTURE0)
                glBindTexture(GL_TEXTURE_2D, self.video_texture)
                self._draw_video(0, df)
            glEnable(GL_BLEND)

        # OSD layers are premultiplied RGBA
        glUniform1i(self.loc["mode"], 0)
        glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ZERO, GL_ONE)
        glActiveTexture(GL_TEXTURE0)
        for k in OSD_LAYERS:
            qimg = getattr(self, f"osd_{k}")
            if not qimg.isNull():
                tid = self.osd_texture_ids[k]
                self._upload_osd(tid, qimg, nearest=(k == 'bg'))
                self._draw_quad_fill(tid)
        glBindVertexArray(0)
        glUseProgram(0)

    def _frame_failed(self, e):
        """Count a failed frame: report it at most every GL_LOG_INTERVAL, rebuild the GL objects once after
        GL_REBUILD_AFTER failures in a row, give up after GL_GIVE_UP_AFTER."""
        self._gl_failures += 1
        self._gl_unreported += 1
        now = time.monotonic()
        if self._gl_reported_at is None or now - self._gl_reported_at >= GL_LOG_INTERVAL:
            more = f" ({self._gl_unreported - 1} more failed frames since the last report)" if self._gl_unreported > 1 else ""
            logging.error(f"[NovaGL] Render Error{more}: {traceback.format_exc()}")
            self._gl_reported_at = now
            self._gl_unreported = 0
        try:                                     # leave a clean state for the next attempt
            glBindVertexArray(0)
            glUseProgram(0)
        except Exception:
            pass
        n = self._gl_failures
        if n == GL_REBUILD_AFTER and (self._gl_rebuilt_at is None or now - self._gl_rebuilt_at >= GL_LOG_INTERVAL):
            logging.warning(f"[NovaGL] {n} frames in a row failed to render; rebuilding the OpenGL state")
            self._gl_rebuilt_at = now
            if not self._rebuild_gl():
                self._give_up(e)
        elif n >= GL_GIVE_UP_AFTER:
            self._give_up(e)

    def _rebuild_gl(self):
        self._delete_gl_objects()
        self.shader_program = None
        self.texture_cache.clear()
        return self._init_gl()

    def _give_up(self, e):
        self._gl_dead = True
        ctx = self.context()
        lost = " (the OpenGL context is lost)" if ctx is not None and not ctx.isValid() else ""
        logging.error(f"[NovaGL] rendering keeps failing{lost}, {self._gl_failures} frames in a row, last error: "
                      f"{e!r}. OpenGL rendering stopped for this window; restart the camera (nova restart ID) "
                      f"or run it without --opengl.")
        self.show_render_error(RENDER_FAILED)

    def _paint_failed(self):
        """After giving up: no video, only the OSD (with the error message) through QPainter, if the context
        can still draw at all. Errors here are not reported again."""
        try:
            glBindVertexArray(0)
            glUseProgram(0)
        except Exception:
            pass
        try:
            p = QPainter(self)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(0, 0, self.width(), self.height(), QColor(0, 0, 0, 204))
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            self._draw_osd(p)
            p.end()
        except Exception:
            pass

    # ------------------------------------------------------------------ texture uploads
    def _upload_osd(self, tid, qimg, nearest=False):
        """Upload an OSD layer only when its QImage changed (cacheKey)."""
        ck = qimg.cacheKey()
        cached = self.texture_cache.get(tid)
        if cached is not None and cached[2] == ck:
            return
        if qimg.format() not in (QImage.Format_RGBA8888_Premultiplied, QImage.Format_RGBA8888):
            qimg = qimg.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
        w, h = qimg.width(), qimg.height()
        ptr = qimg.constBits()
        glBindTexture(GL_TEXTURE_2D, tid)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glPixelStorei(GL_UNPACK_ROW_LENGTH, qimg.bytesPerLine() // 4)
        if cached is None or cached[0] != w or cached[1] != h:
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, ptr)
            f = GL_NEAREST if nearest else GL_LINEAR
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, f)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, f)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        else:
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, ptr)
        glPixelStorei(GL_UNPACK_ROW_LENGTH, 0)
        self.texture_cache[tid] = (w, h, ck)

    def _upload_rgba(self, tid, qimg, downscaled):
        if qimg.format() != QImage.Format_RGBA8888:
            qimg = qimg.convertToFormat(QImage.Format_RGBA8888)
        w, h, ptr = qimg.width(), qimg.height(), qimg.constBits()
        glBindTexture(GL_TEXTURE_2D, tid)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glPixelStorei(GL_UNPACK_ROW_LENGTH, qimg.bytesPerLine() // 4)
        if self.texture_cache.get(tid) != (w, h):
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, ptr)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            self.texture_cache[tid] = (w, h)
        else:
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, ptr)
        glPixelStorei(GL_UNPACK_ROW_LENGTH, 0)
        self._video_filters(downscaled)

    def _video_filters(self, downscaled):
        if self.max_anisotropy > 0:
            glTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_MAX_ANISOTROPY_EXT, self.max_anisotropy)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR if downscaled else GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        if downscaled:
            glGenerateMipmap(GL_TEXTURE_2D)

    def _upload_planes(self, df):
        """Upload NV12 / YUV420P planes straight from the decoder buffers.
        GL_UNPACK_ROW_LENGTH handles the decoder's line padding, so no numpy slicing/copy."""
        f = df.frame
        w, h = f.width, f.height
        cw, ch = (w + 1) // 2, (h + 1) // 2
        planes = f.planes
        if f.format.name == "nv12":
            specs = [(self.plane_tex[0], planes[0], w, h, GL_RED, GL_R8, 1),
                     (self.plane_tex[1], planes[1], cw, ch, GL_RG, GL_RG8, 2)]
        else:  # yuv420p / yuvj420p
            specs = [(self.plane_tex[0], planes[0], w, h, GL_RED, GL_R8, 1),
                     (self.plane_tex[1], planes[1], cw, ch, GL_RED, GL_R8, 1),
                     (self.plane_tex[2], planes[2], cw, ch, GL_RED, GL_R8, 1)]
        downscaled = self.zoomer.scale() < 1
        for tid, plane, pw, ph, fmt, internal, bpp in specs:
            data = np.frombuffer(plane, dtype=np.uint8)
            row_len = max(pw, plane.line_size // bpp)
            if data.size < row_len * bpp * ph:          # defensive: never read past the plane buffer
                row_len = data.size // (bpp * ph)
            glBindTexture(GL_TEXTURE_2D, tid)
            glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
            glPixelStorei(GL_UNPACK_ROW_LENGTH, row_len)
            key = (internal, pw, ph)
            if self.texture_cache.get(tid) != key:
                glTexImage2D(GL_TEXTURE_2D, 0, internal, pw, ph, 0, fmt, GL_UNSIGNED_BYTE, None)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                self.texture_cache[tid] = key
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, pw, ph, fmt, GL_UNSIGNED_BYTE, data)
            glPixelStorei(GL_UNPACK_ROW_LENGTH, 0)
            self._video_filters(downscaled)

    # ------------------------------------------------------------------ drawing
    def _color_params(self, frame):
        cs = _enum_int(getattr(frame, "colorspace", None), 2)      # AVCOL_SPC_*: 1=BT709, 5/6=BT601
        cr = _enum_int(getattr(frame, "color_range", None), 0)     # AVCOL_RANGE_*: 1=MPEG, 2=JPEG
        full = frame.format.name == "yuvj420p" or cr == 2
        if cs == 1: bt709 = True
        elif cs in (4, 5, 6, 7): bt709 = False
        else: bt709 = frame.height > 576                            # unspecified: HD -> BT.709
        return (COEF_BT709 if bt709 else COEF_BT601), full

    def _get_fit_model(self):
        ww, wh = self.width(), self.height()
        tr = self._pic_ratio()
        if not tr: tr = 1.0
        vw, vh = (wh * tr, float(wh)) if (ww/wh > tr) else (float(ww), ww/tr)
        model = np.identity(4, dtype=np.float32)
        model[0][0], model[1][1] = vw/ww, vh/wh
        return model

    def _draw_quad_fill(self, tid):
        glBindVertexArray(self.VAO_static)
        glUniformMatrix4fv(self.loc["model"], 1, GL_TRUE, np.identity(4, dtype=np.float32))
        glBindTexture(GL_TEXTURE_2D, tid)
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)

    def _draw_video(self, mode, df):
        glBindVertexArray(self.VAO)
        self._update_vbo(df)
        glUniformMatrix4fv(self.loc["model"], 1, GL_TRUE, self._get_fit_model())
        glUniform1i(self.loc["mode"], mode)
        if mode:
            coef, full = self._color_params(df.frame)
            glUniform4f(self.loc["coef"], *coef)
            glUniform1i(self.loc["fullRange"], 1 if full else 0)
            for unit, tid in enumerate(self.plane_tex):
                glActiveTexture(GL_TEXTURE0 + unit)
                glBindTexture(GL_TEXTURE_2D, tid)
            glActiveTexture(GL_TEXTURE0)
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)

    def _update_vbo(self, df):
        u0, v0, u1, v1 = 0.0, 0.0, 1.0, 1.0
        if self.zoomer.is_zoom() and df is not None and df.width > 0 and df.height > 0:
            fw, fh = df.width, df.height
            zr = self.zoomer.toRect()
            u0, v0 = zr.x()/fw, zr.y()/fh
            u1, v1 = (zr.x()+zr.width())/fw, (zr.y()+zr.height())/fh
        data = np.array([
            1, 1, 0, u1, v0,
            1,-1, 0, u1, v1,
           -1,-1, 0, u0, v1,
           -1, 1, 0, u0, v0
        ], dtype=np.float32)
        glBindBuffer(GL_ARRAY_BUFFER, self.VBO)
        glBufferSubData(GL_ARRAY_BUFFER, 0, data.nbytes, data)

    def _delete_gl_objects(self):
        """Delete what _init_gl created, in the current context. Errors (a lost context) are ignored."""
        objs = [(glDeleteVertexArrays, v) for v in (self.VAO, self.VAO_static)]
        objs += [(glDeleteBuffers, b) for b in (self.VBO, self.EBO, self.VBO_static)]
        objs += [(glDeleteTextures, t) for t in [self.video_texture] + list(self.plane_tex) + list(self.osd_texture_ids.values())]
        for fn, obj in objs:
            if obj:
                try: fn(1, [obj])
                except Exception as e: logging.debug(f"[NovaGL] delete: {e}")
        if self.shader_program:
            try: glDeleteProgram(self.shader_program)
            except Exception as e: logging.debug(f"[NovaGL] delete: {e}")
        self.VAO = self.VBO = self.EBO = self.VAO_static = self.VBO_static = None
        self.video_texture = None
        self.plane_tex = []
        self.osd_texture_ids = {}

    def cleanup(self):
        if not self.is_running: return
        # only with a live context: a window that was never shown has none, and a GL call without one
        # crashes on macOS (destroying the context frees the objects anyway)
        ctx = self.context()
        if ctx is not None and ctx.isValid() and self.shader_program:
            try:
                self.makeCurrent()
                if QOpenGLContext.currentContext() is ctx:
                    self._delete_gl_objects()
                self.doneCurrent()
            except Exception as e:
                logging.debug(f"[NovaGL] cleanup: {e}")
        super().cleanup()
