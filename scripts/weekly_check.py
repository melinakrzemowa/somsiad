#!/usr/bin/env python3
"""Weekly health check for somsiad.

Collects status from Docker, Prometheus, Tempo, Alertmanager, and
node_exporter; renders an HTML + plain-text email and sends it via the
same SMTPS account Alertmanager uses.

Runs from launchd (~/Library/LaunchAgents/com.melinakrzemowa.somsiad-weekly.plist),
invoked through scripts/weekly_check.sh.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html import escape

# --- config -----------------------------------------------------------------

DEPLOY_DIR = "/Users/kelu/services/monitoring.melinakrzemowa.pl"
SMTP_HOST = "h18.seohost.pl"
SMTP_PORT = 465
SMTP_USER = "alert@melinakrzemowa.pl"
SMTP_PASSWORD_FILE = f"{DEPLOY_DIR}/alertmanager/smtp_password"
MAIL_FROM_NAME = "somsiad"
MAIL_FROM_ADDR = "alert@melinakrzemowa.pl"
MAIL_TO = "kelostrada@gmail.com"
PROM_CONTAINER = "monitoringmelinakrzemowapl-prometheus-1"
ALLOY_NETWORK = "monitoringmelinakrzemowapl_default"
ENV_FILE = f"{DEPLOY_DIR}/.env"

# Grafana panels to render into the email. Each panel is fetched as a PNG
# from Grafana's image renderer and attached inline.
WEEKLY_DASHBOARD_UID = "somsiad-weekly"

# Default chart window. The "weekly" cadence wants a 7-day view; if we
# don't have a week of data yet, Grafana's panel can render its
# "No data" placeholder (sparse-result heuristic). By the first real
# Monday run we should be past that.
CHART_FROM = "now-7d"

RENDER_PANELS = [
    {"id": 1, "title": "Service uptime", "height": 280},
    {"id": 2, "title": "Phoenix request rate", "height": 320},
    {"id": 3, "title": "Phoenix p95 latency", "height": 280},
    {"id": 4, "title": "Container memory", "height": 280},
]

# Public URLs to probe.
PROBES = [
    ("instagrain", "https://insta.melinakrzemowa.pl"),
    ("sribia", "https://sribia.melinakrzemowa.pl/auth"),
    ("mooncraft", "https://mooncraft.melinakrzemowa.pl"),
    ("abyss", "https://abyss.melinakrzemowa.pl"),
    ("monitoring", "https://monitoring.melinakrzemowa.pl"),
]
PHOENIX_SERVICES = ["instagrain", "sribia"]
STATIC_SERVICES = ["mooncraft", "abyss"]

# All container name prefixes the report knows about. Used to order rows and
# to tell "expected" containers from anything new that shows up.
KNOWN_PREFIXES = (
    "monitoringmelinakrzemowapl-",
    "instamelinakrzemowapl-",
    "sribiamelinakrzemowapl-",
    "mooncraft",
    "abyss",
)

# --- subprocess helpers -----------------------------------------------------

def sh(*args: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
        return r.stdout
    except Exception:
        return ""


def mon_curl(url: str, timeout: int = 8) -> str:
    """Curl an internal monitoring URL via the prometheus container's wget."""
    return sh(
        "docker", "exec", PROM_CONTAINER,
        "wget", "-qO-", "-T", str(timeout), url,
        timeout=timeout + 4,
    )


def mon_curl_bytes(url: str, headers: list[tuple[str, str]] | None = None,
                   timeout: int = 60) -> bytes:
    """Fetch a URL inside the monitoring docker network as raw bytes.

    Uses a throwaway curlimages/curl container so we get binary-safe stdout
    (subprocess with text=False) and proper HTTP semantics.
    """
    args = ["docker", "run", "--rm", "--network", ALLOY_NETWORK,
            "curlimages/curl:8.10.1",
            "-s", "--max-time", str(timeout), "--fail-with-body"]
    for k, v in headers or []:
        args.extend(["-H", f"{k}: {v}"])
    args.append(url)
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout + 10, check=False)
        if r.returncode != 0:
            sys.stderr.write(
                f"mon_curl_bytes: curl exit {r.returncode} for {url}\n"
                f"stderr: {r.stderr.decode('utf-8', 'replace')[:400]}\n"
            )
            return b""
        return r.stdout
    except Exception as e:
        sys.stderr.write(f"mon_curl_bytes: {e}\n")
        return b""


def read_env_file(path: str) -> dict[str, str]:
    """Tiny .env parser. Strips surrounding quotes; ignores comments."""
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def render_panel(uid: str, panel_id: int, width: int = 1100, height: int = 280,
                 time_from: str = CHART_FROM, time_to: str = "now") -> bytes:
    """Snapshot a single Grafana panel as a PNG.

    width=1100 produces a crisp image at the email's display width (600px)
    on retina screens. The renderer scales internally to whatever it needs.
    """
    env = read_env_file(ENV_FILE)
    user = env.get("GF_SECURITY_ADMIN_USER", "admin")
    password = env.get("GF_SECURITY_ADMIN_PASSWORD", "")
    if not password:
        sys.stderr.write("render_panel: no GF_SECURITY_ADMIN_PASSWORD in .env\n")
        return b""

    # Basic auth header. The renderer is stateless and only available on the
    # internal docker network, so basic auth here is fine.
    import base64
    creds = base64.b64encode(f"{user}:{password}".encode()).decode()

    url = (
        f"http://grafana:3000/render/d-solo/{uid}/?"
        f"panelId={panel_id}&from={time_from}&to={time_to}"
        f"&width={width}&height={height}&tz=Europe%2FWarsaw"
    )
    return mon_curl_bytes(url, headers=[("Authorization", f"Basic {creds}")])


def query_prom(query: str) -> dict:
    body = mon_curl(f"http://prometheus:9090/api/v1/query?query={query}")
    try:
        return json.loads(body)
    except Exception:
        return {"data": {"result": []}}


def query_prom_range(query: str, start: int, end: int, step: int) -> dict:
    body = mon_curl(
        f"http://prometheus:9090/api/v1/query_range?query={query}"
        f"&start={start}&end={end}&step={step}"
    )
    try:
        return json.loads(body)
    except Exception:
        return {"data": {"result": []}}

# --- collectors -------------------------------------------------------------

def collect_containers() -> list[dict]:
    out = sh("docker", "ps", "--format",
             "{{.Names}}|{{.Status}}|{{.Image}}|{{.RunningFor}}")
    rows = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name, status, image, rf = parts[0], parts[1], parts[2], parts[3]
        # restart count
        rc_raw = sh("docker", "inspect", name, "--format", "{{.RestartCount}}").strip()
        try:
            rc = int(rc_raw)
        except ValueError:
            rc = 0
        rows.append({
            "name": name, "status": status, "image": image,
            "running_for": rf, "restart_count": rc,
            "ok": status.startswith("Up"),
        })
    # Stable sort: known prefixes first (in declaration order), then alpha.
    def sort_key(r):
        for i, p in enumerate(KNOWN_PREFIXES):
            if r["name"].startswith(p):
                return (i, r["name"])
        return (len(KNOWN_PREFIXES), r["name"])
    rows.sort(key=sort_key)
    return rows


def collect_probes() -> list[dict]:
    rows = []
    for name, url in PROBES:
        code = sh("curl", "-sLo", "/dev/null", "-w", "%{http_code}",
                  "--max-time", "8", url, timeout=12).strip() or "000"
        rows.append({
            "name": name, "url": url, "code": code,
            "ok": code.startswith("2") or code.startswith("3"),
        })
    return rows


def collect_traces() -> list[dict]:
    end = int(time.time())
    start = end - 24 * 3600
    rows = []
    for svc in PHOENIX_SERVICES:
        body = mon_curl(
            f"http://tempo:3200/api/search?tags=service.name%3D{svc}&limit=1"
            f"&start={start}&end={end}"
        )
        try:
            traces = json.loads(body).get("traces", [])
        except Exception:
            traces = []
        if traces:
            ns = int(traces[0].get("startTimeUnixNano", "0"))
            age_s = end - ns // 10**9 if ns else None
            rows.append({"service": svc, "ok": True, "age_s": age_s})
        else:
            rows.append({"service": svc, "ok": False, "age_s": None})
    for svc in STATIC_SERVICES:
        rows.append({"service": svc, "ok": None,
                     "note": "static site — no traces expected"})
    return rows


def collect_alerts_past_week() -> list[dict]:
    """Group fire intervals of each (alertname, target/job) seen in past 7d."""
    end = int(time.time())
    start = end - 7 * 86400
    step = 300
    data = query_prom_range('ALERTS{alertstate="firing"}', start, end, step)
    by_key: dict[tuple[str, str], list[int]] = {}
    for r in data.get("data", {}).get("result", []):
        m = r.get("metric", {})
        target = m.get("target") or m.get("job") or "?"
        key = (m.get("alertname", "?"), target)
        ts = [int(float(t)) for t, _ in r.get("values", [])]
        by_key.setdefault(key, []).extend(ts)
    rows = []
    for (alertname, target), tses in sorted(by_key.items()):
        tses.sort()
        # Summary: first seen, last seen, total eval points, severity guess
        rows.append({
            "alertname": alertname,
            "target": target,
            "first": tses[0] if tses else None,
            "last": tses[-1] if tses else None,
            "points": len(tses),
        })
    return rows


def collect_silences() -> list[dict]:
    body = mon_curl("http://alertmanager:9093/api/v2/silences?silenced=false")
    try:
        all_s = json.loads(body)
    except Exception:
        all_s = []
    rows = []
    for s in all_s:
        if (s.get("status") or {}).get("state") != "active":
            continue
        matchers = ", ".join(
            f"{m['name']}{'=~' if m.get('isRegex') else '='}{m['value']}"
            for m in s.get("matchers", [])
        )
        rows.append({
            "matchers": matchers,
            "comment": s.get("comment", ""),
            "ends_at": s.get("endsAt", ""),
        })
    return rows


_MEM_RE = re.compile(r"\s*([\d.]+)\s*([KMGTP]?i?B)\b", re.IGNORECASE)


def _parse_mem_string(s: str) -> float:
    """Parse '203.7MiB' / '5.772GiB' / '1.0KiB' -> MiB."""
    if not s:
        return 0.0
    m = _MEM_RE.search(s)
    if not m:
        return 0.0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0.0
    unit = m.group(2).upper().replace("I", "")  # KiB -> KB style
    mult = {
        "B": 1 / (1024 * 1024),
        "KB": 1 / 1024,
        "MB": 1,
        "GB": 1024,
        "TB": 1024 * 1024,
        "PB": 1024 * 1024 * 1024,
    }.get(unit, 1)
    return n * mult


def collect_host_resources() -> dict:
    # ---- macOS host memory (from vm_stat) ----
    # Apple page size is 16384 on Apple Silicon.
    vm = sh("vm_stat")
    pages: dict[str, int] = {}
    page_size = 16384
    for line in vm.splitlines():
        if "page size of" in line:
            # "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
            try:
                page_size = int(line.split("page size of")[1].split()[0])
            except Exception:
                pass
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().rstrip(".")
        try:
            pages[k.strip()] = int(v)
        except ValueError:
            pass
    free = pages.get("Pages free", 0)
    active = pages.get("Pages active", 0)
    inactive = pages.get("Pages inactive", 0)
    wired = pages.get("Pages wired down", 0)
    comp = pages.get("Pages occupied by compressor", 0)
    bytes_per_page = page_size
    host_used_mib = (active + wired + comp) * bytes_per_page / (1024 * 1024)
    host_total_mib = (free + active + inactive + wired + comp) * bytes_per_page / (1024 * 1024)
    host_mem_pct = round(host_used_mib * 100 / host_total_mib) if host_total_mib else None

    # ---- Disk ----
    disk = sh("df", "-h", "/").splitlines()
    disk_row = {}
    if len(disk) >= 2:
        cols = disk[1].split()
        if len(cols) >= 5:
            disk_row = {"used": cols[2], "total": cols[1], "pct": cols[4]}

    # ---- Per-container CPU/RAM (sorted by mem desc) ----
    out = sh("docker", "stats", "--no-stream",
             "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}")
    containers = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 3:
            containers.append({
                "name": parts[0],
                "cpu": parts[1],
                "mem": parts[2],
                "mem_pct": parts[3] if len(parts) > 3 else "",
            })

    def mem_used_mib(c) -> float:
        return _parse_mem_string(c["mem"].split("/")[0])

    def mem_limit_mib(c) -> float:
        parts = c["mem"].split("/")
        return _parse_mem_string(parts[1]) if len(parts) > 1 else 0.0

    containers.sort(key=mem_used_mib, reverse=True)

    # ---- Docker VM memory (Colima VM) ----
    # Sum of containers + reported limit. Colima VM total comes from any
    # container's MemUsage limit field (all containers see the same VM).
    vm_total_mib = max((mem_limit_mib(c) for c in containers), default=0.0)
    vm_used_mib = sum(mem_used_mib(c) for c in containers)
    vm_pct = round(vm_used_mib * 100 / vm_total_mib) if vm_total_mib else None

    # ---- Top macOS processes (host, NOT inside the VM) ----
    # `ps -axo` reports RSS in 1KB units. The Colima Virtualization.framework
    # XPC process represents the entire VM as one process from macOS's view.
    ps_out = sh("ps", "-axo", "pid,user,rss,comm")
    procs = []
    for line in ps_out.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rss_kb = int(parts[2])
        except ValueError:
            continue
        if rss_kb < 30 * 1024:
            continue
        comm = parts[3]
        # Friendly label = last path segment, capped.
        label = comm.split("/")[-1].split(".")[0] or comm
        if "Virtualization.VirtualMachine" in comm:
            label = "Colima VM (Docker)"
        procs.append({"label": label, "user": parts[1], "rss_mib": rss_kb / 1024})
    procs.sort(key=lambda p: p["rss_mib"], reverse=True)

    return {
        "host_mem_pct": host_mem_pct,
        "host_used_mib": host_used_mib,
        "host_total_mib": host_total_mib,
        "vm_pct": vm_pct,
        "vm_used_mib": vm_used_mib,
        "vm_total_mib": vm_total_mib,
        "disk": disk_row,
        "containers": containers,
        "top_host_procs": procs[:7],
    }


def collect_upgrades() -> dict:
    """Pending Homebrew and macOS updates on the host.

    Best-effort: a failed check degrades to ok=False instead of failing
    the whole report. `softwareupdate -l` can take a minute; launchd has
    no user-session constraints that stop either command.
    """
    out = {"brew": [], "brew_ok": False, "macos": [], "macos_ok": False}

    # Homebrew formulae + casks. Empty output = the command itself failed
    # (with nothing outdated it still prints {"formulae":[],"casks":[]}).
    raw = sh("brew", "outdated", "--json=v2", timeout=180)
    if raw.strip():
        try:
            data = json.loads(raw)

            def versions(v) -> str:
                return ", ".join(v) if isinstance(v, list) else str(v)

            for f in data.get("formulae", []) + data.get("casks", []):
                out["brew"].append({
                    "name": f.get("name", "?"),
                    "installed": versions(f.get("installed_versions", "?")),
                    "current": f.get("current_version", "?"),
                })
            out["brew_ok"] = True
        except ValueError:
            pass

    # macOS updates. The banner and the "no updates" notice split between
    # stdout and stderr depending on the OS release, so read both.
    try:
        r = subprocess.run(
            ["softwareupdate", "-l"],
            capture_output=True, text=True, timeout=300, check=False,
        )
        su_out = (r.stdout or "") + (r.stderr or "")
    except Exception:
        su_out = ""
    if "No new software available" in su_out:
        out["macos_ok"] = True
    else:
        lines = su_out.splitlines()
        for i, ln in enumerate(lines):
            m = re.match(r"^\*\s*Label:\s*(.+)$", ln.strip())
            if m:
                detail = lines[i + 1].strip() if i + 1 < len(lines) else ""
                out["macos"].append({"label": m.group(1).strip(), "detail": detail})
        out["macos_ok"] = bool(out["macos"])
    return out


def collect_node_exporter() -> dict:
    code = sh(
        "docker", "run", "--rm", "--network", ALLOY_NETWORK,
        "curlimages/curl:8.10.1",
        "-sLo", "/dev/null", "-w", "%{http_code}", "--max-time", "5",
        "http://host.docker.internal:9100/metrics", timeout=20,
    ).strip()
    return {"code": code or "000", "ok": code == "200"}

# --- formatting helpers -----------------------------------------------------

def fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d %H:%M UTC")


def warnings(report: dict) -> list[str]:
    w = []
    for c in report["containers"]:
        if not c["ok"]:
            w.append(f"{c['name']} not Up ({c['status']})")
    for p in report["probes"]:
        if not p["ok"]:
            w.append(f"{p['name']} probe returned {p['code']}")
    for t in report["traces"]:
        if t.get("ok") is False:
            w.append(f"{t['service']} has no recent traces")
    if not report["node_exporter"]["ok"]:
        w.append(f"node_exporter not responding ({report['node_exporter']['code']})")
    up = report.get("upgrades", {})
    if up.get("macos"):
        n = len(up["macos"])
        w.append(f"{n} macOS update{'s' if n != 1 else ''} pending (softwareupdate)")
    elif not up.get("macos_ok", True):
        w.append("macOS update check failed (softwareupdate -l)")
    if up.get("brew"):
        n = len(up["brew"])
        w.append(f"{n} Homebrew package{'s' if n != 1 else ''} outdated")
    elif not up.get("brew_ok", True):
        w.append("Homebrew outdated check failed")
    return w

# --- HTML rendering ---------------------------------------------------------

def html_report(report: dict, warns: list[str]) -> str:
    """Render an inline-styled, mobile-friendly HTML email."""
    overall_ok = not warns
    accent = "#1a7f37" if overall_ok else "#9a6700"
    accent_bg = "#dcfce7" if overall_ok else "#fef3c7"
    title = "All systems healthy" if overall_ok else f"{len(warns)} item{'s' if len(warns) != 1 else ''} to check"

    css_card = (
        "background:#ffffff;border:1px solid #d0d7de;border-radius:8px;"
        "padding:20px 24px;margin-bottom:16px;"
    )
    css_h2 = (
        "margin:0 0 12px 0;font-size:16px;line-height:24px;font-weight:600;"
        "color:#1f2328;"
    )
    css_th = (
        "text-align:left;padding:8px 12px;font-size:12px;font-weight:600;"
        "text-transform:uppercase;letter-spacing:0.04em;color:#656d76;"
        "border-bottom:1px solid #d0d7de;"
    )
    css_td = (
        "padding:10px 12px;font-size:14px;color:#1f2328;"
        "border-bottom:1px solid #eaeef2;"
    )
    css_td_last = css_td.replace("border-bottom:1px solid #eaeef2;", "")

    def badge(ok: bool | None, label: str | None = None) -> str:
        if ok is True:
            return ('<span style="display:inline-block;padding:2px 8px;'
                    'background:#dcfce7;color:#166534;border-radius:12px;'
                    f'font-size:12px;font-weight:600;">{escape(label or "OK")}</span>')
        if ok is False:
            return ('<span style="display:inline-block;padding:2px 8px;'
                    'background:#fee2e2;color:#991b1b;border-radius:12px;'
                    f'font-size:12px;font-weight:600;">{escape(label or "FAIL")}</span>')
        return ('<span style="display:inline-block;padding:2px 8px;'
                'background:#f3f4f6;color:#374151;border-radius:12px;'
                f'font-size:12px;font-weight:600;">{escape(label or "—")}</span>')

    def table(cols: list[str], rows: list[list[str]]) -> str:
        head = "".join(f'<th style="{css_th}">{escape(c)}</th>' for c in cols)
        body_rows = []
        for i, row in enumerate(rows):
            cells = "".join(f'<td style="{css_td if i < len(rows)-1 else css_td_last}">{c}</td>' for c in row)
            body_rows.append(f"<tr>{cells}</tr>")
        return (
            '<table role="presentation" cellspacing="0" cellpadding="0" '
            'width="100%" style="border-collapse:collapse;">'
            f"<thead><tr>{head}</tr></thead>"
            f'<tbody>{"".join(body_rows)}</tbody>'
            "</table>"
        )

    # ---- TL;DR card ----
    n_up = sum(1 for c in report["containers"] if c["ok"])
    n_total = len(report["containers"])
    n_probe_ok = sum(1 for p in report["probes"] if p["ok"])
    n_probe = len(report["probes"])
    n_alerts = len(report["alerts"])
    tldr_items = [
        ("Containers", f'{n_up}/{n_total} up'),
        ("Probes", f"{n_probe_ok}/{n_probe} 2xx/3xx"),
        ("Alerts (7d)", f"{n_alerts} fired" if n_alerts else "0 fired"),
        ("Docker", f'{report["host"]["vm_pct"]}%' if report["host"]["vm_pct"] is not None else "—"),
        ("Host", f'{report["host"]["host_mem_pct"]}%' if report["host"]["host_mem_pct"] is not None else "—"),
        ("Disk", report["host"]["disk"].get("pct", "—")),
    ]
    tldr_html = (
        '<table role="presentation" cellspacing="0" cellpadding="0" width="100%" '
        'style="border-collapse:collapse;"><tr>'
        + "".join(
            f'<td style="padding:8px 12px;text-align:center;'
            f'border-right:{"1px solid #eaeef2" if i < len(tldr_items)-1 else "none"};">'
            f'<div style="font-size:11px;color:#656d76;text-transform:uppercase;'
            f'letter-spacing:0.04em;font-weight:600;">{escape(label)}</div>'
            f'<div style="font-size:18px;font-weight:600;color:#1f2328;'
            f'margin-top:4px;">{escape(value)}</div></td>'
            for i, (label, value) in enumerate(tldr_items)
        )
        + "</tr></table>"
    )

    # ---- Containers ----
    cont_rows = []
    for c in report["containers"]:
        # Trim the long compose prefix in the visible name for readability.
        name = c["name"]
        for prefix in ("monitoringmelinakrzemowapl-", "instamelinakrzemowapl-",
                       "sribiamelinakrzemowapl-"):
            if name.startswith(prefix):
                name = prefix.split("melinakrzemowapl-")[0] + " · " + name[len(prefix):]
                break
        rc = c["restart_count"]
        rc_cell = (
            f'<span style="color:#9a6700;font-weight:600;">{rc}</span>'
            if rc > 0 else '<span style="color:#656d76;">0</span>'
        )
        cont_rows.append([
            badge(c["ok"], "Up" if c["ok"] else "Down"),
            f'<span style="font-family:ui-monospace,monospace;">{escape(name)}</span>',
            f'<span style="color:#656d76;">{escape(c["running_for"])}</span>',
            rc_cell,
        ])
    containers_card = (
        f'<div style="{css_card}">'
        f'<h2 style="{css_h2}">Containers ({n_up}/{n_total} up)</h2>'
        + table(["", "name", "uptime", "restarts"], cont_rows)
        + "</div>"
    )

    # ---- Public probes ----
    probe_rows = []
    for p in report["probes"]:
        probe_rows.append([
            badge(p["ok"], p["code"]),
            f'<span style="font-family:ui-monospace,monospace;">{escape(p["name"])}</span>',
            f'<span style="color:#656d76;font-size:13px;">'
            f'<a href="{escape(p["url"])}" style="color:#0969da;text-decoration:none;">'
            f'{escape(p["url"])}</a></span>',
        ])
    probes_card = (
        f'<div style="{css_card}">'
        f'<h2 style="{css_h2}">Public probes</h2>'
        + table(["", "service", "url"], probe_rows) + "</div>"
    )

    # ---- Traces ----
    trace_rows = []
    for t in report["traces"]:
        if t.get("ok") is None:
            trace_rows.append([
                badge(None, "skip"),
                f'<span style="font-family:ui-monospace,monospace;">{escape(t["service"])}</span>',
                f'<span style="color:#656d76;">{escape(t.get("note", ""))}</span>',
            ])
        else:
            age = fmt_age(t["age_s"]) if t["ok"] else "no traces"
            trace_rows.append([
                badge(t["ok"], "OK" if t["ok"] else "stale"),
                f'<span style="font-family:ui-monospace,monospace;">{escape(t["service"])}</span>',
                f'<span style="color:#656d76;">last trace {escape(age)} ago</span>'
                if t["ok"]
                else '<span style="color:#991b1b;">no traces in last 24h</span>',
            ])
    traces_card = (
        f'<div style="{css_card}">'
        f'<h2 style="{css_h2}">Traces (last 24h)</h2>'
        + table(["", "service", "status"], trace_rows) + "</div>"
    )

    # ---- Alerts (past 7d) ----
    if report["alerts"]:
        alert_rows = []
        for a in report["alerts"]:
            alert_rows.append([
                f'<span style="font-family:ui-monospace,monospace;font-weight:600;">{escape(a["alertname"])}</span>',
                f'<span style="font-family:ui-monospace,monospace;color:#656d76;">{escape(a["target"])}</span>',
                fmt_ts(a["first"]),
                fmt_ts(a["last"]),
                f'{a["points"]} eval points',
            ])
        alerts_card = (
            f'<div style="{css_card}">'
            f'<h2 style="{css_h2}">Alerts fired in past 7 days</h2>'
            + table(["alert", "target", "first seen", "last seen", "duration"], alert_rows)
            + "</div>"
        )
    else:
        alerts_card = (
            f'<div style="{css_card}">'
            f'<h2 style="{css_h2}">Alerts fired in past 7 days</h2>'
            f'<div style="color:#656d76;font-size:14px;">No alerts in the last 7 days.</div>'
            "</div>"
        )

    # ---- Active silences ----
    if report["silences"]:
        sil_rows = []
        for s in report["silences"]:
            sil_rows.append([
                f'<span style="font-family:ui-monospace,monospace;">{escape(s["matchers"])}</span>',
                f'<span style="color:#656d76;">{escape(s["comment"])}</span>',
                f'<span style="color:#656d76;font-size:12px;">until {escape(s["ends_at"])}</span>',
            ])
        silences_card = (
            f'<div style="{css_card}">'
            f'<h2 style="{css_h2}">Active silences</h2>'
            + table(["matchers", "comment", "expires"], sil_rows)
            + "</div>"
        )
    else:
        silences_card = ""

    # ---- Host resources (split into Docker VM + macOS host) ----
    h = report["host"]

    def bar(pct, color: str = "#0969da") -> str:
        if pct is None:
            return "—"
        pct = max(0, min(100, int(pct)))
        return (
            '<div style="background:#eaeef2;border-radius:4px;height:8px;width:100%;overflow:hidden;">'
            f'<div style="background:{color};height:8px;width:{pct}%;"></div></div>'
        )

    def mem_color_for(pct):
        p = pct or 0
        return "#1a7f37" if p < 70 else "#9a6700" if p < 85 else "#d1242f"

    def gib(mib):
        return f"{mib / 1024:.2f} GiB" if mib else "—"

    # Disk percentage
    disk_pct_str = h["disk"].get("pct", "0%").rstrip("%")
    try:
        disk_int = int(disk_pct_str)
    except ValueError:
        disk_int = 0

    # Three stat tiles: Docker VM, macOS host, Disk.
    def stat_tile(label, big, sub, pct, color):
        return (
            '<td style="padding:12px;width:33%;vertical-align:top;'
            'border:1px solid #d0d7de;border-radius:8px;">'
            '<div style="font-size:11px;color:#656d76;text-transform:uppercase;'
            'letter-spacing:0.04em;font-weight:600;margin-bottom:8px;">'
            f'{escape(label)}</div>'
            f'<div style="font-size:22px;font-weight:600;color:#1f2328;'
            f'line-height:1.2;">{escape(big)}</div>'
            f'<div style="font-size:12px;color:#656d76;margin:4px 0 8px 0;">'
            f'{escape(sub)}</div>'
            f"{bar(pct, color)}"
            "</td>"
        )

    docker_sub = (
        f'{gib(h["vm_used_mib"])} of {gib(h["vm_total_mib"])}'
        if h["vm_total_mib"] else "no data"
    )
    host_sub = (
        f'{gib(h["host_used_mib"])} of {gib(h["host_total_mib"])}'
        if h["host_total_mib"] else "no data"
    )

    host_summary_html = (
        '<table role="presentation" cellspacing="8" cellpadding="0" width="100%" '
        'style="border-collapse:separate;border-spacing:8px 0;margin-bottom:16px;">'
        '<tr>'
        + stat_tile(
            "Docker VM",
            f'{h["vm_pct"]}%' if h["vm_pct"] is not None else "—",
            docker_sub,
            h["vm_pct"],
            mem_color_for(h["vm_pct"]),
        )
        + stat_tile(
            "macOS host",
            f'{h["host_mem_pct"]}%' if h["host_mem_pct"] is not None else "—",
            host_sub,
            h["host_mem_pct"],
            mem_color_for(h["host_mem_pct"]),
        )
        + stat_tile(
            "Disk",
            h["disk"].get("pct", "—"),
            f'{h["disk"].get("used", "?")} of {h["disk"].get("total", "?")}',
            disk_int,
            mem_color_for(disk_int),
        )
        + "</tr></table>"
    )

    # Per-container memory table (Docker VM allocation usage).
    cont_stat_rows = []
    for c in h["containers"][:12]:
        mem_used_part = c["mem"].split("/")[0].strip()
        cont_stat_rows.append([
            f'<span style="font-family:ui-monospace,monospace;">{escape(c["name"])}</span>',
            f'<span style="font-family:ui-monospace,monospace;color:#656d76;">{escape(c["cpu"])}</span>',
            f'<span style="font-family:ui-monospace,monospace;">{escape(mem_used_part)}</span>',
        ])

    # macOS host top processes (everything OUTSIDE the Colima VM,
    # plus the VM itself as a single line).
    proc_rows = []
    for p in h["top_host_procs"]:
        proc_rows.append([
            f'<span style="font-family:ui-monospace,monospace;">{escape(p["label"])}</span>',
            f'<span style="color:#656d76;font-size:13px;">{escape(p["user"])}</span>',
            f'<span style="font-family:ui-monospace,monospace;">{p["rss_mib"]:.0f} MiB</span>',
        ])

    host_card = (
        f'<div style="{css_card}">'
        f'<h2 style="{css_h2}">Resources</h2>'
        + host_summary_html
        + '<div style="font-size:12px;color:#656d76;text-transform:uppercase;'
        'letter-spacing:0.04em;font-weight:600;margin:8px 0 6px 0;">'
        'Containers (Docker VM, sorted by RAM)</div>'
        + table(["name", "cpu", "memory"], cont_stat_rows)
        + '<div style="font-size:12px;color:#656d76;text-transform:uppercase;'
        'letter-spacing:0.04em;font-weight:600;margin:16px 0 6px 0;">'
        'Top macOS processes (host, outside the VM)</div>'
        + table(["process", "user", "rss"], proc_rows)
        + "</div>"
    )

    # ---- Pending upgrades ----
    up = report.get("upgrades", {"brew": [], "macos": [], "brew_ok": False, "macos_ok": False})
    up_rows = []
    for m in up["macos"]:
        up_rows.append([
            badge(None, "macOS"),
            f'<span style="font-family:ui-monospace,monospace;">{escape(m["label"])}</span>',
            f'<span style="color:#656d76;font-size:13px;">{escape(m["detail"])}</span>',
        ])
    for b in up["brew"]:
        up_rows.append([
            badge(None, "brew"),
            f'<span style="font-family:ui-monospace,monospace;">{escape(b["name"])}</span>',
            f'<span style="color:#656d76;font-size:13px;">'
            f'{escape(b["installed"])} → {escape(b["current"])}</span>',
        ])
    check_notes = []
    if not up["macos_ok"] and not up["macos"]:
        check_notes.append("macOS update check failed")
    if not up["brew_ok"]:
        check_notes.append("Homebrew check failed")
    if up_rows:
        upgrades_body = (
            table(["", "package", "update"], up_rows)
            + '<div style="font-size:12px;color:#656d76;margin-top:10px;">'
            'Apply on the Air: <span style="font-family:ui-monospace,monospace;">'
            'brew upgrade</span> · <span style="font-family:ui-monospace,monospace;">'
            'sudo softwareupdate -i -a</span></div>'
        )
    elif check_notes:
        upgrades_body = (
            f'<div style="color:#9a6700;font-size:14px;">{escape(" · ".join(check_notes))}</div>'
        )
    else:
        upgrades_body = (
            '<div style="color:#656d76;font-size:14px;">Everything up to date.</div>'
        )
    upgrades_card = (
        f'<div style="{css_card}">'
        f'<h2 style="{css_h2}">Pending upgrades</h2>'
        + upgrades_body
        + "</div>"
    )

    # ---- Header ----
    today = datetime.now().strftime("%A, %B %d, %Y")
    header_html = (
        f'<div style="background:{accent_bg};border:1px solid {accent};'
        'border-radius:8px;padding:20px 24px;margin-bottom:16px;">'
        f'<div style="display:inline-block;padding:4px 10px;background:{accent};'
        'color:#ffffff;border-radius:4px;font-size:11px;font-weight:700;'
        'text-transform:uppercase;letter-spacing:0.06em;">'
        f'{"Healthy" if overall_ok else "Attention"}</div>'
        f'<h1 style="margin:10px 0 4px 0;font-size:22px;line-height:30px;'
        f'color:#1f2328;">{escape(title)}</h1>'
        f'<div style="font-size:13px;color:#656d76;">somsiad weekly · {escape(today)}</div>'
        + (
            ('<div style="margin-top:12px;font-size:14px;color:#1f2328;">'
             '<strong>Items to check:</strong><ul style="margin:6px 0 0 0;padding-left:20px;">'
             + "".join(f'<li>{escape(w)}</li>' for w in warns)
             + "</ul></div>") if warns else ""
        )
        + "</div>"
    )

    footer_html = (
        '<div style="text-align:center;font-size:12px;color:#656d76;'
        'padding:8px 0 0 0;">'
        '<a href="https://monitoring.melinakrzemowa.pl" '
        'style="color:#0969da;text-decoration:none;">monitoring.melinakrzemowa.pl</a>'
        ' · '
        '<a href="https://github.com/melinakrzemowa/somsiad" '
        'style="color:#0969da;text-decoration:none;">github.com/melinakrzemowa/somsiad</a>'
        "</div>"
    )

    # ---- Charts (rendered Grafana panels) ----
    charts = report.get("charts", [])
    if charts:
        chart_blocks = []
        for c in charts:
            chart_blocks.append(
                f'<div style="margin-bottom:14px;">'
                f'<div style="font-size:12px;color:#656d76;text-transform:uppercase;'
                f'letter-spacing:0.04em;font-weight:600;margin-bottom:6px;">'
                f'{escape(c["title"])}</div>'
                f'<img src="cid:{escape(c["cid"])}" alt="{escape(c["title"])}" '
                f'width="100%" style="display:block;width:100%;max-width:552px;'
                f'height:auto;border:1px solid #d0d7de;border-radius:6px;"/>'
                "</div>"
            )
        # Friendly window label, derived from CHART_FROM ("now-24h" -> "24h").
        window_label = CHART_FROM.replace("now-", "").lstrip("-")
        charts_card = (
            f'<div style="{css_card}">'
            f'<h2 style="{css_h2}">Charts (last {escape(window_label)})</h2>'
            + "".join(chart_blocks)
            + '<div style="font-size:11px;color:#656d76;margin-top:8px;">'
            'Live versions: '
            f'<a href="https://monitoring.melinakrzemowa.pl/d/{escape(WEEKLY_DASHBOARD_UID)}" '
            'style="color:#0969da;text-decoration:none;">'
            'monitoring.melinakrzemowa.pl/d/somsiad-weekly</a>'
            "</div>"
            "</div>"
        )
    else:
        charts_card = ""

    body = (
        '<!doctype html><html><head>'
        '<meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        '<meta name="color-scheme" content="light"/>'
        '<meta name="supported-color-schemes" content="light"/>'
        '<title>somsiad weekly</title>'
        "</head>"
        '<body style="margin:0;padding:0;background:#f6f8fa;">'
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        'width="100%" style="background:#f6f8fa;padding:24px 12px;">'
        '<tr><td align="center">'
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        'width="600" style="max-width:600px;width:100%;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        '">'
        '<tr><td>'
        + header_html
        + f'<div style="{css_card}">'
        + f'<h2 style="{css_h2}">At a glance</h2>'
        + tldr_html
        + "</div>"
        + charts_card
        + containers_card
        + probes_card
        + traces_card
        + alerts_card
        + silences_card
        + host_card
        + upgrades_card
        + footer_html
        + "</td></tr></table></td></tr></table></body></html>"
    )
    return body

# --- plain-text rendering ---------------------------------------------------

def text_report(report: dict, warns: list[str]) -> str:
    lines = []
    lines.append(f'somsiad weekly · {datetime.now().strftime("%A, %B %d, %Y")}')
    lines.append("=" * 60)
    if warns:
        lines.append(f"ATTENTION: {len(warns)} item(s) to check")
        for w in warns:
            lines.append(f"  - {w}")
    else:
        lines.append("All systems healthy.")
    lines.append("")

    lines.append("CONTAINERS")
    for c in report["containers"]:
        mark = "✓" if c["ok"] else "✗"
        rc = f" (restarts: {c['restart_count']})" if c["restart_count"] else ""
        lines.append(f"  {mark} {c['name']:<48} {c['running_for']}{rc}")
    lines.append("")

    lines.append("PUBLIC PROBES")
    for p in report["probes"]:
        mark = "✓" if p["ok"] else "✗"
        lines.append(f"  {mark} {p['name']:<12} {p['code']:>4}  {p['url']}")
    lines.append("")

    lines.append("TRACES (last 24h)")
    for t in report["traces"]:
        if t.get("ok") is None:
            lines.append(f"  · {t['service']:<12} {t.get('note','')}")
        elif t["ok"]:
            lines.append(f"  ✓ {t['service']:<12} last trace {fmt_age(t['age_s'])} ago")
        else:
            lines.append(f"  ✗ {t['service']:<12} no traces in last 24h")
    lines.append("")

    lines.append("ALERTS FIRED (past 7d)")
    if report["alerts"]:
        for a in report["alerts"]:
            lines.append(
                f"  ⚠ {a['alertname']} on {a['target']} — "
                f"{fmt_ts(a['first'])} → {fmt_ts(a['last'])} ({a['points']} eval points)"
            )
    else:
        lines.append("  ✓ none")
    lines.append("")

    if report["silences"]:
        lines.append("ACTIVE SILENCES")
        for s in report["silences"]:
            lines.append(f"  · {s['matchers']} — {s['comment']} (until {s['ends_at']})")
        lines.append("")

    h = report["host"]
    lines.append("RESOURCES")

    def gib(mib):
        return f"{mib / 1024:.2f} GiB" if mib else "—"

    lines.append(f"  Docker VM:  {h['vm_pct']}% ({gib(h['vm_used_mib'])} of {gib(h['vm_total_mib'])})")
    lines.append(f"  macOS host: {h['host_mem_pct']}% ({gib(h['host_used_mib'])} of {gib(h['host_total_mib'])})")
    if h["disk"]:
        lines.append(
            f"  Disk:       {h['disk'].get('pct','?')} "
            f"({h['disk'].get('used','?')} of {h['disk'].get('total','?')})"
        )
    lines.append("")
    lines.append("CONTAINERS (Docker VM, by RAM)")
    for c in h["containers"][:12]:
        lines.append(f"  {c['name']:<46} cpu {c['cpu']:>7}  mem {c['mem']}")
    lines.append("")
    lines.append("TOP macOS PROCESSES (outside the VM)")
    for p in h["top_host_procs"]:
        lines.append(f"  {p['label']:<32} {p['user']:<12} {p['rss_mib']:>5.0f} MiB")
    lines.append("")

    up = report.get("upgrades", {"brew": [], "macos": [], "brew_ok": False, "macos_ok": False})
    lines.append("PENDING UPGRADES")
    if up["macos"]:
        for m in up["macos"]:
            lines.append(f"  ⚠ macOS  {m['label']}")
            if m["detail"]:
                lines.append(f"           {m['detail']}")
    elif up["macos_ok"]:
        lines.append("  ✓ macOS: up to date")
    else:
        lines.append("  ? macOS: update check failed")
    if up["brew"]:
        for b in up["brew"]:
            lines.append(f"  ⚠ brew   {b['name']:<28} {b['installed']} → {b['current']}")
    elif up["brew_ok"]:
        lines.append("  ✓ Homebrew: up to date")
    else:
        lines.append("  ? Homebrew: check failed")
    if up["macos"] or up["brew"]:
        lines.append("  apply: brew upgrade / sudo softwareupdate -i -a")
    lines.append("")

    ne = report["node_exporter"]
    lines.append(f"node_exporter: {'OK' if ne['ok'] else 'ERROR'} (HTTP {ne['code']})")
    lines.append("")
    lines.append("monitoring.melinakrzemowa.pl  ·  github.com/melinakrzemowa/somsiad")
    return "\n".join(lines)

# --- main -------------------------------------------------------------------

def collect_charts() -> list[dict]:
    """Render the weekly dashboard panels as PNG bytes. Best-effort: a
    chart that fails to render is omitted from the email but doesn't fail
    the whole run."""
    out = []
    for spec in RENDER_PANELS:
        png = render_panel(WEEKLY_DASHBOARD_UID, spec["id"],
                           height=spec.get("height", 280))
        if png and png[:4] == b"\x89PNG":
            cid = make_msgid(domain="somsiad")[1:-1]  # strip < >
            out.append({
                "id": spec["id"],
                "title": spec["title"],
                "png": png,
                "cid": cid,
            })
        else:
            sys.stderr.write(
                f"chart panel {spec['id']} ({spec['title']}): no PNG returned "
                f"({len(png)} bytes)\n"
            )
    return out


def collect_all() -> dict:
    return {
        "containers": collect_containers(),
        "probes": collect_probes(),
        "traces": collect_traces(),
        "alerts": collect_alerts_past_week(),
        "silences": collect_silences(),
        "host": collect_host_resources(),
        "node_exporter": collect_node_exporter(),
        "upgrades": collect_upgrades(),
        "charts": collect_charts(),
    }


def send_email(subject: str, html: str, text: str, charts: list[dict]) -> None:
    """Build a multipart/related email so cid: image references in the HTML
    resolve to the rendered Grafana panel PNGs."""
    # Outer container holds the body + inline images.
    root = MIMEMultipart("related")
    root["From"] = f"{MAIL_FROM_NAME} <{MAIL_FROM_ADDR}>"
    root["To"] = MAIL_TO
    root["Subject"] = subject
    root["Date"] = formatdate(localtime=True)

    # Body alternates text and HTML for clients that pick.
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    root.attach(alt)

    for c in charts:
        img = MIMEImage(c["png"], _subtype="png")
        img.add_header("Content-ID", f"<{c['cid']}>")
        img.add_header("Content-Disposition", "inline",
                       filename=f"{c['cid']}.png")
        root.attach(img)

    password = open(SMTP_PASSWORD_FILE).read().strip()
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
        s.login(SMTP_USER, password)
        s.send_message(root)


def main() -> int:
    try:
        report = collect_all()
        warns = warnings(report)
        html = html_report(report, warns)
        text = text_report(report, warns)

        scripts_dir = f"{DEPLOY_DIR}/scripts"
        os.makedirs(scripts_dir, exist_ok=True)
        with open(f"{scripts_dir}/last_run.html", "w") as f:
            f.write(html)
        with open(f"{scripts_dir}/last_run.txt", "w") as f:
            f.write(text)

        date_tag = datetime.now().strftime("%Y-%m-%d")
        if warns:
            subject = f"[somsiad] {len(warns)} to check — {date_tag}"
        else:
            subject = f"[somsiad] OK — {date_tag}"

        send_email(subject, html, text, report.get("charts", []))
        n_charts = len(report.get("charts", []))
        print(f"sent: {subject} ({n_charts} chart{'s' if n_charts != 1 else ''})")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
