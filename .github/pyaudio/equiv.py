"""The Extension that reaches the compiler: 0.2.14's setup.py against the patched one, for each case of
the win32 branch and for the other platforms, field by field. No compiler is run: the patched tree's
build_ext hook is called (twice, as "setup.py build_ext ... bdist_wheel" does) with the compiler type
set, and its parent's build_extensions() replaced by nothing.

usage: equiv.py DIR_AS_RELEASED DIR_PATCHED"""
import importlib.util, os, sys, types
import setuptools
import setuptools.command.build_ext as real      # before sys.platform is set to something this machine is not

FIELDS = ("name", "sources", "include_dirs", "define_macros", "libraries", "extra_compile_args", "extra_link_args", "library_dirs")


def load(path, platform, vcpkg, compiler_type):
    captured = {}
    real_setup, real_build = setuptools.setup, real.build_ext.build_extensions
    setuptools.setup = lambda **kw: captured.update(kw)
    real.build_ext.build_extensions = lambda self: None
    old_platform, old_env = sys.platform, os.environ.get("VCPKG_PATH")
    sys.platform = platform
    if vcpkg: os.environ["VCPKG_PATH"] = vcpkg
    else: os.environ.pop("VCPKG_PATH", None)
    cwd = os.getcwd(); os.chdir(os.path.dirname(path))
    try:
        spec = importlib.util.spec_from_file_location("pyaudio_setup", path)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        ext = captured["ext_modules"][0]
        cmd = captured.get("cmdclass", {}).get("build_ext")
        if cmd is not None:
            c = cmd(setuptools.Distribution({"ext_modules": [ext]}))
            c.compiler = types.SimpleNamespace(compiler_type=compiler_type); c.extensions = [ext]
            c.build_extensions(); c.build_extensions()
        return {f: getattr(ext, f) for f in FIELDS}
    finally:
        os.chdir(cwd); sys.platform = old_platform
        setuptools.setup, real.build_ext.build_extensions = real_setup, real_build
        if old_env is None: os.environ.pop("VCPKG_PATH", None)
        else: os.environ["VCPKG_PATH"] = old_env


a_dir, b_dir = (os.path.abspath(p) for p in sys.argv[1:3])
ok = True
for platform, vcpkg, ct in (("win32", r"C:\vcpkg\installed\x64-windows-static", "msvc"), ("win32", None, "msvc"),
                            ("linux", None, "unix"), ("darwin", None, "unix")):
    a = load(os.path.join(a_dir, "setup.py"), platform, vcpkg, ct)
    b = load(os.path.join(b_dir, "setup.py"), platform, vcpkg, ct)
    ok &= a == b
    print("%-7s VCPKG_PATH %-7s %-5s %s" % (platform, "set" if vcpkg else "not set", ct, "identical" if a == b else "DIFFERENT"))
    for f in FIELDS:
        if a[f] != b[f]: print("   ", f, a[f], "->", b[f])
shown = ("define_macros", "libraries", "extra_compile_args", "library_dirs", "include_dirs")
for ct in ("mingw32",):
    o = load(os.path.join(a_dir, "setup.py"), "win32", None, ct); m = load(os.path.join(b_dir, "setup.py"), "win32", None, ct)
    print("win32 with gcc or clang, as released:", {f: o[f] for f in shown})
    print("win32 with gcc or clang, patched:    ", {f: m[f] for f in shown})
sys.exit(0 if ok else 1)
