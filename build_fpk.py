# -*- coding: utf-8 -*-
"""视频助手 fpk 确定性打包脚本。

修复 1.6.9 打包脚本的三个回归：
  1. 内层用 os.walk 整目录打包，把 server/__pycache__/*.pyc 带进了安装包；
  2. 内层文件权限按 Windows 的 os.access(X_OK) 判断，导致所有文件变成 0755；
  3. gzip 头带 FNAME 且 OS 字段为 0xff，与历史可安装包不一致、也不可复现。

本脚本改为：固定白名单条目 + 手工 TarInfo + 手写 gzip 头 + 构建后逐项自检。
外层 ICON.PNG / ICON_256.PNG 不在仓库里重复存放，打包时从内层 ui/images 取同尺寸图标。
用法：
    python build_fpk.py                       # 使用默认源码目录与输出名
    python build_fpk.py --src DIR --out FILE # 自定义
    python build_fpk.py --src fpk --inner-src fpk/app  # GitHub 仓库结构
"""
import argparse
import hashlib
import io
import os
import re
import struct
import sys
import tarfile
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(BASE, "fpk")

# 外层条目顺序与 1.6.5/1.6.7/1.6.8 完全一致（18 项）
OUTER_SEQUENCE = [
    ("manifest", "file", 0o644),
    ("ICON.PNG", "file", 0o644),
    ("ICON_256.PNG", "file", 0o644),
    ("cmd", "dir", 0o755),
    ("cmd/acl_apply", "file", 0o755),
    ("cmd/config_callback", "file", 0o755),
    ("cmd/config_init", "file", 0o755),
    ("cmd/install_callback", "file", 0o755),
    ("cmd/install_init", "file", 0o755),
    ("cmd/main", "file", 0o755),
    ("cmd/uninstall_callback", "file", 0o755),
    ("cmd/uninstall_init", "file", 0o755),
    ("cmd/upgrade_callback", "file", 0o755),
    ("cmd/upgrade_init", "file", 0o755),
    ("config", "dir", 0o755),
    ("config/privilege", "file", 0o644),
    ("config/resource", "file", 0o644),
]
# 外层图标不在仓库里重复存一份，打包时从内层 ui/images 取同尺寸图标
OUTER_FALLBACK = {
    "ICON.PNG": "ui/images/icon_64.png",
    "ICON_256.PNG": "ui/images/icon_256.png",
}
# 内层 app.tgz 条目顺序（15 项），绝不递归扫描目录
INNER_SEQUENCE = [
    ("server", "dir", 0o755),
    ("server/app.py", "file", 0o644),
    ("server/requirements.txt", "file", 0o644),
    ("ui", "dir", 0o755),
    ("ui/app.js", "file", 0o644),
    ("ui/config", "file", 0o644),
    ("ui/index.html", "file", 0o644),
    ("ui/manifest.webmanifest", "file", 0o644),
    ("ui/style.css", "file", 0o644),
    ("ui/images", "dir", 0o755),
    ("ui/images/icon_180.png", "file", 0o644),
    ("ui/images/icon_192.png", "file", 0o644),
    ("ui/images/icon_256.png", "file", 0o644),
    ("ui/images/icon_512.png", "file", 0o644),
    ("ui/images/icon_64.png", "file", 0o644),
]

FORBIDDEN = ("__pycache__", ".pyc", ".pyo", ".md", ".bak", ".DS_Store",
             "Thumbs.db", "desktop.ini")
GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\x0a"  # FLG=0, mtime=0, XFL=2, OS=Unix


def gzip_bytes(data):
    """手写 gzip 容器：无 FNAME、mtime=0、OS=Unix，保证可复现构建。"""
    comp = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = comp.compress(data) + comp.flush()
    trailer = struct.pack("<II", zlib.crc32(data) & 0xffffffff, len(data) & 0xffffffff)
    return GZIP_HEADER + body + trailer


def read_source(src, rel, normalize_lf=False):
    path = os.path.join(src, rel.replace("/", os.sep))
    if not os.path.isfile(path):
        raise RuntimeError("缺少源文件：%s" % rel)
    with open(path, "rb") as fp:
        data = fp.read()
    if normalize_lf and b"\r\n" in data:
        # Windows 编辑生命周期脚本极易引入 CRLF，打包时统一成 LF。
        data = data.replace(b"\r\n", b"\n")
    return data


def build_tar(sequence, src, include_app_tgz=None, fallback_src=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, kind, mode in sequence:
            info = tarfile.TarInfo(name)
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = 0
            info.mode = mode
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.size = 0
                tar.addfile(info)
                continue
            info.type = tarfile.REGTYPE
            if fallback_src and name in OUTER_FALLBACK and not os.path.isfile(
                    os.path.join(src, name)):
                data = read_source(fallback_src, OUTER_FALLBACK[name])
            else:
                data = read_source(src, name, normalize_lf=name.startswith("cmd/"))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if include_app_tgz is not None:
            info = tarfile.TarInfo("app.tgz")
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            info.size = len(include_app_tgz)
            tar.addfile(info, io.BytesIO(include_app_tgz))
    return buf.getvalue()


def preflight(src, inner_src=None):
    """打包前检查：白名单齐全、无杂质、LF、编码、版本号三处一致。"""
    inner_src = inner_src or src
    problems = []
    for base, sequence in ((src, OUTER_SEQUENCE), (inner_src, INNER_SEQUENCE)):
        for name, kind, _ in sequence:
            path = os.path.join(base, name.replace("/", os.sep))
            if kind == "dir":
                if not os.path.isdir(path):
                    problems.append("缺少目录：%s" % name)
            elif not os.path.isfile(path):
                fb = OUTER_FALLBACK.get(name) if base == src else None
                if fb and os.path.isfile(os.path.join(inner_src, fb.replace("/", os.sep))):
                    continue
                problems.append("缺少文件：%s" % name)
    # 源码目录里是否混进杂质（即使不在白名单里也要提醒）
    for base in (src, inner_src):
        for root, dirs, files in os.walk(base):
            for item in dirs + files:
                if any(token in item.lower() for token in FORBIDDEN):
                    problems.append("源码目录存在杂质：%s" % os.path.join(root, item))
    manifest = read_source(src, "manifest")
    if manifest.startswith(b"\xef\xbb\xbf"):
        problems.append("manifest 含 UTF-8 BOM")
    if b"\r" in manifest:
        problems.append("manifest 含 CRLF")
    try:
        text = manifest.decode("utf-8")
    except UnicodeDecodeError:
        problems.append("manifest 不是合法 UTF-8")
        text = ""
    version = ""
    m = re.search(r"^version\s*=\s*(\S+)\s*$", text, re.M)
    if m:
        version = m.group(1)
    else:
        problems.append("manifest 缺少 version 字段")
    app_py = read_source(inner_src, "server/app.py").decode("utf-8")
    html = read_source(inner_src, "ui/index.html").decode("utf-8")
    py_versions = re.findall(r'APP_VERSION\s*=\s*"([^"]+)"', app_py)
    html_versions = sorted(set(re.findall(r"\?v=([\d.]+)", html)))
    if not py_versions or py_versions[0] != version:
        problems.append("版本号不一致：manifest=%s app.py=%s" % (version, py_versions))
    if html_versions != [version]:
        problems.append("版本号不一致：index.html 资源版本号=%s" % html_versions)
    for name, _, _ in OUTER_SEQUENCE:
        if not name.startswith("cmd/"):
            continue
        data = read_source(src, name)
        if b"\r\n" in data:
            # 打包时会自动归一化，仅提示
            print("  [preflight] %s 含 CRLF，打包时自动转 LF" % name)
    if problems:
        for p in problems:
            print("  [preflight][ERROR]", p)
        raise RuntimeError("打包前检查未通过，共 %d 项" % len(problems))
    return version


def verify(pkg_path, version):
    """构建后读回真实文件逐项自检，不信任内存中的构建结果。"""
    raw = open(pkg_path, "rb").read()
    assert raw[:4] == b"\x1f\x8b\x08\x00", "外层不是 gzip+tar（FLG 必须为 0）"
    assert raw[9] == 0x0a, "gzip OS 字段必须为 Unix(0x0a)"
    with tarfile.open(pkg_path, "r:gz") as tar:
        outer = tar.getmembers()
        expected_outer = [n for n, _, _ in OUTER_SEQUENCE] + ["app.tgz"]
        got_outer = [m.name for m in outer]
        assert got_outer == expected_outer, "外层条目不符：%s" % got_outer
        for m in outer:
            assert m.uid == 0 and m.gid == 0 and m.uname == "root" and m.gname == "root", \
                "所有者不符：%s" % m.name
            assert m.mtime == 0, "mtime 不为 0：%s" % m.name
            assert m.type in (tarfile.REGTYPE, tarfile.DIRTYPE), "存在扩展头：%s" % m.name
            assert not any(t in m.name.lower() for t in FORBIDDEN), "混入杂质：%s" % m.name
            expected_mode = dict((n, md) for n, _, md in OUTER_SEQUENCE).get(m.name, 0o644)
            assert m.mode == expected_mode, "权限不符 %s: %s" % (m.name, oct(m.mode))
        manifest = tar.extractfile("manifest").read().decode("utf-8")
        assert ("version" in manifest and version in manifest), "manifest 版本不符"
        assert not manifest.startswith("\ufeff") and "\r" not in manifest
        app_gz = tar.extractfile("app.tgz").read()
    assert app_gz[:4] == b"\x1f\x8b\x08\x00" and app_gz[9] == 0x0a, "内层 gzip 头不符"
    with tarfile.open(fileobj=io.BytesIO(app_gz), mode="r:gz") as inner:
        members = inner.getmembers()
        expected_inner = [n for n, _, _ in INNER_SEQUENCE]
        got_inner = [m.name for m in members]
        assert got_inner == expected_inner, "内层条目不符：%s" % got_inner
        modes = dict((n, md) for n, _, md in INNER_SEQUENCE)
        for m in members:
            assert m.uid == 0 and m.gid == 0 and m.uname == "root", "内层所有者不符：%s" % m.name
            assert m.mtime == 0, "内层 mtime 不为 0：%s" % m.name
            assert m.mode == modes[m.name], "内层权限不符 %s: %s" % (m.name, oct(m.mode))
            assert not any(t in m.name.lower() for t in FORBIDDEN), "内层混入杂质：%s" % m.name
        py = inner.extractfile("server/app.py").read().decode("utf-8")
        assert ('APP_VERSION = "%s"' % version) in py, "包内 app.py 版本不符"
        html = inner.extractfile("ui/index.html").read().decode("utf-8")
        assert ("?v=" + version) in html, "包内 index.html 资源版本不符"
    return len(raw), hashlib.sha256(raw).hexdigest().upper(), len(outer), len(members)


def main():
    parser = argparse.ArgumentParser(description="视频助手 fpk 确定性打包")
    parser.add_argument("--src", default=DEFAULT_SRC, help="源码目录（含 manifest/cmd/config/server/ui）")
    parser.add_argument("--out", default=None, help="输出 fpk 路径")
    parser.add_argument("--inner-src", default=None,
                        help="内层 app.tgz 的源码根（仓库结构下为 fpk/app）")
    args = parser.parse_args()

    src = os.path.abspath(args.src)
    inner_src = os.path.abspath(args.inner_src or args.src)
    print("源码目录：", src)
    print("内层目录：", inner_src)
    version = preflight(src, inner_src)
    out = os.path.abspath(args.out or os.path.join(BASE, "video-tool-%s.fpk" % version))
    print("版本：", version)

    app_tgz = gzip_bytes(build_tar(INNER_SEQUENCE, inner_src))
    outer_raw = build_tar(OUTER_SEQUENCE, src, include_app_tgz=app_tgz,
                          fallback_src=inner_src)
    payload = gzip_bytes(outer_raw)
    with open(out, "wb") as fp:
        fp.write(payload)

    size, digest, outer_n, inner_n = verify(out, version)
    print("\n构建完成：%s" % out)
    print("  大小         : %d 字节" % size)
    print("  SHA-256      : %s" % digest)
    print("  外层条目     : %d 项（含 app.tgz）" % outer_n)
    print("  内层条目     : %d 项" % inner_n)
    print("  gzip 头      : 1f8b0800 ... 020a（无 FNAME / mtime=0 / OS=Unix）")
    print("  权限         : 目录与 cmd/* = 0755，其余文件 = 0644")
    print("  自检         : 全部通过（白名单、杂质、版本、权限、编码）")

    # 可复现性验证：同样源码再构建一次应得到相同字节
    again = gzip_bytes(build_tar(OUTER_SEQUENCE, src,
                                 include_app_tgz=gzip_bytes(build_tar(INNER_SEQUENCE, inner_src)),
                                 fallback_src=inner_src))
    assert again == payload, "同一源码两次打包结果不一致（不可复现）"
    print("  可复现性     : 通过（两次构建字节完全一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
