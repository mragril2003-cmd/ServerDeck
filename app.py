#!/usr/bin/env python3
"""
ServerDeck - a simple desktop app for hosting modded Minecraft servers.

Backend: a small local HTTP API (127.0.0.1 only, token protected) that
creates/starts/stops servers, installs mod loaders, Java, mods and modpacks.
Frontend: ui/index.html, shown in a native desktop window via pywebview.
"""
import collections
import concurrent.futures
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    print("Missing dependency 'psutil'. Run setup.bat first.")
    sys.exit(1)

APP_NAME = "ServerDeck"
VERSION = "1.0.0"
UA = f"ServerDeck/{VERSION} (desktop Minecraft server manager)"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

if getattr(sys, "frozen", False):  # PyInstaller build
    BASE = Path(sys.executable).resolve().parent
    UI_DIR = Path(getattr(sys, "_MEIPASS", BASE)) / "ui"
else:
    BASE = Path(__file__).resolve().parent
    UI_DIR = BASE / "ui"

DATA = Path(os.environ.get("SERVERDECK_DATA", BASE / "data"))
SERVERS = DATA / "servers"
JAVA_DIR = DATA / "java"
BACKUPS = DATA / "backups"
CACHE = DATA / "cache"
for _d in (SERVERS, JAVA_DIR, BACKUPS, CACHE):
    _d.mkdir(parents=True, exist_ok=True)

TOKEN = secrets.token_urlsafe(24)
MODRINTH = "https://api.modrinth.com/v2"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class UserError(Exception):
    """An error with a message meant for the user."""


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, timeout=30):
    return json.loads(http_get(url, timeout))


def http_post_json(url, body, headers=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json",
                                          "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def download(url, dest, job=None, label=None, sha1=None, headers=None):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".{uuid.uuid4().hex[:6]}.part")
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    h = hashlib.sha1()
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        last = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if job and total and time.time() - last > 0.3:
                last = time.time()
                job.set(progress=done / total,
                        message=f"{label or dest.name}: {done // 1048576} / {total // 1048576} MB")
    if sha1 and h.hexdigest().lower() != sha1.lower():
        tmp.unlink(missing_ok=True)
        raise UserError(f"Download corrupted (hash mismatch): {dest.name}")
    os.replace(tmp, dest)
    return dest


def safe_join(root, rel):
    """Join a relative path from an archive, refusing anything that escapes root.

    Purely text-based (no filesystem resolution), so it behaves the same on every
    OS and isn't affected by OneDrive folders, junctions or folders being created
    by other download threads at the same moment.
    """
    raw = str(rel).replace("\\", "/")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if (not parts or raw.startswith("/") or any(p == ".." for p in parts)
            or any(":" in p for p in parts)):
        raise UserError(f"Unsafe path in archive: {rel}")
    return Path(root).joinpath(*parts)


def slugify(s):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return (s or "server")[:32]


def kill_tree(pid):
    try:
        parent = psutil.Process(pid)
        for c in parent.children(recursive=True):
            c.kill()
        parent.kill()
    except psutil.Error:
        pass


def open_path(p):
    p = str(p)
    if IS_WIN:
        os.startfile(p)  # noqa
    elif IS_MAC:
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------
# Background jobs (installs, downloads, backups)
# --------------------------------------------------------------------------
class Job:
    def __init__(self, title, sid=None):
        self.id = uuid.uuid4().hex[:10]
        self.title = title
        self.sid = sid
        self.status = "running"
        self.progress = None
        self.message = "Starting..."
        self.log = []
        self.error = None
        self.result = None
        self.created = time.time()

    def set(self, progress="keep", message=None):
        if progress != "keep":
            self.progress = progress
        if message:
            self.message = message

    def step(self, message):
        self.message = message
        self.progress = None
        self.log.append(message)

    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("id", "title", "sid", "status", "progress", "message", "error", "result")} | {
                    "log": self.log[-50:]}


JOBS = {}


def run_job(title, fn, *args, sid=None):
    job = Job(title, sid)
    JOBS[job.id] = job

    def runner():
        try:
            job.result = fn(job, *args)
            job.status = "done"
            job.progress = 1
            job.message = "Done"
        except UserError as e:
            job.status, job.error = "error", str(e)
        except urllib.error.HTTPError as e:
            job.status, job.error = "error", f"Download failed ({e.code}) for {e.url}"
        except urllib.error.URLError as e:
            job.status, job.error = "error", f"Network error: {e.reason}. Check your internet connection."
        except Exception as e:  # noqa
            job.status, job.error = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=runner, daemon=True).start()
    return job


# --------------------------------------------------------------------------
# Minecraft versions & Java
# --------------------------------------------------------------------------
_manifest = {"t": 0, "data": None}


def mc_manifest():
    if not _manifest["data"] or time.time() - _manifest["t"] > 3600:
        _manifest["data"] = http_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        _manifest["t"] = time.time()
    return _manifest["data"]


def mc_versions(snapshots=False):
    m = mc_manifest()
    return [v["id"] for v in m["versions"] if snapshots or v["type"] == "release"]


def mc_version_info(ver):
    cache = CACHE / "versions" / f"{ver}.json"
    if cache.exists():
        return json.loads(cache.read_text("utf-8"))
    entry = next((v for v in mc_manifest()["versions"] if v["id"] == ver), None)
    if not entry:
        raise UserError(f"Unknown Minecraft version: {ver}")
    data = http_json(entry["url"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data), "utf-8")
    return data


def required_java(mc):
    try:
        return int(mc_version_info(mc).get("javaVersion", {}).get("majorVersion", 8))
    except Exception:  # noqa - offline or unknown version
        return 21


def java_exe():
    return "java.exe" if IS_WIN else "java"


def managed_java(major):
    d = JAVA_DIR / str(major)
    if d.exists():
        for p in d.rglob(java_exe()):
            if p.parent.name == "bin" and p.is_file():
                return p
    return None


def java_major_of(path):
    try:
        out = subprocess.run([str(path), "-version"], capture_output=True, text=True,
                             timeout=20, creationflags=NO_WINDOW)
        m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
        if not m:
            return None
        major = int(m.group(1))
        return int(m.group(2)) if major == 1 else major
    except (OSError, subprocess.SubprocessError):
        return None


def ensure_java(major, log=print):
    """Return a java executable for `major`, downloading Eclipse Temurin if needed."""
    major = 17 if major == 16 else major
    j = managed_java(major)
    if j:
        return j
    os_name = "windows" if IS_WIN else ("mac" if IS_MAC else "linux")
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    archive = None
    for image in ("jre", "jdk"):
        url = (f"https://api.adoptium.net/v3/binary/latest/{major}/ga/"
               f"{os_name}/{arch}/{image}/hotspot/normal/eclipse")
        log(f"Downloading Java {major} ({image.upper()})...")
        try:
            archive = download(url, CACHE / f"java-{major}-{image}.bin")
            break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    if not archive:
        raise UserError(f"Could not find a Java {major} download for this PC.")
    log(f"Installing Java {major}...")
    target = JAVA_DIR / str(major)
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                safe_join(target, m.filename)
            z.extractall(target)
    else:
        with tarfile.open(archive) as t:
            t.extractall(target, filter="data")
    archive.unlink(missing_ok=True)
    j = managed_java(major)
    if not j:
        raise UserError("Java download finished but java was not found inside it.")
    if not IS_WIN:
        j.chmod(0o755)
    log(f"Java {major} ready.")
    return j


def resolve_java(meta, log=print):
    custom = (meta.get("java_path") or "").strip()
    if custom:
        if not Path(custom).exists():
            raise UserError(f"Custom Java path not found: {custom}")
        return Path(custom)
    return ensure_java(int(meta.get("java_major") or 21), log)


# --------------------------------------------------------------------------
# Server metadata & files
# --------------------------------------------------------------------------
def sdir(sid):
    if not re.fullmatch(r"[a-z0-9-]+", sid or ""):
        raise UserError("Bad server id")
    d = SERVERS / sid
    if not d.exists():
        raise UserError("Server not found")
    return d


def load_meta(sid):
    return json.loads((sdir(sid) / "serverdeck.json").read_text("utf-8"))


def save_meta(sid, meta):
    p = SERVERS / sid / "serverdeck.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2), "utf-8")
    os.replace(tmp, p)


def list_server_ids():
    return sorted((p.parent.name for p in SERVERS.glob("*/serverdeck.json")),
                  key=lambda s: json.loads((SERVERS / s / "serverdeck.json").read_text("utf-8")).get("created", 0))


def new_server_dir(name):
    sid = f"{slugify(name)}-{uuid.uuid4().hex[:4]}"
    d = SERVERS / sid
    d.mkdir(parents=True)
    return sid, d


def base_meta(sid, name, mc, loader, ram_mb):
    return {
        "id": sid, "name": name, "mc": mc, "loader": loader, "loader_version": "",
        "ram_mb": int(ram_mb), "java_major": required_java(mc) if mc else 21, "java_path": "",
        "jvm_args": "", "auto_restart": False, "launch": {}, "mods": {}, "source": {},
        "created": time.time(),
    }


def read_props(d):
    p = Path(d) / "server.properties"
    out = {}
    if p.exists():
        for line in p.read_text("utf-8", errors="replace").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v
    return out


def write_props(d, updates):
    p = Path(d) / "server.properties"
    lines = p.read_text("utf-8", errors="replace").splitlines() if p.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in updates:
                lines[i] = f"{k}={updates[k]}"
                seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", "utf-8")


def accept_eula(d):
    (Path(d) / "eula.txt").write_text(
        "# Accepted via ServerDeck (https://aka.ms/MinecraftEULA)\neula=true\n", "utf-8")


# --------------------------------------------------------------------------
# Mod loader installers
# --------------------------------------------------------------------------
def detect_launch(d):
    d = Path(d)
    args_name = "win_args.txt" if IS_WIN else "unix_args.txt"
    for base in ("libraries/net/neoforged/neoforge", "libraries/net/neoforged/forge",
                 "libraries/net/minecraftforge/forge"):
        hits = sorted((d / base).glob(f"*/{args_name}")) if (d / base).exists() else []
        if hits:
            return {"type": "args", "args": hits[-1].relative_to(d).as_posix()}
    jars = [p.name for p in d.glob("*.jar")]
    lower = {j.lower(): j for j in jars}
    for pref in ("fabric-server-launch.jar", "quilt-server-launch.jar"):
        if pref in lower:
            return {"type": "jar", "jar": lower[pref]}
    for j in sorted(jars):
        jl = j.lower()
        if (jl.startswith("forge-") or jl.startswith("neoforge-")) and "installer" not in jl:
            return {"type": "jar", "jar": j}
    for pref in ("server.jar",):
        if pref in lower:
            return {"type": "jar", "jar": lower[pref]}
    for j in sorted(jars):
        if j.lower().startswith("minecraft_server"):
            return {"type": "jar", "jar": j}
    if IS_WIN:
        for s in ("startserver.bat", "start.bat", "run.bat", "ServerStart.bat"):
            for f in d.glob("*.bat"):
                if f.name.lower() == s.lower():
                    return {"type": "script", "script": f.name}
    return None


def run_installer(d, installer, java, job):
    job.step("Running mod loader installer (this can take a few minutes)...")
    proc = subprocess.Popen([str(java), "-jar", str(installer), "--installServer"], cwd=d,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW)
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if line:
            job.set(message=line[:140])
    if proc.wait() != 0:
        raise UserError("Mod loader installer failed. Check that the Minecraft version is supported.")
    Path(installer).unlink(missing_ok=True)
    for f in Path(d).glob("*installer*.log"):
        f.unlink(missing_ok=True)


def install_vanilla(d, mc, java, job, version=None):
    job.step(f"Downloading Minecraft {mc} server...")
    info = mc_version_info(mc)
    srv = info.get("downloads", {}).get("server")
    if not srv:
        raise UserError(f"Minecraft {mc} has no official server download.")
    download(srv["url"], Path(d) / "server.jar", job, sha1=srv.get("sha1"))
    return {"type": "jar", "jar": "server.jar"}, ""


def install_fabric(d, mc, java, job, version=None):
    job.step("Finding Fabric version...")
    loaders = http_json(f"https://meta.fabricmc.net/v2/versions/loader/{urllib.parse.quote(mc)}")
    if not loaders:
        raise UserError(f"Fabric does not support Minecraft {mc}.")
    lv = version or next((l["loader"]["version"] for l in loaders if l["loader"].get("stable")),
                         loaders[0]["loader"]["version"])
    installers = http_json("https://meta.fabricmc.net/v2/versions/installer")
    iv = next((i["version"] for i in installers if i.get("stable")), installers[0]["version"])
    job.step(f"Downloading Fabric {lv}...")
    download(f"https://meta.fabricmc.net/v2/versions/loader/{mc}/{lv}/{iv}/server/jar",
             Path(d) / "fabric-server-launch.jar", job)
    # Pre-fetch the vanilla jar the Fabric launcher needs (saves time on first start)
    try:
        install_vanilla(d, mc, java, job)
    except Exception:  # noqa - the launcher will fetch it itself
        pass
    return {"type": "jar", "jar": "fabric-server-launch.jar"}, lv


def neoforge_version(mc):
    vers = http_json("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")["versions"]
    if mc.startswith("1."):
        parts = mc.split(".")
        prefixes = [f"{parts[1]}.{parts[2] if len(parts) > 2 else '0'}."]
    else:  # year-based versions, e.g. 26.1 / 26.1.1
        prefixes = [mc + ".0.", mc + "."] if mc.count(".") == 1 else [mc + "."]
    for pre in prefixes:
        cands = [v for v in vers if v.startswith(pre)]
        if cands:
            stable = [v for v in cands if "beta" not in v and "alpha" not in v]
            return (stable or cands)[-1]
    raise UserError(f"NeoForge does not support Minecraft {mc} (NeoForge starts at 1.20.2).")


def install_neoforge(d, mc, java, job, version=None):
    job.step("Finding NeoForge version...")
    v = version or neoforge_version(mc)
    job.step(f"Downloading NeoForge {v} installer...")
    inst = download(f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{v}/neoforge-{v}-installer.jar",
                    Path(d) / f"neoforge-{v}-installer.jar", job)
    run_installer(d, inst, java, job)
    launch = detect_launch(d)
    if not launch:
        raise UserError("NeoForge installed but no launch files were found.")
    return launch, v


def install_forge(d, mc, java, job, version=None):
    job.step("Finding Forge version...")
    if not version:
        promos = http_json("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")["promos"]
        version = promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")
        if not version:
            raise UserError(f"Forge does not support Minecraft {mc}.")
    full = f"{mc}-{version}"
    job.step(f"Downloading Forge {version} installer...")
    inst = download(f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full}/forge-{full}-installer.jar",
                    Path(d) / f"forge-{full}-installer.jar", job)
    run_installer(d, inst, java, job)
    launch = detect_launch(d)
    if not launch:
        raise UserError("Forge installed but no launch files were found.")
    return launch, version


INSTALLERS = {"vanilla": install_vanilla, "fabric": install_fabric,
              "neoforge": install_neoforge, "forge": install_forge}


def job_create_server(job, name, mc, loader, ram_mb, eula):
    if not eula:
        raise UserError("You must accept the Minecraft EULA.")
    if loader not in INSTALLERS:
        raise UserError("Unknown server type.")
    sid, d = new_server_dir(name)
    try:
        meta = base_meta(sid, name, mc, loader, ram_mb)
        job.step(f"Checking Java {meta['java_major']}...")
        java = ensure_java(meta["java_major"], job.step)
        launch, lv = INSTALLERS[loader](d, mc, java, job)
        meta.update(launch=launch, loader_version=lv)
        accept_eula(d)
        write_props(d, {"motd": name})
        if loader != "vanilla":
            (d / "mods").mkdir(exist_ok=True)
        save_meta(sid, meta)
        return {"id": sid}
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise


# --------------------------------------------------------------------------
# Modrinth: mods & modpacks
# --------------------------------------------------------------------------
def mr_search(query, kind, mc=None, loader=None, offset=0):
    facets = [[f"project_type:{kind}"]]
    if kind == "mod":
        if mc:
            facets.append([f"versions:{mc}"])
        if loader and loader != "vanilla":
            facets.append([f"categories:{loader}"])
        facets.append(["server_side:required", "server_side:optional"])
    params = {"query": query or "", "facets": json.dumps(facets), "limit": 20, "offset": int(offset),
              "index": "relevance" if query else "downloads"}
    data = http_json(f"{MODRINTH}/search?{urllib.parse.urlencode(params)}")
    keep = ("project_id", "slug", "title", "description", "icon_url", "downloads", "author",
            "categories", "client_side", "server_side", "versions")
    return {"hits": [{k: h.get(k) for k in keep} for h in data.get("hits", [])],
            "total": data.get("total_hits", 0)}


def mr_versions(project, mc=None, loader=None):
    params = {}
    if loader:
        params["loaders"] = json.dumps([loader])
    if mc:
        params["game_versions"] = json.dumps([mc])
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    return http_json(f"{MODRINTH}/project/{urllib.parse.quote(project)}/version{q}")


def job_install_mod(job, sid, project_id, _depth=0):
    meta = load_meta(sid)
    if meta["loader"] == "vanilla":
        raise UserError("This is a Vanilla server. Create a Fabric, NeoForge or Forge server to use mods.")
    installed = {m.get("project_id") for m in meta.get("mods", {}).values()}
    if project_id in installed and _depth:
        return
    job.step(f"Finding a version for Minecraft {meta['mc']} / {meta['loader']}...")
    vers = mr_versions(project_id, meta["mc"], meta["loader"])
    if not vers:
        raise UserError(f"No version of this mod for Minecraft {meta['mc']} on {meta['loader'].title()}.")
    v = vers[0]
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    title = project_id
    try:
        title = http_json(f"{MODRINTH}/project/{project_id}").get("title", project_id)
    except Exception:  # noqa
        pass
    job.step(f"Downloading {title}...")
    mods = SERVERS / sid / "mods"
    for old_name, m in list(meta.get("mods", {}).items()):  # replace older version of same mod
        if m.get("project_id") == project_id and old_name != f["filename"]:
            (mods / old_name).unlink(missing_ok=True)
            (mods / (old_name + ".disabled")).unlink(missing_ok=True)
            meta["mods"].pop(old_name, None)
    download(f["url"], mods / f["filename"], job, sha1=f.get("hashes", {}).get("sha1"))
    meta = load_meta(sid) | {"mods": meta.get("mods", {})}
    meta["mods"][f["filename"]] = {"project_id": project_id, "version_id": v["id"], "title": title,
                                   "version": v.get("version_number")}
    save_meta(sid, meta)
    if _depth < 4:
        for dep in v.get("dependencies", []):
            if dep.get("dependency_type") == "required" and dep.get("project_id"):
                try:
                    job_install_mod(job, sid, dep["project_id"], _depth + 1)
                except UserError as e:
                    job.step(f"Warning: dependency skipped: {e}")
    return {"title": title}


def install_mrpack(job, pack_path, name, ram_mb, source=None):
    with zipfile.ZipFile(pack_path) as z:
        try:
            index = json.loads(z.read("modrinth.index.json"))
        except KeyError:
            raise UserError("Not a valid Modrinth modpack (.mrpack).")
        deps = index.get("dependencies", {})
        mc = deps.get("minecraft")
        if not mc:
            raise UserError("Modpack does not say which Minecraft version it uses.")
        if "neoforge" in deps:
            loader, lv = "neoforge", deps["neoforge"]
        elif "forge" in deps:
            loader, lv = "forge", deps["forge"]
        elif "fabric-loader" in deps:
            loader, lv = "fabric", deps["fabric-loader"]
        elif "quilt-loader" in deps:
            raise UserError("Quilt modpacks are not supported yet. Pick a Fabric, NeoForge or Forge pack.")
        else:
            loader, lv = "vanilla", None
        sid, d = new_server_dir(name or index.get("name", "Modpack"))
        try:
            meta = base_meta(sid, name or index.get("name", "Modpack"), mc, loader, ram_mb)
            meta["source"] = source or {"type": "mrpack", "title": index.get("name")}
            job.step(f"Checking Java {meta['java_major']}...")
            java = ensure_java(meta["java_major"], job.step)
            launch, lv = INSTALLERS[loader](d, mc, java, job, lv)
            meta.update(launch=launch, loader_version=lv)

            files = [f for f in index.get("files", [])
                     if (f.get("env") or {}).get("server") != "unsupported"]
            job.step(f"Downloading {len(files)} mod files...")
            done = [0]

            def fetch(f):
                dest = safe_join(d, f["path"])
                err = None
                for url in f.get("downloads", []):
                    try:
                        download(url, dest, sha1=f.get("hashes", {}).get("sha1"))
                        err = None
                        break
                    except Exception as e:  # noqa
                        err = e
                if err:
                    raise UserError(f"Failed to download {f['path']}: {err}")
                done[0] += 1
                job.set(progress=done[0] / max(1, len(files)),
                        message=f"Downloading mods: {done[0]} / {len(files)}")

            with concurrent.futures.ThreadPoolExecutor(6) as ex:
                list(ex.map(fetch, files))

            job.step("Copying config and override files...")
            for prefix in ("overrides/", "server-overrides/"):
                for m in z.infolist():
                    if m.filename.startswith(prefix) and not m.is_dir():
                        rel = m.filename[len(prefix):]
                        if not rel:
                            continue
                        dest = safe_join(d, rel)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(m) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
            accept_eula(d)
            write_props(d, {"motd": meta["name"]})
            save_meta(sid, meta)
            return {"id": sid}
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise


def job_install_modpack(job, project_id, version_id, name, ram_mb, eula):
    if not eula:
        raise UserError("You must accept the Minecraft EULA.")
    job.step("Fetching modpack info...")
    v = http_json(f"{MODRINTH}/version/{urllib.parse.quote(version_id)}")
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    if not f["filename"].endswith(".mrpack"):
        raise UserError("This modpack version has no .mrpack file.")
    job.step(f"Downloading modpack {v.get('name', '')}...")
    pack = download(f["url"], CACHE / "packs" / f["filename"], job, sha1=f.get("hashes", {}).get("sha1"))
    try:
        return install_mrpack(job, pack, name, ram_mb,
                              {"type": "modrinth", "project_id": project_id, "version_id": version_id,
                               "title": name, "version": v.get("version_number")})
    finally:
        pack.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# App config & CurseForge
# --------------------------------------------------------------------------
CONFIG_PATH = DATA / "config.json"
CF = "https://api.curseforge.com"
CF_LOADERS = {1: "forge", 4: "fabric", 5: "quilt", 6: "neoforge"}
MCVER = re.compile(r"\d+\.\d+(?:\.\d+)?")


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), "utf-8")


def cf_key():
    k = (load_config().get("cf_api_key") or "").strip()
    if not k:
        raise UserError("Add your CurseForge API key first (App settings in the bottom-left).")
    return k


def cf_api(path, body=None, key=None):
    hdr = {"x-api-key": key or cf_key(), "Accept": "application/json"}
    try:
        if body is not None:
            return http_post_json(CF + path, body, hdr)
        req = urllib.request.Request(CF + path, headers={"User-Agent": UA, **hdr})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise UserError("CurseForge rejected the API key. Check it in App settings.")
        raise


def cf_search(query, offset=0):
    params = {"gameId": 432, "classId": 4471, "sortField": 2, "sortOrder": "desc",
              "pageSize": 20, "index": int(offset)}
    if query:
        params["searchFilter"] = query
    data = cf_api("/v1/mods/search?" + urllib.parse.urlencode(params))
    hits = []
    for m in data.get("data", []):
        loaders = sorted({CF_LOADERS[i["modLoader"]] for i in m.get("latestFilesIndexes", [])
                          if i.get("modLoader") in CF_LOADERS})
        hits.append({"project_id": str(m["id"]), "title": m.get("name"), "description": m.get("summary"),
                     "icon_url": (m.get("logo") or {}).get("thumbnailUrl"), "downloads": m.get("downloadCount", 0),
                     "author": ((m.get("authors") or [{}])[0]).get("name", ""), "categories": loaders})
    return {"hits": hits, "total": data.get("pagination", {}).get("totalCount", 0)}


def cf_files(project):
    data = cf_api(f"/v1/mods/{int(project)}/files?pageSize=50")["data"]
    data = sorted((f for f in data if not f.get("isServerPack")), key=lambda f: f.get("fileDate", ""), reverse=True)
    out = []
    for f in data:
        gv = f.get("gameVersions", [])
        out.append({"id": str(f["id"]), "name": f.get("displayName") or f.get("fileName"),
                    "mc": [v for v in gv if MCVER.fullmatch(v)],
                    "loaders": [v.lower() for v in gv if v.lower() in ("forge", "neoforge", "fabric", "quilt")],
                    "date": (f.get("fileDate") or "")[:10], "server_pack": bool(f.get("serverPackFileId"))})
    return out


def modrinth_client_only(sha1s):
    """Use Modrinth's metadata to spot client-only mods (CurseForge has no such flag)."""
    sha1s = [s for s in sha1s if s]
    if not sha1s:
        return set()
    try:
        vers = http_post_json(f"{MODRINTH}/version_files", {"hashes": sha1s, "algorithm": "sha1"})
        pids = sorted({v["project_id"] for v in vers.values()})
        side = {}
        for i in range(0, len(pids), 100):
            q = urllib.parse.urlencode({"ids": json.dumps(pids[i:i + 100])})
            for p in http_json(f"{MODRINTH}/projects?{q}"):
                side[p["id"]] = p.get("server_side")
        return {h for h, v in vers.items() if side.get(v["project_id"]) == "unsupported"}
    except Exception:  # noqa - best effort only
        return set()


def install_cf_manifest(job, pack, name, ram_mb, source, hdr):
    with zipfile.ZipFile(pack) as z:
        try:
            man = json.loads(z.read("manifest.json"))
        except KeyError:
            raise UserError("Not a CurseForge modpack (no manifest.json).")
        mc = man["minecraft"]["version"]
        lds = man["minecraft"].get("modLoaders", [])
        ld = next((l for l in lds if l.get("primary")), lds[0] if lds else None)
        loader, lv = "vanilla", None
        if ld:
            kind, _, lv = ld["id"].partition("-")
            loader = kind.lower()
            if loader == "quilt":
                raise UserError("Quilt modpacks are not supported yet.")
            if loader not in INSTALLERS or (loader == "neoforge" and lv.startswith("1.20.1")):
                raise UserError(f"Unsupported mod loader: {ld['id']}")
        files = [x for x in man.get("files", []) if x.get("required", True)]
        sid, d = new_server_dir(name)
        try:
            meta = base_meta(sid, name, mc, loader, ram_mb)
            meta["source"] = source
            job.step(f"Checking Java {meta['java_major']}...")
            java = ensure_java(meta["java_major"], job.step)
            launch, lv = INSTALLERS[loader](d, mc, java, job, lv or None)
            meta.update(launch=launch, loader_version=lv)

            job.step(f"Looking up {len(files)} mod files...")
            infos, mods_info = {}, {}
            fids = [x["fileID"] for x in files]
            for i in range(0, len(fids), 500):
                for it in cf_api("/v1/mods/files", {"fileIds": fids[i:i + 500]})["data"]:
                    infos[it["id"]] = it
            mids = sorted({x["projectID"] for x in files})
            for i in range(0, len(mids), 500):
                for m in cf_api("/v1/mods", {"modIds": mids[i:i + 500]})["data"]:
                    mods_info[m["id"]] = m

            def sha1_of(it):
                return next((h["value"] for h in it.get("hashes", []) if h.get("algo") == 1), None)

            job.step("Checking which mods are client-only...")
            client_only = modrinth_client_only([sha1_of(it) for it in infos.values()])
            todo, manual, skipped = [], [], []
            for x in files:
                it, m = infos.get(x["fileID"]), mods_info.get(x["projectID"], {})
                if not it:
                    skipped.append(f"project {x['projectID']} (not found)")
                    continue
                if m.get("classId") not in (6, None):  # resource packs, shaders, worlds...
                    continue
                if sha1_of(it) in client_only:
                    skipped.append(it["fileName"])
                    continue
                if not it.get("downloadUrl"):
                    site = (m.get("links") or {}).get("websiteUrl") or "https://www.curseforge.com"
                    manual.append({"name": m.get("name") or it["fileName"], "file": it["fileName"],
                                   "url": f"{site}/files/{it['id']}"})
                    continue
                todo.append(it)
            if skipped:
                job.step(f"Skipped {len(skipped)} client-only mods.")

            done = [0]

            def fetch(it):
                download(it["downloadUrl"], safe_join(d / "mods", it["fileName"]), sha1=sha1_of(it), headers=hdr)
                done[0] += 1
                job.set(progress=done[0] / max(1, len(todo)), message=f"Downloading mods: {done[0]} / {len(todo)}")

            job.step(f"Downloading {len(todo)} mods...")
            with concurrent.futures.ThreadPoolExecutor(6) as ex:
                list(ex.map(fetch, todo))

            job.step("Copying config and override files...")
            prefix = (man.get("overrides") or "overrides").strip("/") + "/"
            for m in z.infolist():
                if m.filename.startswith(prefix) and not m.is_dir() and m.filename != prefix:
                    dest = safe_join(d, m.filename[len(prefix):])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(m) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
            accept_eula(d)
            write_props(d, {"motd": name, "allow-flight": "true"})
            meta["manual_mods"] = manual
            meta["skipped_mods"] = skipped
            save_meta(sid, meta)
            return {"id": sid, "manual": manual, "skipped": len(skipped)}
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise


def job_install_cf_pack(job, project_id, file_id, name, ram_mb, eula):
    if not eula:
        raise UserError("You must accept the Minecraft EULA.")
    hdr = {"x-api-key": cf_key()}
    pid, fid = int(project_id), int(file_id)
    job.step("Fetching modpack info...")
    f = cf_api(f"/v1/mods/{pid}/files/{fid}")["data"]
    mcs = [v for v in f.get("gameVersions", []) if MCVER.fullmatch(v)]
    source = {"type": "curseforge", "project_id": pid, "file_id": fid, "title": name,
              "version": f.get("displayName")}
    if f.get("serverPackFileId"):
        try:
            sp = cf_api(f"/v1/mods/{pid}/files/{f['serverPackFileId']}")["data"]
            if sp.get("downloadUrl"):
                job.step("Downloading the official server pack...")
                up = download(sp["downloadUrl"], CACHE / "uploads" / f"{uuid.uuid4().hex}-{sp['fileName']}",
                              job, headers=hdr)
                res = job_import_pack(job, up, sp["fileName"], name, ram_mb, mcs[0] if mcs else None, True)
                meta = load_meta(res["id"])
                meta["source"] = source
                save_meta(res["id"], meta)
                return res
        except UserError as e:
            job.step(f"Server pack could not be used ({e}). Building from the modpack instead...")
    if not f.get("downloadUrl"):
        raise UserError("This modpack's author only allows downloads through the CurseForge app. "
                        "Download its Server Pack from the website and use Import a file.")
    job.step("Downloading modpack...")
    pack = download(f["downloadUrl"], CACHE / "packs" / f["fileName"], job, headers=hdr)
    try:
        return install_cf_manifest(job, pack, name, ram_mb, source, hdr)
    finally:
        pack.unlink(missing_ok=True)


def guess_mc_version(d):
    d = Path(d)
    for base in ("libraries/net/minecraft/server", "libraries/net/minecraft/server-release"):
        p = d / base
        if p.exists():
            vs = sorted(x.name for x in p.iterdir() if x.is_dir())
            if vs:
                return vs[-1].split("-")[0]
    for f in d.glob("*.jar"):
        m = re.search(r"(?:minecraft_server|forge|neoforge)[.-](1\.\d+(?:\.\d+)?)", f.name)
        if m:
            return m.group(1)
    for f in d.glob("*installer*.jar"):
        m = re.search(r"forge-(1\.\d+(?:\.\d+)?)-", f.name)
        if m:
            return m.group(1)
    return None


def job_import_pack(job, upload, filename, name, ram_mb, mc, eula):
    if not eula:
        raise UserError("You must accept the Minecraft EULA.")
    try:
        if filename.lower().endswith(".mrpack"):
            return install_mrpack(job, upload, name, ram_mb, {"type": "file", "title": filename})
        if not zipfile.is_zipfile(upload):
            raise UserError("Unsupported file. Use a .mrpack or a server pack .zip.")
        with zipfile.ZipFile(upload) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            if "manifest.json" in names and not any(n.startswith("mods/") for n in names):
                man = json.loads(z.read("manifest.json"))
                if man.get("manifestType") == "minecraftModpack":
                    key = (load_config().get("cf_api_key") or "").strip()
                    if key:
                        return install_cf_manifest(job, upload, name or Path(filename).stem, ram_mb,
                                                   {"type": "file", "title": filename}, {"x-api-key": key})
                    raise UserError("This is a CurseForge CLIENT pack. On the modpack's CurseForge page, open "
                                    "Files, pick the version, and download the 'Server Pack' instead.")
            tops = {n.split("/", 1)[0] for n in names}
            strip = (tops.pop() + "/") if len(tops) == 1 and all("/" in n for n in names) else ""
            sid, d = new_server_dir(name or Path(filename).stem)
            try:
                job.step("Extracting server pack...")
                for i, m in enumerate(z.infolist()):
                    if m.is_dir() or not m.filename.startswith(strip):
                        continue
                    rel = m.filename[len(strip):]
                    dest = safe_join(d, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(m) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    if i % 25 == 0:
                        job.set(progress=i / max(1, len(names)), message=f"Extracting {i} / {len(names)}")
                mc = mc or guess_mc_version(d)
                if not mc:
                    raise UserError("Could not detect the Minecraft version. Pick it in the import window.")
                loader = "vanilla"
                if list(d.glob("*neoforge*")) or (d / "libraries/net/neoforged").exists():
                    loader = "neoforge"
                elif list(d.glob("forge-*")) or (d / "libraries/net/minecraftforge").exists():
                    loader = "forge"
                elif list(d.glob("fabric-server*")) or (d / ".fabric").exists():
                    loader = "fabric"
                meta = base_meta(sid, name or Path(filename).stem, mc, loader, ram_mb)
                meta["source"] = {"type": "file", "title": filename}
                job.step(f"Checking Java {meta['java_major']}...")
                java = ensure_java(meta["java_major"], job.step)
                launch = detect_launch(d)
                if not launch or launch["type"] == "script":
                    inst = sorted(d.glob("*installer*.jar"))
                    if inst:
                        run_installer(d, inst[0], java, job)
                        launch = detect_launch(d)
                    elif loader == "fabric" and not (d / "fabric-server-launch.jar").exists():
                        launch, _ = install_fabric(d, mc, java, job)
                if not launch:
                    raise UserError("Could not find how to start this server pack.")
                meta["launch"] = launch
                accept_eula(d)
                save_meta(sid, meta)
                return {"id": sid}
            except BaseException:
                shutil.rmtree(d, ignore_errors=True)
                raise
    finally:
        Path(upload).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Running servers
# --------------------------------------------------------------------------
JOIN_RE = re.compile(r"\]: (?:\[[^\]]*\] )?([A-Za-z0-9_]{2,16}) joined the game")
LEFT_RE = re.compile(r"\]: (?:\[[^\]]*\] )?([A-Za-z0-9_]{2,16}) left the game")


class ServerProc:
    def __init__(self, sid):
        self.sid = sid
        self.proc = None
        self.status = "stopped"
        self.lines = collections.deque(maxlen=4000)
        self.seq = 0
        self.lock = threading.Lock()
        self.players = set()
        self.history = collections.deque(maxlen=300)  # ~10 min at 2s
        self.started_at = None
        self.ps = None
        self.want_stop = False
        self.ram_at_start = None

    # console -----------------------------------------------------------
    def log(self, text, kind="out"):
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, text, kind))

    def console_after(self, after):
        with self.lock:
            return [{"n": n, "t": t, "k": k} for n, t, k in self.lines if n > after]

    # lifecycle ---------------------------------------------------------
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def build_cmd(self, meta, java):
        ram = int(meta["ram_mb"])
        launch = meta.get("launch") or {}
        if launch.get("type") == "script":
            return ["cmd", "/c", launch["script"]]
        cmd = [str(java), f"-Xmx{ram}M", f"-Xms{min(ram, 2048)}M", "-XX:+UseG1GC",
               "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200", "-XX:+DisableExplicitGC"]
        if meta.get("jvm_args"):
            cmd += shlex.split(meta["jvm_args"], posix=not IS_WIN)
        if launch.get("type") == "args":
            cmd.append("@" + launch["args"])
        elif launch.get("type") == "jar":
            cmd += ["-jar", launch["jar"]]
        else:
            raise UserError("This server has no launch file. Try re-creating it.")
        return cmd + ["nogui"]

    def start(self):
        if self.running() or self.status in ("starting", "stopping"):
            raise UserError("Server is already running.")
        meta = load_meta(self.sid)
        self.status = "starting"
        self.want_stop = False
        self.players.clear()
        threading.Thread(target=self._launch, args=(meta,), daemon=True).start()

    def _launch(self, meta):
        d = SERVERS / self.sid
        try:
            java = resolve_java(meta, lambda m: self.log(f"[ServerDeck] {m}", "sys"))
            cmd = self.build_cmd(meta, java)
            if meta.get("launch", {}).get("type") == "script":
                self.log("[ServerDeck] Using the pack's start script; the RAM slider may not apply.", "sys")
            self.log(f"[ServerDeck] Starting with {meta['ram_mb']} MB RAM (Java {meta['java_major']})...", "sys")
            self.proc = subprocess.Popen(cmd, cwd=d, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            self.ps = psutil.Process(self.proc.pid)
            self.ps.cpu_percent(None)
            self.started_at = time.time()
            self.ram_at_start = meta["ram_mb"]
        except Exception as e:  # noqa
            self.status = "stopped"
            self.log(f"[ServerDeck] Could not start: {e}", "err")
            return
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            self.log(line)
            if self.status == "starting" and "Done (" in line and "help" in line:
                self.status = "running"
                self.log("[ServerDeck] Server is online.", "sys")
            m = JOIN_RE.search(line)
            if m:
                self.players.add(m.group(1))
            m = LEFT_RE.search(line)
            if m:
                self.players.discard(m.group(1))
        code = self.proc.wait()
        crashed = code != 0 and not self.want_stop
        self.status = "stopped"
        self.players.clear()
        self.ps = None
        self.started_at = None
        self.log(f"[ServerDeck] Server stopped (exit code {code}).", "err" if crashed else "sys")
        if crashed:
            self.log("[ServerDeck] It looks like the server crashed. Check the lines above or the "
                     "crash-reports folder. A mod may be missing, client-only, or need more RAM.", "err")
            if load_meta(self.sid).get("auto_restart"):
                self.log("[ServerDeck] Auto-restart in 10 seconds...", "sys")
                time.sleep(10)
                if not self.running() and not self.want_stop:
                    try:
                        self.start()
                    except UserError:
                        pass

    def send(self, cmd):
        if not self.running():
            raise UserError("Server is not running.")
        self.log(f"> {cmd}", "cmd")
        try:
            self.proc.stdin.write((cmd + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError:
            raise UserError("Could not send command.")

    def stop(self, wait=False):
        if not self.running():
            self.status = "stopped"
            return
        self.want_stop = True
        self.status = "stopping"
        try:
            self.send("stop")
        except UserError:
            pass

        def waiter():
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.log("[ServerDeck] Server did not stop in 60s; forcing it.", "err")
                kill_tree(self.proc.pid)
        if wait:
            waiter()
        else:
            threading.Thread(target=waiter, daemon=True).start()

    def kill(self):
        if self.proc and self.running():
            self.want_stop = True
            kill_tree(self.proc.pid)
            self.log("[ServerDeck] Server was force-killed.", "err")

    def restart(self):
        def go():
            self.stop(wait=True)
            for _ in range(100):
                if self.status == "stopped":
                    break
                time.sleep(0.2)
            self.start()
        threading.Thread(target=go, daemon=True).start()

    # stats -------------------------------------------------------------
    def sample(self):
        cpu = mem = 0.0
        if self.ps and self.running():
            try:
                kids = {c.pid: c for c in self.ps.children(recursive=True)}
                cache = getattr(self, "_kids", {})
                self._kids = {pid: cache.get(pid, c) for pid, c in kids.items()}
                for p in [self.ps, *self._kids.values()]:
                    try:
                        cpu += p.cpu_percent(None)
                        mem += p.memory_info().rss
                    except psutil.Error:
                        pass
            except psutil.Error:
                pass
        self.history.append({"t": round(time.time()), "cpu": round(cpu / (psutil.cpu_count() or 1), 1),
                             "mem": round(mem / 1048576), "players": len(self.players)})


PROCS = {}
PROCS_LOCK = threading.Lock()


def proc_for(sid):
    with PROCS_LOCK:
        if sid not in PROCS:
            sdir(sid)
            PROCS[sid] = ServerProc(sid)
        return PROCS[sid]


def sampler():
    while True:
        for p in list(PROCS.values()):
            if p.running() or p.history:
                p.sample()
        time.sleep(2)


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------
BACKUP_SKIP = {"libraries", "logs", "crash-reports", ".fabric", "cache", "versions"}


def job_backup(job, sid):
    d = sdir(sid)
    p = PROCS.get(sid)
    live = p and p.running()
    if live:
        p.send("save-off")
        p.send("save-all flush")
        time.sleep(4)
    try:
        out = BACKUPS / sid / time.strftime("%Y-%m-%d_%H-%M-%S.zip")
        out.parent.mkdir(parents=True, exist_ok=True)
        files = [f for f in d.rglob("*") if f.is_file() and f.relative_to(d).parts[0] not in BACKUP_SKIP]
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
            for i, f in enumerate(files):
                try:
                    z.write(f, f.relative_to(d).as_posix())
                except (PermissionError, OSError):
                    job.step(f"Skipped locked file: {f.name}")
                if i % 20 == 0:
                    job.set(progress=i / max(1, len(files)), message=f"Backing up {i} / {len(files)} files")
        return {"file": out.name}
    finally:
        if live and p.running():
            p.send("save-on")


def list_backups(sid):
    d = BACKUPS / sid
    if not d.exists():
        return []
    return [{"file": f.name, "size": f.stat().st_size, "time": f.stat().st_mtime}
            for f in sorted(d.glob("*.zip"), reverse=True)]


def job_restore(job, sid, file):
    d = sdir(sid)
    if PROCS.get(sid) and PROCS[sid].running():
        raise UserError("Stop the server before restoring a backup.")
    src = BACKUPS / sid / Path(file).name
    if not src.exists():
        raise UserError("Backup not found.")
    with zipfile.ZipFile(src) as z:
        tops = {n.split("/", 1)[0] for n in z.namelist() if "/" in n}
        job.step("Removing current world...")
        level = read_props(d).get("level-name", "world")
        for t in tops:
            if t.startswith(level) or t in ("mods", "config"):
                shutil.rmtree(d / t, ignore_errors=True)
        job.step("Restoring files...")
        for m in z.infolist():
            safe_join(d, m.filename)
        z.extractall(d)
    return {}


# --------------------------------------------------------------------------
# Server info for the UI
# --------------------------------------------------------------------------
def server_summary(sid):
    meta = load_meta(sid)
    p = PROCS.get(sid)
    return {"id": sid, "name": meta["name"], "mc": meta["mc"], "loader": meta["loader"],
            "status": p.status if p else "stopped", "players": len(p.players) if p else 0}


def server_detail(sid):
    meta = load_meta(sid)
    d = sdir(sid)
    p = proc_for(sid)
    props = read_props(d)
    mods_dir = d / "mods"
    mod_count = len(list(mods_dir.glob("*.jar"))) if mods_dir.exists() else 0
    return {
        **meta,
        "status": p.status,
        "players_online": sorted(p.players),
        "max_players": int(props.get("max-players", 20) or 20),
        "port": int(props.get("server-port", 25565) or 25565),
        "motd": props.get("motd", ""),
        "local_ip": local_ip(),
        "uptime": round(time.time() - p.started_at) if p.started_at else 0,
        "mod_count": mod_count,
        "ram_pending": bool(p.running() and p.ram_at_start and p.ram_at_start != meta["ram_mb"]),
        "path": str(d),
    }


def list_mods(sid):
    meta = load_meta(sid)
    d = sdir(sid) / "mods"
    out = []
    if d.exists():
        for f in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            if f.suffix == ".jar" or f.name.endswith(".jar.disabled"):
                base = f.name[:-9] if f.name.endswith(".disabled") else f.name
                info = meta.get("mods", {}).get(base, {})
                out.append({"file": base, "enabled": f.suffix == ".jar", "size": f.stat().st_size,
                            "title": info.get("title") or re.sub(r"[-_][\d.].*$", "", base[:-4]) or base,
                            "version": info.get("version", ""), "project_id": info.get("project_id")})
    return out


def system_info():
    vm = psutil.virtual_memory()
    return {"ram_total_mb": vm.total // 1048576, "ram_used_mb": (vm.total - vm.available) // 1048576,
            "cpus": psutil.cpu_count(), "cpu_percent": psutil.cpu_percent(None), "os": platform.platform(),
            "version": VERSION}


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


@route("GET", r"/api/state")
def api_state(q, b):
    ids = list_server_ids()
    return {"servers": [server_summary(s) for s in ids], "system": system_info(),
            "jobs": [j.to_dict() for j in JOBS.values() if j.status == "running"]}


@route("GET", r"/api/mc_versions")
def api_mc_versions(q, b):
    return {"versions": mc_versions(q.get("snapshots") == "1")}


@route("POST", r"/api/create")
def api_create(q, b):
    name = (b.get("name") or "").strip() or "My Server"
    job = run_job(f"Creating {name}", job_create_server, name, b["mc"], b.get("loader", "fabric"),
                  int(b.get("ram_mb", 4096)), bool(b.get("eula")))
    return {"job": job.id}


@route("GET", r"/api/job/(\w+)")
def api_job(q, b, jid):
    j = JOBS.get(jid)
    if not j:
        raise UserError("Job not found")
    return j.to_dict()


@route("GET", r"/api/server/([a-z0-9-]+)")
def api_server(q, b, sid):
    return server_detail(sid)


@route("GET", r"/api/server/([a-z0-9-]+)/stats")
def api_stats(q, b, sid):
    p = proc_for(sid)
    return {"history": list(p.history), "ram_mb": load_meta(sid)["ram_mb"]}


@route("GET", r"/api/server/([a-z0-9-]+)/console")
def api_console(q, b, sid):
    p = proc_for(sid)
    return {"lines": p.console_after(int(q.get("after", 0))), "status": p.status}


@route("POST", r"/api/server/([a-z0-9-]+)/action")
def api_action(q, b, sid):
    p = proc_for(sid)
    a = b.get("action")
    if a == "start":
        p.start()
    elif a == "stop":
        p.stop()
    elif a == "restart":
        p.restart()
    elif a == "kill":
        p.kill()
    else:
        raise UserError("Unknown action")
    return {"ok": True}


@route("POST", r"/api/server/([a-z0-9-]+)/command")
def api_command(q, b, sid):
    cmd = (b.get("cmd") or "").strip().lstrip("/")
    if cmd:
        proc_for(sid).send(cmd)
    return {"ok": True}


@route("POST", r"/api/server/([a-z0-9-]+)/settings")
def api_settings(q, b, sid):
    meta = load_meta(sid)
    if "ram_mb" in b:
        ram = int(b["ram_mb"])
        total = psutil.virtual_memory().total // 1048576
        if ram < 1024 or ram > total:
            raise UserError("RAM must be between 1 GB and your PC's total RAM.")
        meta["ram_mb"] = ram
    for k in ("name", "jvm_args", "java_path"):
        if k in b:
            meta[k] = str(b[k]).strip()
    if "auto_restart" in b:
        meta["auto_restart"] = bool(b["auto_restart"])
    if "java_major" in b:
        meta["java_major"] = int(b["java_major"])
    if b.get("clear_manual"):
        meta["manual_mods"] = []
    save_meta(sid, meta)
    return {"ok": True}


@route("GET", r"/api/config")
def api_config(q, b):
    return {"cf_key_set": bool((load_config().get("cf_api_key") or "").strip())}


@route("POST", r"/api/config")
def api_config_save(q, b):
    cfg = load_config()
    if "cf_api_key" in b:
        k = str(b["cf_api_key"] or "").strip()
        if k:
            cf_api("/v1/games/432", key=k)  # validates the key
        cfg["cf_api_key"] = k
    save_config(cfg)
    return {"cf_key_set": bool(cfg.get("cf_api_key"))}


@route("GET", r"/api/curseforge/search")
def api_cf_search(q, b):
    return cf_search(q.get("q", ""), q.get("offset", 0))


@route("GET", r"/api/curseforge/files")
def api_cf_files(q, b):
    return {"versions": cf_files(q["project"])}


@route("POST", r"/api/curseforge/install")
def api_cf_install(q, b):
    name = (b.get("name") or "Modpack").strip()
    job = run_job(f"Installing {name}", job_install_cf_pack, b["project_id"], b["file_id"], name,
                  int(b.get("ram_mb", 6144)), bool(b.get("eula")))
    return {"job": job.id}


@route("GET", r"/api/server/([a-z0-9-]+)/properties")
def api_props(q, b, sid):
    return {"props": read_props(sdir(sid))}


@route("POST", r"/api/server/([a-z0-9-]+)/properties")
def api_props_save(q, b, sid):
    clean = {str(k).strip(): str(v).replace("\n", " ") for k, v in (b.get("props") or {}).items()
             if re.fullmatch(r"[a-zA-Z0-9.\-_]+", str(k).strip())}
    write_props(sdir(sid), clean)
    return {"ok": True}


@route("GET", r"/api/server/([a-z0-9-]+)/mods")
def api_mods(q, b, sid):
    return {"mods": list_mods(sid)}


@route("POST", r"/api/server/([a-z0-9-]+)/mods/toggle")
def api_mod_toggle(q, b, sid):
    d = sdir(sid) / "mods"
    name = Path(b["file"]).name
    on, off = d / name, d / (name + ".disabled")
    if on.exists():
        on.rename(off)
    elif off.exists():
        off.rename(on)
    return {"ok": True}


@route("POST", r"/api/server/([a-z0-9-]+)/mods/delete")
def api_mod_delete(q, b, sid):
    d = sdir(sid) / "mods"
    name = Path(b["file"]).name
    (d / name).unlink(missing_ok=True)
    (d / (name + ".disabled")).unlink(missing_ok=True)
    meta = load_meta(sid)
    meta.get("mods", {}).pop(name, None)
    save_meta(sid, meta)
    return {"ok": True}


@route("POST", r"/api/server/([a-z0-9-]+)/mods/install")
def api_mod_install(q, b, sid):
    job = run_job(f"Installing mod", job_install_mod, sid, b["project_id"], sid=sid)
    return {"job": job.id}


@route("GET", r"/api/modrinth/search")
def api_mr_search(q, b):
    mc = loader = None
    if q.get("sid"):
        meta = load_meta(q["sid"])
        mc, loader = meta["mc"], meta["loader"]
    return mr_search(q.get("q", ""), q.get("type", "mod"), mc, loader, q.get("offset", 0))


@route("GET", r"/api/modrinth/versions")
def api_mr_versions(q, b):
    vers = mr_versions(q["project"])
    return {"versions": [{"id": v["id"], "name": v.get("name"), "version_number": v.get("version_number"),
                          "game_versions": v.get("game_versions", []), "loaders": v.get("loaders", []),
                          "date": v.get("date_published", "")[:10]} for v in vers[:60]]}


@route("POST", r"/api/modpack/install")
def api_modpack_install(q, b):
    name = (b.get("name") or "Modpack").strip()
    job = run_job(f"Installing {name}", job_install_modpack, b["project_id"], b["version_id"], name,
                  int(b.get("ram_mb", 6144)), bool(b.get("eula")))
    return {"job": job.id}


@route("GET", r"/api/server/([a-z0-9-]+)/backups")
def api_backups(q, b, sid):
    return {"backups": list_backups(sid)}


@route("POST", r"/api/server/([a-z0-9-]+)/backups/create")
def api_backup_create(q, b, sid):
    return {"job": run_job("Creating backup", job_backup, sid, sid=sid).id}


@route("POST", r"/api/server/([a-z0-9-]+)/backups/restore")
def api_backup_restore(q, b, sid):
    return {"job": run_job("Restoring backup", job_restore, sid, b["file"], sid=sid).id}


@route("POST", r"/api/server/([a-z0-9-]+)/backups/delete")
def api_backup_delete(q, b, sid):
    (BACKUPS / sid / Path(b["file"]).name).unlink(missing_ok=True)
    return {"ok": True}


@route("POST", r"/api/server/([a-z0-9-]+)/open_folder")
def api_open_folder(q, b, sid):
    sub = b.get("sub") or ""
    target = sdir(sid) / sub if sub in ("", "mods", "config", "logs", "crash-reports") else sdir(sid)
    target.mkdir(exist_ok=True)
    open_path(target)
    return {"ok": True}


@route("POST", r"/api/server/([a-z0-9-]+)/delete")
def api_delete(q, b, sid):
    d = sdir(sid)
    p = PROCS.get(sid)
    if p and p.running():
        raise UserError("Stop the server before deleting it.")
    shutil.rmtree(d)
    PROCS.pop(sid, None)
    return {"ok": True}


@route("GET", r"/api/public_ip")
def api_public_ip(q, b):
    return {"ip": http_get("https://api.ipify.org", 10).decode().strip()}


class Handler(BaseHTTPRequestHandler):
    server_version = "ServerDeck"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, (UI_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        self._dispatch("GET", url)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/upload" or url.path == "/api/import":
            return self._upload(url)
        self._dispatch("POST", url)

    def _dispatch(self, method, url):
        if not self._authed():
            return self._send(403, {"error": "forbidden"})
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                body = json.loads(self.rfile.read(n) or b"{}")
        for m, rx, fn in ROUTES:
            if m == method:
                mt = rx.match(url.path)
                if mt:
                    try:
                        return self._send(200, fn(q, body, *mt.groups()))
                    except UserError as e:
                        return self._send(400, {"error": str(e)})
                    except urllib.error.URLError as e:
                        return self._send(502, {"error": f"Network error: {getattr(e, 'reason', e)}"})
                    except Exception as e:  # noqa
                        return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(404, {"error": "not found"})

    def _upload(self, url):
        if not self._authed():
            return self._send(403, {"error": "forbidden"})
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        filename = Path(urllib.parse.unquote(q.get("filename", "upload.bin"))).name
        try:
            if url.path == "/api/upload":  # single mod jar into a server
                if not filename.lower().endswith(".jar"):
                    raise UserError("Only .jar mod files can be added here.")
                dest = sdir(q["sid"]) / "mods" / filename
            else:
                dest = CACHE / "uploads" / f"{uuid.uuid4().hex}-{filename}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if url.path == "/api/upload":
                return self._send(200, {"ok": True})
            job = run_job(f"Importing {filename}", job_import_pack, dest, filename, q.get("name") or "",
                          int(q.get("ram_mb", 6144)), q.get("mc") or None, q.get("eula") == "1")
            return self._send(200, {"job": job.id})
        except UserError as e:
            return self._send(400, {"error": str(e)})


def shutdown_servers():
    live = [p for p in PROCS.values() if p.running()]
    for p in live:
        p.stop()
    deadline = time.time() + 60
    for p in live:
        while p.running() and time.time() < deadline:
            time.sleep(0.3)
        if p.running():
            kill_tree(p.proc.pid)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--browser", action="store_true", help="open in your web browser instead of a window")
    ap.add_argument("--no-open", action="store_true", help="only run the backend (for testing)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--token", default=None)
    args = ap.parse_args()
    global TOKEN
    if args.token:
        TOKEN = args.token

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"
    print(f"{APP_NAME} running at {url}")

    use_window = not args.browser and not args.no_open
    if use_window:
        try:
            import webview  # pywebview
            webview.create_window(APP_NAME, url, width=1380, height=880, min_size=(1000, 680),
                                  background_color="#0d1016")
            webview.start()
            print("Window closed - stopping servers safely...")
            shutdown_servers()
            return
        except Exception as e:  # noqa - missing pywebview / WebView2 runtime
            print(f"Desktop window unavailable ({e}) - opening in your browser instead.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping servers safely...")
        shutdown_servers()


if __name__ == "__main__":
    main()
