#!/usr/bin/env python3
"""JFrog Curation traffic test for npm, PyPI and Maven.

Pulls every package listed in packages/<ecosystem>.txt through your Curation-enabled
Artifactory repositories using the real package managers, and logs what happened.

Required environment variables:
  JF_HOST      e.g. acme.jfrog.io
  JF_USER      Artifactory user (email is fine)
  JF_TOKEN     access token (or password) with read access to the repos below
  NPM_REPO / PYPI_REPO / MAVEN_REPO   names of the curated virtual (or remote) repos

Usage:
  python3 curation_test.py [npm] [pypi] [maven] [--workers N] [--timeout SEC] [--limit N]
                           [--no-upstream-check]
Only python3 (stdlib) plus npm and pip (for those ecosystems) are needed; Maven needs no client.
"""
import argparse
import csv
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ECOSYSTEMS = {
    # ecosystem: (repo env var, client executable)
    "npm": ("NPM_REPO", "npm"),
    "pypi": ("PYPI_REPO", sys.executable),  # run pip as "python -m pip"
    "maven": ("MAVEN_REPO", None),  # spoken to over HTTP, no client needed
}
FIELDS = ["timestamp", "ecosystem", "package", "spec", "outcome", "resolved_version",
          "newest_public_in_range", "older_than_newest", "blocked_package", "policy", "detail", "seconds", "note"]
print_lock = threading.Lock()


# ---------------------------------------------------------------- config
def load_config():
    missing = [v for v in ("JF_HOST", "JF_USER", "JF_TOKEN") if not os.environ.get(v)]
    if missing:
        sys.exit("Missing environment variables: " + ", ".join(missing) + "\nSee the header of this file or README.md.")
    host = re.sub(r"^https?://", "", os.environ["JF_HOST"]).rstrip("/")
    return {"host": host, "user": os.environ["JF_USER"], "token": os.environ["JF_TOKEN"]}


def scrub(text, cfg):
    text = re.sub(r"\S*/curation-test-[^/\s]*/\S*", "<tmp>", text)
    for secret in {cfg["token"], urllib.parse.quote(cfg["token"], safe="")}:
        if secret:
            text = text.replace(secret, "****")
    return text


def read_packages(eco):
    path = HERE / "packages" / f"{eco}.txt"
    rows = []
    for line in path.read_text().splitlines():
        body, _, note = line.partition("#")
        body = body.strip()
        if not body:
            continue
        name, _, spec = body.partition(" ")
        rows.append((name.strip(), spec.strip(), note.strip()))
    return rows


def run(cmd, cwd, env, timeout):
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, str(out) + f"\nTIMEOUT after {timeout}s"
    except FileNotFoundError as e:
        return 127, str(e)


def classify(rc, output):
    """Map a client run to OK / BLOCKED / NO_MATCH / ERROR."""
    if rc == 0:
        return "OK"
    low = output.lower()
    if rc == 124:
        return "ERROR"
    if re.search(r"\b403\b|forbidden|blocked by jfrog packages curation", low):
        return "BLOCKED"
    if re.search(r"etarget|no version of|no matching version|no matching distribution|could not find a version|"
                 r"could not find artifact|no versions available|\b404\b|not found|failed to resolve version|"
                 r"no version.* found", low):
        return "NO_MATCH"
    return "ERROR"


BLOCK_RE = re.compile(r"package (\S+?) download was blocked by jfrog packages curation service due to the following "
                      r"policies violated \{([^}]*)\}", re.I)


def block_info(output):
    """(blocked package, policy name, full reason) from the message Curation returns, if present."""
    m = BLOCK_RE.search(output.replace('\\"', '"'))
    if not m:
        return "", "", ""
    reason = m.group(2)
    return m.group(1), reason.split(",")[0].strip(), reason


def detail_of(output, cfg, limit=400):
    """Short explanation: Curation's own message when the client shows it, else the first error lines."""
    pkg, policy, reason = block_info(output)
    if pkg:
        return scrub(re.sub(r"\s+", " ", f"{pkg} blocked by policy {policy}: {reason}")[:limit], cfg)
    keep = [l.strip() for l in output.splitlines()
            if re.search(r"403|forbidden|blocked|policy|etarget|no matching|could not find|error|404", l, re.I)
            and "complete log of this run" not in l]
    if not keep:
        keep = [l.strip() for l in output.splitlines() if l.strip()][-2:]
    return scrub(re.sub(r"\s+", " ", " | ".join(keep))[:limit], cfg)


# ---------------------------------------------------------------- npm
def pull_npm(name, spec, cfg, repo, timeout, tmp):
    reg = f"https://{cfg['host']}/artifactory/api/npm/{repo}/"
    rc_file = Path(tmp, "npmrc")
    rc_file.write_text(f"registry={reg}\n//{cfg['host']}/artifactory/api/npm/{repo}/:_authToken={cfg['token']}\n"
                       "audit=false\nfund=false\nupdate-notifier=false\n")
    work = Path(tmp, "work")
    work.mkdir()
    env = dict(os.environ, npm_config_userconfig=str(rc_file), npm_config_globalconfig=str(Path(tmp, "global-npmrc")),
               npm_config_cache=str(Path(tmp, "cache")))
    target = f"{name}@{spec}" if spec else name
    rc, out = run(["npm", "install", target, "--prefix", str(work), "--no-audit", "--no-fund", "--ignore-scripts",
                   "--no-package-lock"], str(work), env, timeout)
    version = ""
    pkg_json = work / "node_modules" / name / "package.json"
    if pkg_json.exists():
        version = json.loads(pkg_json.read_text()).get("version", "")
    return rc, out, version


# ---------------------------------------------------------------- pypi
def pull_pypi(name, spec, cfg, repo, timeout, tmp):
    user, tok = (urllib.parse.quote(cfg["user"], safe=""), urllib.parse.quote(cfg["token"], safe=""))
    index = f"https://{user}:{tok}@{cfg['host']}/artifactory/api/pypi/{repo}/simple"
    dest = Path(tmp, "dl")
    dest.mkdir()
    env = dict(os.environ, PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1")
    rc, out = run([sys.executable, "-m", "pip", "download", f"{name}{spec}", "--no-deps", "--no-cache-dir",
                   "--dest", str(dest), "-i", index, "--progress-bar", "off"], tmp, env, timeout)
    version = ""
    for f in dest.iterdir():
        m = re.match(r"^[^-]+-([^-]+?)(?:-|\.tar\.gz|\.zip)", f.name)
        if m:
            version = m.group(1)
    if rc != 0 and re.search(r"HTTP error 403 while getting (\S+)", out):
        # pip hides Curation's explanation; fetch the same URL to read it
        url = re.search(r"HTTP error 403 while getting (\S+)", out).group(1).split("#")[0]
        try:
            repo_get(url, cfg, 20)
        except HttpStatus as e:
            out += "\n" + e.body.replace("\\n", " ")
        except Exception:
            pass
    elif rc != 0 and "subprocess-exited-with-error" in out and "403" not in out:
        # the package downloaded fine; pip then failed to build old source metadata locally (not a Curation result)
        m = re.search(r"Downloading (?:\S*/)?[^/\s]+?-(\d[^/\s]*?)\.(?:tar\.gz|zip)", out)
        version = m.group(1) if m else version
        out += "\n[local build skipped] package downloaded; pip could not build old source metadata on this machine"
        rc = 0
    return rc, out, version


# ---------------------------------------------------------------- maven
# Maven is exercised at the repository protocol level (metadata -> version-range resolution -> .pom -> .jar),
# the same requests `mvn` makes. This avoids running the Maven plugin tree through Curation, which would be
# blocked/slowed by the plugins' own dependencies and add noise to the results.
_QUAL = {"alpha": 1, "a": 1, "beta": 2, "b": 2, "milestone": 3, "m": 3, "rc": 4, "cr": 4,
         "snapshot": 5, "": 6, "ga": 6, "final": 6, "release": 6, "sp": 7}


def _vkey(v):
    parts = []
    for tok in re.split(r"[.\-_]", v.lower()):
        for t in re.findall(r"\d+|[a-z]+", tok) or [""]:
            parts.append((1, int(t), "") if t.isdigit() else (0, _QUAL.get(t, 0), t))
    while parts and parts[-1][:2] in ((1, 0), (0, 6)):  # trailing 0 / ga are insignificant
        parts.pop()
    return parts


def in_range(v, spec):
    """True if version v satisfies Maven range spec such as [1.0,2.0) (exact or soft versions handled by caller)."""
    for m in re.finditer(r"([\[(])\s*([^,\])]*)\s*(?:(,)\s*([^\])]*))?\s*([\])])", spec):
        lo_inc, lo, comma, hi, hi_inc = m.group(1) == "[", m.group(2).strip(), m.group(3), (m.group(4) or "").strip(), m.group(5) == "]"
        if not comma:  # [1.0] = exactly 1.0
            if _vkey(v) == _vkey(lo):
                return True
            continue
        ok = True
        if lo:
            ok &= _vkey(v) >= _vkey(lo) if lo_inc else _vkey(v) > _vkey(lo)
        if hi:
            ok &= _vkey(v) <= _vkey(hi) if hi_inc else _vkey(v) < _vkey(hi)
        if ok:
            return True
    return False


class HttpStatus(Exception):
    def __init__(self, code, body=""):
        super().__init__(f"HTTP {code}")
        self.code, self.body = code, body


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Artifactory SaaS may redirect downloads to a pre-signed cloud-storage URL; never send credentials there."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def repo_get(url, cfg, timeout):
    import base64
    tok = base64.b64encode(f"{cfg['user']}:{cfg['token']}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + tok, "User-Agent": "curation-test"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise HttpStatus(e.code, e.read(3000).decode("utf-8", "replace"))


def pull_maven(name, spec, cfg, repo, timeout, log):
    """Returns (rc, output, resolved_version); rc 0 = downloaded."""
    group, _, artifact = name.partition(":")
    base = f"https://{cfg['host']}/artifactory/{repo}/{group.replace('.', '/')}/{artifact}"
    try:
        if spec and not re.match(r"^[\[(]", spec):
            version = spec  # exact / soft requirement
            log.append(f"requested {version}")
        else:
            meta = ET.fromstring(repo_get(base + "/maven-metadata.xml", cfg, timeout))
            versions = [v.text for v in meta.findall("./versioning/versions/version")]
            log.append(f"maven-metadata.xml lists {len(versions)} versions (newest listed: "
                       f"{max(versions, key=_vkey) if versions else '-'})")
            pool = [v for v in versions if not spec or in_range(v, spec)]
            pool = [v for v in pool if "snapshot" not in v.lower()] or pool
            if not pool:
                return 1, "\n".join(log + [f"No version of {name} matches {spec or 'any'} in the metadata served by {repo}"]), ""
            version = max(pool, key=_vkey)
            log.append(f"range {spec or '(any)'} resolves to {version}")
        pom = repo_get(f"{base}/{version}/{artifact}-{version}.pom", cfg, timeout)
        log.append(f"GET {artifact}-{version}.pom -> 200")
        packaging = (ET.fromstring(pom).findtext("{*}packaging") or "jar").strip()
        if packaging != "pom":
            try:
                repo_get(f"{base}/{version}/{artifact}-{version}.jar", cfg, timeout)
                log.append(f"GET {artifact}-{version}.jar -> 200")
            except HttpStatus as e:
                if e.code != 404 or packaging == "jar":
                    raise
                log.append(f"no .jar for packaging '{packaging}'")
        return 0, "\n".join(log), version
    except HttpStatus as e:
        return 1, "\n".join(log + [f"HTTP {e.code} {e.body}"]), ""
    except Exception as e:  # network / parse problems
        return 2, "\n".join(log + [f"{type(e).__name__}: {e}"]), ""


# ---------------------------------------------------------------- public-registry comparison
def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "curation-test"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def newest_public(eco, name, spec, timeout=30):
    """Newest version of `name` matching `spec` on the PUBLIC registry (direct internet, read-only metadata lookup).
    If Curation served an older version than this, Compliant Version Selection (or a block-and-fallback) happened.
    Returns "" when the public registry is unreachable or nothing matches."""
    try:
        if eco == "npm":
            with tempfile.TemporaryDirectory(prefix="npmview-") as d:
                Path(d, "user").write_text("")
                Path(d, "global").write_text("")
                env = dict(os.environ, npm_config_userconfig=str(Path(d, "user")), npm_config_globalconfig=str(Path(d, "global")))
                rc, out = run(["npm", "view", f"{name}@{spec}" if spec else name, "version", "--json",
                               "--registry=https://registry.npmjs.org/", "--loglevel=silent"], d, env, timeout)
            vals = json.loads(out.strip()) if rc == 0 and out.strip() else None
            vals = vals if isinstance(vals, list) else [vals]
            stable = [v for v in vals if v and "-" not in v] or [v for v in vals if v]
            return max(stable, key=lambda v: [int(x) for x in re.findall(r"\d+", v.split("+")[0])]) if stable else ""
        if eco == "pypi":
            from pip._vendor.packaging.specifiers import SpecifierSet
            from pip._vendor.packaging.version import Version
            # pip does the lookup (it already trusts the right CA certificates on this machine)
            env = dict(os.environ, PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1")
            rc, out = run([sys.executable, "-m", "pip", "index", "versions", name, "-i", "https://pypi.org/simple"],
                          tempfile.gettempdir(), env, timeout)
            m = re.search(r"Available versions:\s*(.+)", out)
            live = [v.strip() for v in m.group(1).split(",")] if m else []
            ok = list(SpecifierSet(spec or "").filter(live))
            return str(max(ok, key=Version)) if ok else ""
        g, _, a = name.partition(":")
        meta = ET.fromstring(http_get(f"https://repo1.maven.org/maven2/{g.replace('.', '/')}/{a}/maven-metadata.xml"))
        vs = [v.text for v in meta.findall("./versioning/versions/version")]
        if spec and re.match(r"^[\[(]", spec):
            vs = [v for v in vs if in_range(v, spec)]
        elif spec:
            return spec
        vs = [v for v in vs if "snapshot" not in v.lower()]
        return max(vs, key=_vkey) if vs else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- driver
def test_one(eco, name, spec, note, cfg, repo, args, base, rawdir):
    start = time.time()
    with tempfile.TemporaryDirectory(prefix="cur-", dir=base) as tmp:
        if eco == "npm":
            rc, out, ver = pull_npm(name, spec, cfg, repo, args.timeout, tmp)
        elif eco == "pypi":
            rc, out, ver = pull_pypi(name, spec, cfg, repo, args.timeout, tmp)
        else:
            log = []
            rc, out, ver = pull_maven(name, spec, cfg, repo, args.timeout, log)
    outcome = classify(rc, out)
    latest = ""
    if args.upstream_check and outcome == "OK":
        latest = newest_public(eco, name, spec, 60) or newest_public(eco, name, spec, 90)  # one retry for a flaky lookup
    bpkg, bpolicy, _ = block_info(out)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{name}@{spec}")
    (rawdir / f"{safe}.log").write_text(f"$ {eco} {name} {spec}\nexit={rc} outcome={outcome}\n\n" + scrub(out, cfg))
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"), "ecosystem": eco, "package": name, "spec": spec,
        "outcome": outcome, "resolved_version": ver, "newest_public_in_range": latest,
        "older_than_newest": ("yes" if ver and latest and ver != latest else "no") if latest else "",
        "blocked_package": bpkg, "policy": bpolicy,
        "detail": detail_of(out, cfg) if outcome != "OK" else ("local build skipped (package downloaded)" if "[local build skipped]" in out else ""), "seconds": round(time.time() - start, 1), "note": note,
    }
    with print_lock:
        print(f"[{eco:5}] {outcome:9} {name} {spec} -> {ver or '-'}", flush=True)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ecosystems", nargs="*", metavar="ecosystem", help="npm, pypi and/or maven (default: all)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=300, help="per-package timeout in seconds")
    ap.add_argument("--limit", type=int, default=0, help="only run the first N packages of each list")
    ap.add_argument("--no-upstream-check", dest="upstream_check", action="store_false",
                    help="skip the read-only lookup of the newest public version on npmjs.org / pypi.org / Maven Central "
                         "(that lookup is how version substitution by Curation is detected)")
    args = ap.parse_args()
    bad = [e for e in args.ecosystems if e not in ECOSYSTEMS]
    if bad:
        ap.error(f"unknown ecosystem(s): {', '.join(bad)} (choose from {', '.join(ECOSYSTEMS)})")
    cfg = load_config()

    wanted = args.ecosystems or list(ECOSYSTEMS)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = HERE / "results" / stamp
    outdir.mkdir(parents=True)
    base = tempfile.mkdtemp(prefix="curation-test-")
    rows = []
    try:
        for eco in wanted:
            env_var, client = ECOSYSTEMS[eco]
            repo = os.environ.get(env_var)
            exe = True if client is None else (client if eco == "pypi" else shutil.which(client))
            if not repo:
                print(f"Skipping {eco}: {env_var} is not set.")
                continue
            if not exe:
                print(f"Skipping {eco}: '{client}' was not found on PATH.")
                continue
            pkgs = read_packages(eco)[: args.limit or None]
            rawdir = outdir / "raw" / eco
            rawdir.mkdir(parents=True)
            print(f"\n=== {eco}: {len(pkgs)} packages via {cfg['host']}/{repo} ===")
            with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futs = [pool.submit(test_one, eco, n, s, note, cfg, repo, args, base, rawdir) for n, s, note in pkgs]
                rows += [f.result() for f in futs]
    finally:
        shutil.rmtree(base, ignore_errors=True)

    with open(outdir / "results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        w.writerows(rows)
    with open(outdir / "results.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    write_summary(rows, outdir)
    print(f"\nResults written to {outdir}\n")
    print((outdir / "summary.txt").read_text())


def write_summary(rows, outdir):
    lines = [f"Curation test run {datetime.now().isoformat(timespec='seconds')}", ""]
    for eco in ECOSYSTEMS:
        sub = [r for r in rows if r["ecosystem"] == eco]
        if not sub:
            continue
        counts = {}
        for r in sub:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        lines.append(f"{eco}: {len(sub)} packages  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        for r in sub:
            if r["outcome"] == "BLOCKED":
                why = f"  [{r['blocked_package']}: {r['policy']}]" if r["policy"] else ""
                lines.append(f"    BLOCKED  {r['package']} {r['spec']}{why}")
        for r in sub:
            if r["older_than_newest"] == "yes":
                lines.append(f"    SUBSTITUTED  {r['package']} {r['spec']} -> {r['resolved_version']} (newest public in range: {r['newest_public_in_range']})")
        lines.append("")
    lines.append("Check Curation > Audit Events in the JFrog platform for the policy behind each block / version substitution.")
    (outdir / "summary.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
