#!/bin/bash
# try.sh: PyAudio 0.2.14 as released (a) and with the patch (b), built with the Python and compiler on PATH.
# Prints what the compiler and linker were given, whether each build worked, and imports what was built.
here=$(cd "$(dirname "$0")" && pwd); work=${1:-$PWD/pyaudio-try}; mkdir -p "$work" && cd "$work"
python -c "import sys, sysconfig, setuptools; print('sys.platform:', sys.platform, '| sysconfig.get_platform():', sysconfig.get_platform(), '| setuptools', setuptools.__version__); print('sys.version:', sys.version.replace(chr(10), ' '))"
[ -f PyAudio-0.2.14.tar.gz ] || python -m pip download pyaudio==0.2.14 --no-binary :all: --no-deps --no-build-isolation -q -d .
rm -rf a b && tar xzf PyAudio-0.2.14.tar.gz && mv PyAudio-0.2.14 a && cp -r a b && (cd b && git apply -p1 "$here/pyaudio-0.2.14-compiler-not-platform.patch") || exit 2
rc=0
for v in a b; do
  echo "=== $v: $([ $v = a ] && echo 'as released' || echo 'patched')  ${EXTRA:+(build_ext $EXTRA)}"
  if (cd $v && python setup.py build_ext $EXTRA bdist_wheel -d ../out-$v > ../build-$v.log 2>&1); then
    echo "BUILD OK: $(ls out-$v)"
  else
    echo "BUILD FAILED:"; grep -i -m4 'error\|No such file\|undefined reference\|cannot find\|cannot open' build-$v.log | cut -c1-260
    [ $v = b ] && rc=1
  fi
  # the compiler's and the linker's command lines
  grep -E '(^|[\\/" ])(cl\.exe|link\.exe|gcc|clang|cc)(\.exe)?"? ' build-$v.log | grep -v '^copying\|^creating' > cmds-$v.txt
  sed -n '1p;$p' cmds-$v.txt | cut -c1-900
done
if [ -s cmds-a.txt ] && diff -q cmds-a.txt cmds-b.txt > /dev/null; then echo "SAME: compiler and linker command lines of a and b are identical ($(grep -c '' cmds-a.txt) lines)"; else echo "DIFFERENT command lines (a: $(grep -c '' cmds-a.txt), b: $(grep -c '' cmds-b.txt))"; fi
for v in a b; do
  ls out-$v/*.whl > /dev/null 2>&1 || continue
  rm -rf t-$v && python -m pip install -q --no-deps --target t-$v out-$v/*.whl
  ( cd t-$v && python -c "
import os, pyaudio
p = pyaudio.PyAudio()
ext = [f for f in os.listdir('pyaudio') if f.startswith('_portaudio')][0]
print('$v: imported PyAudio', pyaudio.__version__, '|', pyaudio.get_portaudio_version_text(), '| host APIs', [p.get_host_api_info_by_index(i)['name'] for i in range(p.get_host_api_count())], '|', ext, os.path.getsize(os.path.join('pyaudio', ext)), 'bytes')
p.terminate()" ) || { [ $v = b ] && rc=1; }
done
exit $rc
