#!/usr/bin/env python3
"""Find version ranges that exercise Compliant Version Selection (CVS) on YOUR Curation policies.

For each candidate package it asks your curated repository for the newest versions, checks which ones Curation
allows or blocks (a tiny ranged GET, no real download), and keeps packages where the newest version in a major line
is blocked while an older version in the same line is allowed. Those ranges are where a range-based install
(`^4.0.0`, `>=2,<3`, `[2.0,3.0)`) should be served an older compliant version instead of failing.

Same environment variables as curation_test.py. Output is in packages-file format, e.g.
    python3 tools/discover_cvs.py npm --pool popular --max 300 > cvs_npm.txt

Usage: discover_cvs.py {npm|pypi|maven} [--pool popular|FILE] [--max N] [--versions N] [--workers N]
"""
import argparse
import base64
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = re.sub(r"^https?://", "", os.environ.get("JF_HOST", "")).rstrip("/")
AUTH = "Basic " + base64.b64encode(f"{os.environ.get('JF_USER', '')}:{os.environ.get('JF_TOKEN', '')}".encode()).decode()


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Artifactory SaaS may redirect downloads to a pre-signed cloud-storage URL; never send credentials there."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def fetch(url, rng=False, timeout=30):
    """Return (status, body) - body only for non-range requests."""
    hdr = {"Authorization": AUTH, "User-Agent": "curation-discover"}
    if rng:
        hdr["Range"] = "bytes=0-0"
    try:
        with _opener.open(urllib.request.Request(url, headers=hdr), timeout=timeout) as r:
            return r.status, (b"" if rng else r.read())
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def vkey(v):
    return [int(x) for x in re.findall(r"\d+", re.split(r"[-+]", v)[0])] or [0]


def stable(v):
    return not re.search(r"(?i)(alpha|beta|rc|dev|snapshot|pre|canary|next|nightly|\.a\d|\.b\d|-\d+$)", v)


# ---- version listings and artifact URLs per ecosystem ---------------------------------------------------------
def npm_versions(name, repo):
    st, body = fetch(f"https://{HOST}/artifactory/api/npm/{repo}/{urllib.parse.quote(name, safe='@')}")
    if st != 200:
        return {}
    return {v: d["dist"]["tarball"] for v, d in json.loads(body).get("versions", {}).items() if "dist" in d}


def pypi_versions(name, repo):
    base = f"https://{HOST}/artifactory/api/pypi/{repo}/simple/{urllib.parse.quote(name)}/"
    st, body = fetch(base)
    if st != 200:
        return {}
    pat = re.compile(r"(?i)^" + re.sub(r"[-_.]+", "[-_.]+", re.escape(name)).replace(r"\[-_\.\]\+", "[-_.]+")
                     + r"-(\d[^-]*?)(?:-.*\.whl|\.tar\.gz|\.zip)$")
    best = {}
    for href in re.findall(r'href="([^"]+)"', body.decode("utf-8", "replace")):
        url = urllib.parse.urljoin(base, href.split("#")[0])
        m = pat.match(url.rsplit("/", 1)[-1])
        if not m:
            continue
        rank = 0 if url.endswith("py3-none-any.whl") else (1 if url.endswith(".whl") else 2)
        v = m.group(1)
        if v not in best or rank < best[v][0]:
            best[v] = (rank, url)
    return {v: u for v, (r, u) in best.items()}


def maven_versions(coord, repo):
    g, _, a = coord.partition(":")
    base = f"https://{HOST}/artifactory/{repo}/{g.replace('.', '/')}/{a}"
    st, body = fetch(base + "/maven-metadata.xml")
    if st != 200:
        return {}
    root = ET.fromstring(body)
    return {v.text: f"{base}/{v.text}/{a}-{v.text}.jar" for v in root.findall("./versioning/versions/version")}


LISTERS = {"npm": ("NPM_REPO", npm_versions), "pypi": ("PYPI_REPO", pypi_versions), "maven": ("MAVEN_REPO", maven_versions)}


LATEST = {}  # npm: newest published version per package (from the search API)


# ---- candidate pools -------------------------------------------------------------------------------------------
def curl_json(url):
    out = subprocess.run(["curl", "-sL", "--max-time", "60", url], capture_output=True, text=True).stdout
    return json.loads(out)


def popular_pool(eco, n):
    if eco == "pypi":
        rows = curl_json("https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json")["rows"]
        return [r["project"] for r in rows[:n]]
    if eco == "npm":
        names = []
        for kw in ("javascript", "typescript", "node", "react", "cli", "framework", "http", "server", "database",
                   "parser", "build", "test", "security", "utility", "stream", "template", "validation", "logging"):
            data = curl_json(f"https://registry.npmjs.org/-/v1/search?text=keywords:{kw}&size=250&popularity=1.0"
                             "&quality=0&maintenance=0")
            for o in data.get("objects", []):
                if not o["package"]["name"].startswith("@types/"):
                    LATEST[o["package"]["name"]] = o["package"]["version"]
                    names.append(o["package"]["name"])
        return list(dict.fromkeys(names))[:n]
    return [l.split("#")[0].strip() for l in (HERE / "maven_pool.txt").read_text().splitlines() if l.split("#")[0].strip()][:n]


def public_latest(eco, name):
    """Newest stable public version (read-only metadata lookup; uses curl so system CA certificates apply)."""
    try:
        if eco == "npm":
            return LATEST.get(name, "")
        if eco == "pypi":
            return curl_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")["info"]["version"]
        g, _, a = name.partition(":")
        out = subprocess.run(["curl", "-sL", "--max-time", "30",
                              f"https://repo1.maven.org/maven2/{g.replace('.', '/')}/{a}/maven-metadata.xml"],
                             capture_output=True, text=True).stdout
        vs = [v.text for v in ET.fromstring(out).findall("./versioning/versions/version") if stable(v.text)]
        return max(vs, key=vkey) if vs else ""
    except Exception:
        return ""


# ---- analysis --------------------------------------------------------------------------------------------------
def spec_for(eco, major, line0=None):
    if eco == "npm":
        return f"^{major}.0.0" if major else f"~0.{line0}.0"
    return f">={major},<{major + 1}" if eco == "pypi" else f"[{major}.0,{major + 1}.0)"


def analyse(eco, name, repo, nvers):
    vers = LISTERS[eco][1](name, repo)
    vs = sorted((v for v in vers if stable(v)), key=vkey)[-nvers:]
    if len(vs) < 3:
        return []
    status = {v: fetch(vers[v], rng=True)[0] for v in vs}
    found = []
    # (a) the newest PUBLIC release is not offered / is blocked on your tenant, but older ones in its major line are
    latest = public_latest(eco, name)
    if latest and stable(latest) and vkey(latest)[0] >= 1:
        major = vkey(latest)[0]
        line_ok = [v for v in vs if vkey(v)[0] == major and status[v] in (200, 206)]
        if line_ok and latest not in vers:
            found.append((name, spec_for(eco, major), f"CVS candidate: newest public {latest} is not offered by Curation (too new / blocked); "
                          f"newest allowed {line_ok[-1]}"))
        elif line_ok and status.get(latest) == 403:
            found.append((name, spec_for(eco, major), f"CVS candidate: newest public {latest} blocked, newest allowed {line_ok[-1]}"))
        if found:
            return found
    for major in sorted({vkey(v)[0] for v in vs}):
        line = [v for v in vs if vkey(v)[0] == major]
        if len(line) < 2 or status[line[-1]] != 403:
            continue
        ok = [v for v in line if status[v] in (200, 206)]
        if not ok:
            continue
        best = ok[-1]
        blocked_above = [v for v in line if vkey(v) > vkey(best) and status[v] == 403]
        spec = spec_for(eco, major, vkey(line[0])[1] if len(vkey(line[0])) > 1 else 0)
        found.append((name, spec, f"CVS candidate: newest {line[-1]} blocked, {best} allowed ({len(blocked_above)} newer blocked)"))
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ecosystem", choices=list(LISTERS))
    ap.add_argument("--pool", default="popular", help="'popular' or a file with one package per line")
    ap.add_argument("--max", type=int, default=300, help="how many candidate packages to examine")
    ap.add_argument("--versions", type=int, default=30, help="newest N stable versions to probe per package")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help="stop after finding this many candidate ranges")
    args = ap.parse_args()
    if not (HOST and os.environ.get("JF_USER") and os.environ.get("JF_TOKEN")):
        sys.exit("Set JF_HOST, JF_USER and JF_TOKEN (and the repo variable for this ecosystem).")
    repo = os.environ.get(LISTERS[args.ecosystem][0])
    if not repo:
        sys.exit(f"Set {LISTERS[args.ecosystem][0]}")
    pool = ([l.split("#")[0].strip() for l in Path(args.pool).read_text().splitlines() if l.split("#")[0].strip()]
            if args.pool != "popular" else popular_pool(args.ecosystem, args.max))[: args.max]
    print(f"# {len(pool)} candidates, probing newest {args.versions} versions each via {HOST}/{repo}", file=sys.stderr)
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(analyse, args.ecosystem, n, repo, args.versions): n for n in pool}
        for i, f in enumerate(cf.as_completed(futs), 1):
            try:
                hits = f.result()
            except Exception as e:
                print(f"# {futs[f]}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            results += hits
            for h in hits:
                print(f"{h[0]} {h[1]}  # {h[2]}", flush=True)
            if i % 25 == 0:
                print(f"# ... {i}/{len(pool)} examined, {len(results)} candidates", file=sys.stderr)
            if args.limit and len(results) >= args.limit:
                for p in futs:
                    p.cancel()
                break
    print(f"# done: {len(results)} CVS candidate ranges from {len(pool)} packages", file=sys.stderr)


if __name__ == "__main__":
    main()
