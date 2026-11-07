import os, re, tarfile, sys, io

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "fpk")


def package_version():
    """从 fpk/manifest 读取版本号，产物名随版本变化。"""
    with open(os.path.join(SRC, "manifest"), "r", encoding="utf-8-sig") as fp:
        for line in fp:
            m = re.match(r"\s*version\s*=\s*([^\s#]+)", line)
            if m:
                return m.group(1)
    print("MISSING: manifest version"); sys.exit(1)


OUT = os.path.join(BASE, "video-tool-%s.fpk" % package_version())

TEXT_EXTS = {".py", ".html", ".htm", ".css", ".js", ".json", ".txt",
             ".md", ".sh", ".conf", ".ini", ".yml", ".yaml", ".xml"}

def is_text(name):
    ext = os.path.splitext(name)[1].lower()
    return ext == "" or ext in TEXT_EXTS

def norm_bytes(b, name=""):
    if name and not is_text(name):
        return b
    b = b.replace(b"\r\n", b"\n")
    if b.startswith(b"\xef\xbb\xbf"): b = b[3:]
    return b

def add_bytes(tf, name, data, mode):
    ti = tarfile.TarInfo(name)
    ti.size = len(data); ti.mode = mode
    ti.uid=0; ti.gid=0; ti.uname="root"; ti.gname="root"
    tf.addfile(ti, io.BytesIO(data))

def add_dir(tf, name):
    ti = tarfile.TarInfo(name); ti.type = tarfile.DIRTYPE
    ti.mode = 0o755; ti.uid=0; ti.gid=0; ti.uname="root"; ti.gname="root"
    tf.addfile(ti)

def build_app_tgz():
    buf = io.BytesIO()
    appdir = os.path.join(SRC, "app")
    if not os.path.exists(appdir):
        print("MISSING: app/"); sys.exit(1)
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for root, dirs, files in os.walk(appdir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            dirs.sort(); files.sort()
            reldir = os.path.relpath(root, appdir).replace("\\","/")
            if reldir != ".":
                add_dir(tf, reldir)
            for f in files:
                if f.endswith((".pyc",".fixbak")): continue
                full = os.path.join(root, f)
                arc = f if reldir == "." else reldir + "/" + f
                in_bin = "bin" in reldir.split("/")
                raw = open(full,"rb").read()
                data = raw if in_bin else norm_bytes(raw, f)
                is_exec = f.endswith(".sh") or in_bin
                mode = 0o755 if is_exec else 0o644
                add_bytes(tf, arc, data, mode)
    return buf.getvalue()

def add_tree(tf, pkgpath, arcdir, usenorm):
    if os.path.isdir(pkgpath):
        add_dir(tf, arcdir)
        for name in sorted(os.listdir(pkgpath)):
            if name == "__pycache__" or name.endswith((".pyc",".fixbak")): continue
            add_tree(tf, os.path.join(pkgpath,name), arcdir+"/"+name, usenorm)
    else:
        data = open(pkgpath,"rb").read()
        if usenorm: data = norm_bytes(data, os.path.basename(pkgpath))
        mode = 0o755 if arcdir.startswith("cmd/") else 0o644
        add_bytes(tf, arcdir, data, mode)

if os.path.exists(OUT): os.remove(OUT)
app_tgz = build_app_tgz()

with tarfile.open(OUT, "w:gz") as tf:
    # 1. manifest
    mp = os.path.join(SRC,"manifest")
    add_bytes(tf, "manifest", norm_bytes(open(mp,"rb").read()), 0o644)
    # 2. 图标
    for ic in ["ICON.PNG","ICON_256.PNG"]:
        p = os.path.join(SRC, ic)
        if os.path.exists(p):
            add_bytes(tf, ic, open(p,"rb").read(), 0o644)
    # 3. cmd
    add_tree(tf, os.path.join(SRC,"cmd"), "cmd", True)
    # 4. config
    add_tree(tf, os.path.join(SRC,"config"), "config", True)
    # 5. app.tgz
    add_bytes(tf, "app.tgz", app_tgz, 0o644)
    # 6. wizard
    if os.path.exists(os.path.join(SRC,"wizard")):
        add_tree(tf, os.path.join(SRC,"wizard"), "wizard", True)

print("OK ->", OUT)
print("app.tgz size:", len(app_tgz))
