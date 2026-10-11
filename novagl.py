import traceback, logging, sys, ctypes
import numpy as np
from util import WINDOWS
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QSurfaceFormat

try: from OpenGL.GL import *
except ImportError:
    print("Error: PyOpenGL is required. Run 'pip install PyOpenGL PyOpenGL_accelerate'")
    sys.exit(1)

from view import Nova
from zoomer import Zoomer

GL_TEXTURE_MAX_ANISOTROPY_EXT = 0x84FE
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

FRAGMENT_SHADER = """
#version 330 core
out vec4 FragColor;
in vec2 TexCoord;
uniform sampler2D y_texture, uv_texture;
uniform bool isOSD;
void main() {
    if (isOSD) {
        FragColor = texture(y_texture, TexCoord);
    } else {
        float y_raw = texture(y_texture, TexCoord).r;
        vec2 uv_raw = texture(uv_texture, TexCoord).rg;
        float y = (y_raw - (16.0 / 255.0)) * (255.0 / 219.0);
        float u = uv_raw.r - 0.5, v = uv_raw.g - 0.5;
        float r = y + 1.402 * v;
        float g = y - 0.3441 * u - 0.7141 * v;
        float b = y + 1.772 * u;
        FragColor = vec4(clamp(vec3(r, g, b), 0.0, 1.0), 1.0);
    }
}
"""

def check_compile_status(shader, name):
    if not glGetShaderiv(shader, GL_COMPILE_STATUS):
        logging.error(f"[NovaGL] ❌ Shader Error [{name}]: {glGetShaderInfoLog(shader).decode()}")

class NovaGL(Nova):
    def __init__(self, core, grid=20, border=False):
        super().__init__(core, grid, border)
        self.shader_program = self.VAO = self.VBO = self.EBO = self.VAO_static = None
        self.video_texture = self.tex_y = self.tex_uv = None
        self.osd_texture_ids = {}
        self.texture_cache = {}

        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setAlphaBufferSize(8)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        if WINDOWS: fmt.setSwapInterval(0)
        self.setFormat(fmt)

    def initializeGL(self):
        try:
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glClearColor(0.0, 0.0, 0.0, 0.8)

            vs = glCreateShader(GL_VERTEX_SHADER)
            glShaderSource(vs, VERTEX_SHADER)
            glCompileShader(vs)
            check_compile_status(vs, "VS")
            fs = glCreateShader(GL_FRAGMENT_SHADER)
            glShaderSource(fs, FRAGMENT_SHADER)
            glCompileShader(fs)
            check_compile_status(fs, "FS")
            self.shader_program = glCreateProgram()
            glAttachShader(self.shader_program, vs)
            glAttachShader(self.shader_program, fs)
            glLinkProgram(self.shader_program)
            glDeleteShader(vs)
            glDeleteShader(fs)

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
            vbo_s = glGenBuffers(1)
            glBindVertexArray(self.VAO_static)
            glBindBuffer(GL_ARRAY_BUFFER, vbo_s)
            glBufferData(GL_ARRAY_BUFFER, v_dyn.nbytes, v_dyn, GL_STATIC_DRAW)
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.EBO)
            self._set_attribs()
            glBindVertexArray(0)

            self.video_texture, self.tex_y, self.tex_uv = glGenTextures(3)
            self.osd_texture_ids = {k: glGenTextures(1) for k in ['bg', 'txt', 'hlp', 'bt']}

            self.max_anisotropy = 0.0
            if "GL_EXT_texture_filter_anisotropic" in [glGetStringi(GL_EXTENSIONS, i).decode() for i in range(glGetIntegerv(GL_NUM_EXTENSIONS))]:
                self.max_anisotropy = glGetFloatv(GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT)
                logging.debug(f"[NovaGL] Anisotropic Filtering Supported: {self.max_anisotropy}")
            glViewport(0, 0, self.width(), self.height())
            logging.debug("[NovaGL] initializeGL (Static/Dynamic split) done")
        except:
            logging.error(f"[NovaGl] initializeGL Error: {traceback.format_exc()}")
            QTimer.singleShot(0, self.close)

    def _set_attribs(self):
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 20, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 20, ctypes.c_void_p(12))
        glEnableVertexAttribArray(1)

    def paintGL(self):
        if not self.is_running or not self.shader_program: return
        glClear(GL_COLOR_BUFFER_BIT)
        glUseProgram(self.shader_program)
        try:
            is_osd_loc = glGetUniformLocation(self.shader_program, "isOSD")
            if self.curr_qimg:
                flag, img = self.curr_qimg
                if flag == "nv12":
                    y, uv, w, h = img
                    glUniform1i(is_osd_loc, 0)
                    glDisable(GL_BLEND)
                    self._upload_texture_nv12(y, uv)
                    self._draw_quad_fit_nv12()
                    glEnable(GL_BLEND)
                else:
                    glUniform1i(is_osd_loc, 1)
                    glDisable(GL_BLEND)
                    self._upload_texture(self.video_texture, img)
                    self._draw_quad_fit(self.video_texture)
                    glEnable(GL_BLEND)

            glUniform1i(is_osd_loc, 1)
            glBlendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ZERO, GL_ONE)
            for k in ['bg', 'hlp', 'bt', 'txt']:
                qimg = getattr(self, f"osd_{k}")
                if not qimg.isNull():
                    self._upload_texture(self.osd_texture_ids[k], qimg)
                    self._draw_quad_fill(self.osd_texture_ids[k])
            glBindVertexArray(0)
            glUseProgram(0)
        except:
            logging.error(f"[NovaGL] Render Error: {traceback.format_exc()}")

    def _upload_texture(self, tid, qimg):
        if qimg.format() != QImage.Format_RGBA8888:
            qimg = qimg.convertToFormat(QImage.Format_RGBA8888)
        w, h, ptr = qimg.width(), qimg.height(), qimg.constBits()
        glBindTexture(GL_TEXTURE_2D, tid)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        if self.max_anisotropy > 0:
            glTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_MAX_ANISOTROPY_EXT, self.max_anisotropy)

        is_bg = (tid == self.osd_texture_ids.get('bg'))
        mag_f = GL_NEAREST if is_bg else GL_LINEAR
        min_f = mag_f if is_bg or self.zoomer.scale() >= 1 else GL_LINEAR_MIPMAP_LINEAR
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, min_f)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, mag_f)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)

        if self.texture_cache.get(tid) == (w, h):
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, ptr)
        else:
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, ptr)
        self.texture_cache[tid] = (w, h)
        if min_f == GL_LINEAR_MIPMAP_LINEAR:
            glGenerateMipmap(GL_TEXTURE_2D)

    def _get_fit_model(self):
        ww, wh = self.width(), self.height()
        tr = self.zoomer.ratio() if self.zoomer.is_zoom() else self.core.ratio()
        if not tr: tr = 1.0
        vw, vh = (wh * tr, float(wh)) if (ww/wh > tr) else (float(ww), ww/tr)
        model = np.identity(4, dtype=np.float32)
        model[0][0], model[1][1] = vw/ww, vh/wh
        return model

    def _draw_quad_fill(self, tid):
        glBindVertexArray(self.VAO_static)
        glUniformMatrix4fv(glGetUniformLocation(self.shader_program, "model"), 1, GL_TRUE, np.identity(4, dtype=np.float32))
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, tid)
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)

    def _draw_quad_fit(self, tid):
        glBindVertexArray(self.VAO)
        self._update_vbo()
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, tid)
        glUniformMatrix4fv(glGetUniformLocation(self.shader_program, "model"), 1, GL_TRUE, self._get_fit_model())
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)

    def _draw_quad_fit_nv12(self):
        glBindVertexArray(self.VAO)
        self._update_vbo()
        glUniformMatrix4fv(glGetUniformLocation(self.shader_program, "model"), 1, GL_TRUE, self._get_fit_model())
        glUniform1i(glGetUniformLocation(self.shader_program, "y_texture"), 0)
        glUniform1i(glGetUniformLocation(self.shader_program, "uv_texture"), 1)
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, self.tex_y)
        glActiveTexture(GL_TEXTURE1)
        glBindTexture(GL_TEXTURE_2D, self.tex_uv)
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)

    def _update_vbo(self):
        u0, v0, u1, v1 = 0.0, 0.0, 1.0, 1.0
        if self.zoomer.is_zoom() and self.curr_qimg:
            f, img = self.curr_qimg
            fw, fh = (img[2], img[3]) if f == "nv12" else (img.width(), img.height())
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

    def _upload_texture_nv12(self, y, uv):
        h, w = y.shape
        is_ds = self.zoomer.scale() < 1
        min_f = GL_LINEAR_MIPMAP_LINEAR if is_ds else GL_LINEAR
        for i, (tid, data, fmt, internal, k) in enumerate([(self.tex_y, y, GL_RED, GL_RED, 'y'), (self.tex_uv, uv, GL_RG, GL_RG8, 'uv')]):
            glBindTexture(GL_TEXTURE_2D, tid)
            glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
            if self.texture_cache.get(f'{k}_sz') != data.shape[::-1]:
                glTexImage2D(GL_TEXTURE_2D, 0, internal, data.shape[1], data.shape[0], 0, fmt, GL_UNSIGNED_BYTE, None)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                self.texture_cache[f'{k}_sz'] = data.shape[::-1]
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, data.shape[1], data.shape[0], fmt, GL_UNSIGNED_BYTE, data)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, min_f)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            if is_ds: glGenerateMipmap(GL_TEXTURE_2D)

    def cleanup(self):
        if not self.is_running: return
        self.makeCurrent()
        for v in [self.VAO, self.VAO_static]:
            if v: glDeleteVertexArrays(1, [v])
        for b in [self.VBO, self.EBO]:
            if b: glDeleteBuffers(1, [b])
        for t in [self.video_texture, self.tex_y, self.tex_uv]:
            if t: glDeleteTextures(1, [t])
        for t in list(self.osd_texture_ids.values()):
            if t: glDeleteTextures(1, [t])
        if self.shader_program: glDeleteProgram(self.shader_program)
        self.doneCurrent()
        super().cleanup()
