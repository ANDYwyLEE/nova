"""The OpenGL renderer of the camera window: NV12 / YUV planes uploaded as textures and converted to
RGB in a shader, the overlay as an RGBA texture, fit and zoom as a model matrix."""
import traceback, logging, sys, ctypes, time, re
import numpy as np
from util import WINDOWS, opengl_shortfall, opaque_windows
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QSurfaceFormat, QOpenGLContext, QOffscreenSurface, QPainter, QColor

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


class PictureTooLarge(RuntimeError):
    """The camera's picture is larger than a texture of this GPU may be (2048 pixels on a Raspberry Pi 3)."""
GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT = 0x84FF

# asked for in this order: what the shaders are written for, what a Raspberry Pi 4 and 5 give, and what
# every GPU with shaders gives (a Raspberry Pi 3, Intel graphics of 2010)
CONTEXTS = ((3, 3), (3, 1), (2, 1))
OPAQUE = [False]                    # opengl_offered met a context for which windows are made without alpha


def context_format(version, base=None):
    """`base` (a new format when None) as the request for an OpenGL context of `version`: the core
    profile from 3.2 on, where profiles begin."""
    fmt = QSurfaceFormat(base) if base is not None else QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.OpenGL)
    fmt.setVersion(*version)
    fmt.setProfile(QSurfaceFormat.CoreProfile if version >= (3, 2) else QSurfaceFormat.NoProfile)
    return fmt


def opengl_offered():
    """What the GPU's driver offers when that is not a context an --opengl window can draw in
    ('OpenGL 2.0', 'OpenGL ES 3.1'), or None when it can: OpenGL 3.3 core is asked for, and what a
    driver gives in its place is enough from 2.1 on (util.OPENGL_NEEDED).  Where 3.3 is refused
    outright, 3.1 and then 2.1 are asked for (CONTEXTS), and the application's default format is set
    to the one that was given, for the window.
    Asked before the window is made (a QGuiApplication exists): the shaders do not compile in an older
    context, and with some drivers the first call in it ended the process.  None as well when no
    context can be made at all: that is another matter, and the window says it."""
    try:
        for version in CONTEXTS:
            ctx = QOpenGLContext()
            ctx.setFormat(context_format(version))
            if ctx.create():
                break
        else:
            ctx = QOpenGLContext()                  # whatever the platform offers
            ctx.setFormat(QSurfaceFormat())
            if not ctx.create():
                return None
            version = None
        if version is not None and version != CONTEXTS[0]:
            # GLX gives its newest context when 3.3 is asked of a driver that stops at 3.1; EGL (a native
            # Wayland window) gives none at all.  The window is then asked for with what was given here.
            logging.info(f"[NovaGL] the GPU's driver gives no OpenGL {CONTEXTS[0][0]}.{CONTEXTS[0][1]} context "
                         f"here: the window is asked for with OpenGL {version[0]}.{version[1]}")
            QSurfaceFormat.setDefaultFormat(context_format(version, QSurfaceFormat.defaultFormat()))
        # Qt reads the version the driver gave when the context is made; current on a surface as well,
        # for a platform plugin that would only know then
        surface = QOffscreenSurface()
        surface.setFormat(ctx.format())
        surface.create()
        current = ctx.makeCurrent(surface)
        try:
            got = ctx.format()
            OPAQUE[0] = opaque_windows((got.majorVersion(), got.minorVersion()))
            if OPAQUE[0]:
                opaque = QSurfaceFormat.defaultFormat()
                opaque.setAlphaBufferSize(0)
                QSurfaceFormat.setDefaultFormat(opaque)
            return opengl_shortfall(got.majorVersion(), got.minorVersion(), ctx.isOpenGLES())
        finally:
            if current:
                ctx.doneCurrent()
    except Exception as e:
        logging.debug(f"[NovaGL] the OpenGL version could not be asked for: {e!r}")
        return None


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

ATTRIBUTES = ("aPos", "aTexCoord")       # the vertex shader's inputs, at locations 0 and 1 (_set_attribs)


def glsl_120(source):
    """A shader of above in GLSL 1.20, the language of an OpenGL 2.1 context (the GPU of a Raspberry Pi 3,
    Intel graphics of 2010): `attribute` and `varying` for `in` and `out`, texture2D(), gl_FragColor, and
    the two bytes of an NV12 chroma texel in luminance and alpha (such a context has no two-channel
    texture format: Renderer._upload_planes).  ATTRIBUTES are bound by name, as for GLSL 1.40."""
    vertex = "gl_Position" in source
    s = glsl_140(source).replace("#version 140", "#version 120")
    s = re.sub(r"^out vec4 FragColor;\n", "", s, flags=re.M)
    s = re.sub(r"^in ", "attribute " if vertex else "varying ", s, flags=re.M)
    s = re.sub(r"^out ", "varying ", s, flags=re.M)
    s = s.replace("FragColor", "gl_FragColor").replace("texture(", "texture2D(")
    return s.replace("texture2D(tex1, TexCoord).rg", "texture2D(tex1, TexCoord).ra")


def glsl_130(source):
    """A shader of above in GLSL 1.30, the language of an OpenGL 3.0 context: as in GLSL 1.40."""
    return glsl_140(source).replace("#version 140", "#version 130")


def gl_version():
    """(major, minor) of the current context.  GL_MAJOR_VERSION is OpenGL 3.0's: an older context answers
    it with an error, and says its version in a string that begins with it."""
    try:
        return int(glGetIntegerv(GL_MAJOR_VERSION)), int(glGetIntegerv(GL_MINOR_VERSION))
    except Exception:
        m = re.match(rb"\s*(\d+)\.(\d+)", glGetString(GL_VERSION) or b"")
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def glsl_140(source):
    """A shader of above in GLSL 1.40, the language of an OpenGL 3.1 or 3.2 context (the GPU of a
    Raspberry Pi 4 and 5): the same text without `layout (location = N)`, which GLSL has from 3.30
    on.  The program binds ATTRIBUTES to their locations by name before it is linked instead."""
    return re.sub(r"layout\s*\(\s*location\s*=\s*\d+\s*\)\s*", "", source.replace("#version 330 core", "#version 140"))


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
    def __init__(self, core, grid=20, border=False, **place):       # place: geometry, zoom, tile (view.Nova)
        self.shader_program = None
        self.VAO = self.VBO = self.EBO = self.VAO_static = self.VBO_static = None
        self.video_texture = None
        self.plane_tex = []
        self.osd_texture_ids = {}
        self.texture_cache = {}
        self.loc = {}
        self.max_anisotropy = 0.0
        # what the context turned out to be (_init_gl): OpenGL 2.x, with vertex array objects, with
        # glGenerateMipmap, and how large a texture may be
        self.legacy, self.vaos, self.mipmaps, self.max_texture = False, True, True, 16384
        self._gl_failures = 0          # consecutive frames that failed to render
        self._gl_unreported = 0        # failures since the last error report
        self._gl_reported_at = None    # time.monotonic() of the last error report
        self._gl_rebuilt_at = None
        self._gl_dead = False          # given up: only the OSD is drawn, with the error message
        super().__init__(core, grid, border, **place)

    def _surface_format(self):
        # Applied by Nova.__init__ *before* show(): a 3.3 core profile for the '#version 330 core'
        # shaders (macOS gives a legacy 2.1 context otherwise).  A driver that stops at 3.1 or 3.2
        # gives that, and _init_gl takes the shaders in GLSL 1.40; where it had to be asked for by
        # its number (opengl_offered), the default format holds that number.
        asked = QSurfaceFormat.defaultFormat().version()
        fmt = context_format(asked if asked in CONTEXTS else CONTEXTS[0])
        fmt.setAlphaBufferSize(0 if OPAQUE[0] or opaque_windows() else 8)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        if WINDOWS: fmt.setSwapInterval(0)
        return fmt

    def initializeGL(self):
        if not self._init_gl():
            # fatal: nova.py exits non-zero, so `nova start` reports a failure, not "exited with code 0"
            self.gl_init_failed = True
            # CRITICAL: `nova start` runs cameras at that level, and the cause belongs in logs/ID.log
            logging.critical(f"[NovaGL] OpenGL could not be initialised, the window closes: "
                             f"{getattr(self, '_gl_init_error', '?')}")
            QTimer.singleShot(0, self.close)

    def _init_gl(self):
        """Create the shader program, buffers and textures in the current context. False on failure (logged)."""
        try:
            if QOpenGLContext.currentContext() is None:
                raise RuntimeError("the window got no OpenGL context (the GPU's driver refused the one asked for)")
            # On Wayland Qt first makes the context current without a surface; NVIDIA's EGL then
            # leaves the window's draw buffer at GL_NONE, so everything drawn was discarded and a
            # native Wayland window stayed empty. Mesa and GLX start at GL_BACK.
            if self.defaultFramebufferObject() == 0 and glGetIntegerv(GL_DRAW_BUFFER) == GL_NONE:
                glDrawBuffer(GL_BACK)
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glClearColor(0.0, 0.0, 0.0, 0.8)

            # the context the driver gave: 3.3 was asked for, a Raspberry Pi 4's GPU gives 3.1, a Pi 3's 2.1
            version = gl_version()
            older = version < (3, 3)
            self.legacy = version < (3, 0)          # OpenGL 2.x: GLSL 1.20, luminance textures
            shader = (lambda s: s) if not older else glsl_120 if self.legacy else \
                glsl_130 if version < (3, 1) else glsl_140
            try:
                exts = set((glGetString(GL_EXTENSIONS) or b"").decode().split()) if self.legacy else \
                    {glGetStringi(GL_EXTENSIONS, i).decode() for i in range(glGetIntegerv(GL_NUM_EXTENSIONS))}
            except Exception:
                exts = set()
            # vertex array objects are OpenGL 3.0's, and an extension before it
            self.vaos = not self.legacy or "GL_ARB_vertex_array_object" in exts
            # No mipmaps in OpenGL 2.x.  They are made anew for every picture that is shown smaller than
            # it is, and the GPUs that stop at 2.1 are not the ones for that: on a Raspberry Pi 3 B+ (VC4)
            # a 1080p camera at 10 fps kept the window's thread at 97 % of a core with them and at 13 %
            # without.  A picture shown much smaller than it is is then less smooth.
            self.mipmaps = not self.legacy
            self.max_texture = int(glGetIntegerv(GL_MAX_TEXTURE_SIZE))
            vs = glCreateShader(GL_VERTEX_SHADER)
            glShaderSource(vs, shader(VERTEX_SHADER))
            glCompileShader(vs)
            ok = check_compile_status(vs, "VS")
            fs = glCreateShader(GL_FRAGMENT_SHADER)
            glShaderSource(fs, shader(FRAGMENT_SHADER))
            glCompileShader(fs)
            ok = check_compile_status(fs, "FS") and ok
            if not ok:
                raise RuntimeError("shader compilation failed (OpenGL 2.1 or newer is required)")
            prog = glCreateProgram()
            glAttachShader(prog, vs)
            glAttachShader(prog, fs)
            if older:
                for location, name in enumerate(ATTRIBUTES):
                    glBindAttribLocation(prog, location, name.encode())
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

            # Setup Main VAO (without vertex array objects, _bind_quad sets what one would hold at each draw)
            self.VAO = glGenVertexArrays(1) if self.vaos else None
            self.VBO = glGenBuffers(1)
            self.EBO = glGenBuffers(1)
            if self.vaos: glBindVertexArray(self.VAO)
            glBindBuffer(GL_ARRAY_BUFFER, self.VBO)
            glBufferData(GL_ARRAY_BUFFER, v_dyn.nbytes, v_dyn, GL_DYNAMIC_DRAW)
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.EBO)
            glBufferData(GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, GL_STATIC_DRAW)
            self._set_attribs()
            if self.vaos: glBindVertexArray(0)

            # Setup Static VAO for OSD
            self.VAO_static = glGenVertexArrays(1) if self.vaos else None
            self.VBO_static = vbo_s = glGenBuffers(1)
            if self.vaos: glBindVertexArray(self.VAO_static)
            glBindBuffer(GL_ARRAY_BUFFER, vbo_s)
            glBufferData(GL_ARRAY_BUFFER, v_dyn.nbytes, v_dyn, GL_STATIC_DRAW)
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.EBO)
            self._set_attribs()
            if self.vaos: glBindVertexArray(0)

            self.video_texture = int(glGenTextures(1))
            self.plane_tex = [int(t) for t in glGenTextures(3)]
            self.osd_texture_ids = {k: int(glGenTextures(1)) for k in ['bg', 'txt', 'hlp', 'bt']}

            self.max_anisotropy = 0.0
            try:
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
            self._gl_init_error = traceback.format_exc(limit=0).strip()
            return False

    def _set_attribs(self):
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 20, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 20, ctypes.c_void_p(12))
        glEnableVertexAttribArray(1)

    def _bind_quad(self, vao, vbo):
        """The quad of a vertex array object, or, in a context without them, the state it would hold."""
        if self.vaos:
            glBindVertexArray(vao)
        else:
            glBindBuffer(GL_ARRAY_BUFFER, vbo)
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.EBO)
            self._set_attribs()

    def _unbind_quad(self):
        if self.vaos:
            glBindVertexArray(0)

    def paintGL(self):
        if not self.is_running: return
        if self._gl_dead:
            self._paint_failed()
            return
        if not self.shader_program: return
        try:
            self._paint_frame()
        except PictureTooLarge as e:
            self._too_large(e)
            return
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
        self._unbind_quad()
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
            self._unbind_quad()
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

    def _too_large(self, e):
        """Not a failure that a second try or new GL objects change: said once, in the log and, since a
        camera started with -l CRITICAL writes no errors, in the window."""
        self._gl_dead = True
        logging.error(f"[NovaGL] {e}: this camera cannot be drawn with --opengl here. Start it without --opengl "
                      f"(the CPU then converts the pictures), or with the camera's second, smaller stream.")
        self.show_render_error(f"{str(e)[0].upper()}{str(e)[1:]}\nstart this camera without --opengl")

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
            self._unbind_quad()
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
        if max(w, h) > self.max_texture:
            raise PictureTooLarge(f"the picture is {w}x{h}, and this GPU's textures end at {self.max_texture} pixels")
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
        downscaled = downscaled and self.mipmaps
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
        if max(w, h) > self.max_texture:
            raise PictureTooLarge(f"the picture is {w}x{h}, and this GPU's textures end at {self.max_texture} pixels")
        # one byte and two bytes a texel: red and red-green from OpenGL 3.0 on, luminance and
        # luminance-alpha before it (the fragment shader in GLSL 1.20 reads .r and .ra)
        one, two = ((GL_LUMINANCE, GL_LUMINANCE), (GL_LUMINANCE_ALPHA, GL_LUMINANCE_ALPHA)) if self.legacy else \
            ((GL_RED, GL_R8), (GL_RG, GL_RG8))
        if f.format.name == "nv12":
            specs = [(self.plane_tex[0], planes[0], w, h, *one, 1),
                     (self.plane_tex[1], planes[1], cw, ch, *two, 2)]
        else:  # yuv420p / yuvj420p
            specs = [(self.plane_tex[0], planes[0], w, h, *one, 1),
                     (self.plane_tex[1], planes[1], cw, ch, *one, 1),
                     (self.plane_tex[2], planes[2], cw, ch, *one, 1)]
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
        self._bind_quad(self.VAO_static, self.VBO_static)
        glUniformMatrix4fv(self.loc["model"], 1, GL_TRUE, np.identity(4, dtype=np.float32))
        glBindTexture(GL_TEXTURE_2D, tid)
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)

    def _draw_video(self, mode, df):
        self._bind_quad(self.VAO, self.VBO)
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
            zr = self.zoomer.view_rect()
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
