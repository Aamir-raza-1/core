"""Auto-detect installed MCP clients and register/unregister the ci-proof MCP server.

Supported clients:
- claude-code    (Claude Code: ~/.claude.json)
- claude-desktop (Claude Desktop: claude_desktop_config.json)
- cursor         (Cursor: ~/.cursor/mcp.json)
- vscode         (VS Code / Copilot: User/mcp.json)
- codex          (Codex CLI: ~/.codex/config.toml)
- windsurf       (Windsurf: ~/.codeium/windsurf/mcp_config.json)
- gemini         (Gemini CLI: ~/.gemini/settings.json)
- antigravity    (Antigravity: ~/.gemini/config/mcp_config.json)
- zed            (Zed: zed/settings.json)

By default, runs in --dry-run mode and prints a unified diff without modifying disk.
Pass --apply (or --no-dry-run) to back up each existing config file (<file>.bak) and write changes.
"""
import argparse
import difflib
import json
import os
import re
import shutil
import sys

SERVER_NAME = "ci-proof"
DEFAULT_COMMAND = "ci-proof"
DEFAULT_ARGS = ["mcp"]

CLIENT_ORDER = (
    "claude-code",
    "claude-desktop",
    "cursor",
    "vscode",
    "codex",
    "windsurf",
    "gemini",
    "antigravity",
    "zed",
)

CLIENT_ALIASES = {
    "claude": "claude-code",
    "claude_code": "claude-code",
    "claudecode": "claude-code",
    "claude_desktop": "claude-desktop",
    "claudedesktop": "claude-desktop",
    "code": "vscode",
    "copilot": "vscode",
    "vscode-copilot": "vscode",
    "codex-cli": "codex",
    "codex_cli": "codex",
    "gemini-cli": "gemini",
    "gemini_cli": "gemini",
    "agy": "antigravity",
}

CLIENT_META = {
    "claude-code": {
        "display_name": "Claude Code",
        "format": "json",
        "top_key": "mcpServers",
        "binaries": ("claude",),
        "mac_apps": (),
        "home_markers": (".claude.json", ".claude"),
    },
    "claude-desktop": {
        "display_name": "Claude Desktop",
        "format": "json",
        "top_key": "mcpServers",
        "binaries": (),
        "mac_apps": ("Claude.app",),
        "home_markers": (),
    },
    "cursor": {
        "display_name": "Cursor",
        "format": "json",
        "top_key": "mcpServers",
        "binaries": ("cursor",),
        "mac_apps": ("Cursor.app",),
        "home_markers": (".cursor",),
    },
    "vscode": {
        "display_name": "VS Code / Copilot",
        "format": "json",
        "top_key": "servers",
        "binaries": ("code",),
        "mac_apps": ("Visual Studio Code.app",),
        "home_markers": (),
    },
    "codex": {
        "display_name": "Codex CLI",
        "format": "toml",
        "top_key": "mcp_servers",
        "binaries": ("codex",),
        "mac_apps": (),
        "home_markers": (".codex",),
    },
    "windsurf": {
        "display_name": "Windsurf",
        "format": "json",
        "top_key": "mcpServers",
        "binaries": ("windsurf",),
        "mac_apps": ("Windsurf.app",),
        "home_markers": (".codeium/windsurf",),
    },
    "gemini": {
        "display_name": "Gemini CLI",
        "format": "json",
        "top_key": "mcpServers",
        "binaries": ("gemini",),
        "mac_apps": (),
        "home_markers": (".gemini/settings.json",),
    },
    "antigravity": {
        "display_name": "Antigravity",
        "format": "json",
        "top_key": "mcpServers",
        "binaries": ("agy", "antigravity"),
        "mac_apps": ("Antigravity.app",),
        "home_markers": (
            ".gemini/config/mcp_config.json",
            ".gemini/antigravity",
            ".gemini/antigravity-cli",
            ".local/bin/agy",
        ),
    },
    "zed": {
        "display_name": "Zed",
        "format": "json",
        "top_key": "context_servers",
        "binaries": ("zed",),
        "mac_apps": ("Zed.app",),
        "home_markers": (".config/zed",),
    },
}


def normalize_client_name(name):
    key = name.strip().lower()
    key = CLIENT_ALIASES.get(key, key)
    if key not in CLIENT_META:
        raise ValueError(
            "Unknown MCP client %r (expected one of: %s)" % (name, ", ".join(CLIENT_ORDER))
        )
    return key


def _appdata_dir(home, platform_name):
    if platform_name == "win32":
        env_appdata = os.environ.get("APPDATA")
        default_home = os.path.expanduser("~")
        if env_appdata and os.path.abspath(home) == os.path.abspath(default_home):
            return env_appdata
        return os.path.join(home, "AppData", "Roaming")
    return home


def get_config_path(client, home=None, platform_name=None):
    """Return the canonical config file path for `client` on `platform_name`."""
    client = normalize_client_name(client)
    home = os.path.abspath(home or os.path.expanduser("~"))
    plat = platform_name or sys.platform

    if client == "claude-code":
        return os.path.join(home, ".claude.json")
    if client == "claude-desktop":
        if plat == "darwin":
            return os.path.join(
                home, "Library", "Application Support", "Claude", "claude_desktop_config.json"
            )
        if plat == "win32":
            return os.path.join(_appdata_dir(home, plat), "Claude", "claude_desktop_config.json")
        return os.path.join(home, ".config", "Claude", "claude_desktop_config.json")
    if client == "cursor":
        return os.path.join(home, ".cursor", "mcp.json")
    if client == "vscode":
        if plat == "darwin":
            return os.path.join(home, "Library", "Application Support", "Code", "User", "mcp.json")
        if plat == "win32":
            return os.path.join(_appdata_dir(home, plat), "Code", "User", "mcp.json")
        return os.path.join(home, ".config", "Code", "User", "mcp.json")
    if client == "codex":
        return os.path.join(home, ".codex", "config.toml")
    if client == "windsurf":
        return os.path.join(home, ".codeium", "windsurf", "mcp_config.json")
    if client == "gemini":
        return os.path.join(home, ".gemini", "settings.json")
    if client == "antigravity":
        return os.path.join(home, ".gemini", "config", "mcp_config.json")
    if client == "zed":
        if plat == "win32":
            return os.path.join(_appdata_dir(home, plat), "Zed", "settings.json")
        return os.path.join(home, ".config", "zed", "settings.json")
    raise ValueError("Unhandled client: %s" % client)


def _client_parent_marker(client, home, plat):
    """Return a client-specific parent directory whose existence indicates the client is installed."""
    cfg = get_config_path(client, home=home, platform_name=plat)
    if client in ("claude-desktop", "vscode", "zed"):
        # For VS Code, check .../Code; for Claude Desktop, .../Claude; for Zed, .../zed
        if client == "vscode":
            return os.path.dirname(os.path.dirname(cfg))
        return os.path.dirname(cfg)
    return None


def detect_client(client, home=None, platform_name=None, check_system=True):
    """Return dict with detection details for a single MCP client."""
    client = normalize_client_name(client)
    meta = CLIENT_META[client]
    home = os.path.abspath(home or os.path.expanduser("~"))
    plat = platform_name or sys.platform
    cfg_path = get_config_path(client, home=home, platform_name=plat)
    reasons = []

    if os.path.isfile(cfg_path):
        reasons.append("config:%s" % cfg_path)

    parent_marker = _client_parent_marker(client, home, plat)
    if parent_marker and os.path.isdir(parent_marker):
        reasons.append("dir:%s" % parent_marker)

    for rel in meta["home_markers"]:
        marker_path = os.path.join(home, rel)
        if os.path.exists(marker_path):
            reasons.append("home:%s" % marker_path)

    if check_system:
        for bname in meta["binaries"]:
            found = shutil.which(bname)
            if found:
                reasons.append("bin:%s" % found)
            else:
                local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", bname)
                if os.path.isfile(local_bin) and os.access(local_bin, os.X_OK):
                    reasons.append("bin:%s" % local_bin)
        if plat == "darwin":
            for app in meta["mac_apps"]:
                for app_root in ("/Applications", os.path.join(home, "Applications")):
                    app_path = os.path.join(app_root, app)
                    if os.path.isdir(app_path):
                        reasons.append("app:%s" % app_path)

    return {
        "client": client,
        "display_name": meta["display_name"],
        "detected": bool(reasons),
        "reasons": reasons,
        "config_path": cfg_path,
        "config_exists": os.path.isfile(cfg_path),
    }


def detect_clients(home=None, platform_name=None, check_system=True):
    """Return detection status dicts for all 9 supported MCP clients."""
    return [
        detect_client(c, home=home, platform_name=platform_name, check_system=check_system)
        for c in CLIENT_ORDER
    ]


def build_server_entry(client, command=DEFAULT_COMMAND, args=None):
    """Return the client-specific JSON object for the ci-proof server entry."""
    client = normalize_client_name(client)
    arg_list = list(DEFAULT_ARGS if args is None else args)
    if client == "claude-code":
        return {
            "type": "stdio",
            "command": command,
            "args": arg_list,
            "env": {},
        }
    if client == "vscode":
        return {
            "type": "stdio",
            "command": command,
            "args": arg_list,
        }
    if client == "antigravity":
        return {
            "command": command,
            "args": arg_list,
            "disabled": False,
        }
    if client == "zed":
        return {
            "source": "custom",
            "command": command,
            "args": arg_list,
            "env": {},
        }
    return {
        "command": command,
        "args": arg_list,
    }


def _update_json_content(raw_text, client, action, server_name, command, args):
    meta = CLIENT_META[client]
    top_key = meta["top_key"]

    if raw_text.strip():
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            raise ValueError("Expected top-level JSON object in %s config" % client)
    else:
        data = {}
        if client == "vscode":
            data["inputs"] = []

    if action == "install":
        container = data.get(top_key)
        if not isinstance(container, dict):
            container = {}
            data[top_key] = container
        container[server_name] = build_server_entry(client, command=command, args=args)
    elif action == "uninstall":
        if not raw_text.strip():
            return ""
        container = data.get(top_key)
        if isinstance(container, dict) and server_name in container:
            del container[server_name]
        else:
            return raw_text
    else:
        raise ValueError("Unsupported action: %s" % action)

    return json.dumps(data, indent=2) + "\n"


def _format_toml_section(server_name, command, args):
    arg_list = list(DEFAULT_ARGS if args is None else args)
    # Bare key if simple [A-Za-z0-9_-], else quoted
    if re.match(r"^[A-Za-z0-9_-]+$", server_name):
        header = "[mcp_servers.%s]" % server_name
    else:
        header = '[mcp_servers.%s]' % json.dumps(server_name)
    lines = [
        header,
        "command = %s" % json.dumps(command),
        "args = [%s]" % ", ".join(json.dumps(a) for a in arg_list),
    ]
    return "\n".join(lines) + "\n"


def _remove_toml_section(raw_text, server_name):
    if not raw_text:
        return "", False
    escaped = re.escape(server_name)
    quoted = re.escape(json.dumps(server_name))
    header_re = re.compile(
        r"^\s*\[\s*mcp_servers\.(?:%s|%s)\s*\]\s*(?:#.*)?$" % (escaped, quoted)
    )
    any_section_re = re.compile(r"^\s*\[[^\]]+\]\s*(?:#.*)?$")

    lines = raw_text.splitlines(keepends=True)
    out = []
    skipping = False
    removed = False
    for line in lines:
        if header_re.match(line):
            skipping = True
            removed = True
            # Strip a single preceding blank separator line if we added one
            if out and out[-1].strip() == "" and len(out) > 1 and out[-2].strip() == "":
                out.pop()
            continue
        if skipping and any_section_re.match(line):
            skipping = False
        if not skipping:
            out.append(line)

    res = "".join(out)
    # Clean trailing excessive blank lines while keeping single trailing newline if non-empty
    if removed:
        res = res.rstrip()
        if res:
            res += "\n"
    return res, removed


def _update_toml_content(raw_text, action, server_name, command, args):
    cleaned, removed = _remove_toml_section(raw_text, server_name)
    if action == "uninstall":
        if not removed:
            return raw_text
        return cleaned
    if action == "install":
        section = _format_toml_section(server_name, command, args)
        if not cleaned.strip():
            return section
        sep = "\n" if cleaned.endswith("\n\n") else ("\n" if not cleaned.endswith("\n") else "")
        if not cleaned.endswith("\n"):
            cleaned += "\n"
        return cleaned + "\n" + section
    raise ValueError("Unsupported action: %s" % action)


def compute_client_change(
    client,
    action="install",
    home=None,
    platform_name=None,
    server_name=SERVER_NAME,
    command=DEFAULT_COMMAND,
    args=None,
):
    """Compute the before/after content and unified diff for a client config file."""
    client = normalize_client_name(client)
    meta = CLIENT_META[client]
    cfg_path = get_config_path(client, home=home, platform_name=platform_name)
    existed = os.path.isfile(cfg_path)
    before = ""
    if existed:
        with open(cfg_path, encoding="utf-8") as f:
            before = f.read()

    if meta["format"] == "json":
        after = _update_json_content(before, client, action, server_name, command, args)
    elif meta["format"] == "toml":
        after = _update_toml_content(before, action, server_name, command, args)
    else:
        raise ValueError("Unsupported format: %s" % meta["format"])

    changed = before != after
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=cfg_path if existed else "/dev/null",
            tofile=cfg_path,
        )
    )
    diff_text = "".join(diff_lines)
    return {
        "client": client,
        "display_name": meta["display_name"],
        "action": action,
        "config_path": cfg_path,
        "existed": existed,
        "changed": changed,
        "before": before,
        "after": after,
        "diff": diff_text,
        "backup_path": (cfg_path + ".bak") if (existed and changed) else None,
    }


def apply_client_change(change, dry_run=True):
    """Apply a computed client change to disk if dry_run is False.

    When dry_run=False and the target config file already exists on disk, backs it up
    to `<config_path>.bak` before modifying it.
    """
    result = dict(change)
    result["dry_run"] = bool(dry_run)
    result["applied"] = False
    result["backed_up"] = False

    if dry_run or not change["changed"]:
        return result

    cfg_path = change["config_path"]
    if os.path.isfile(cfg_path):
        bak_path = cfg_path + ".bak"
        shutil.copy2(cfg_path, bak_path)
        result["backup_path"] = bak_path
        result["backed_up"] = True

    parent = os.path.dirname(cfg_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(change["after"])
    result["applied"] = True
    return result


def run_install(
    action="install",
    clients=None,
    dry_run=True,
    home=None,
    platform_name=None,
    check_system=True,
    server_name=SERVER_NAME,
    command=DEFAULT_COMMAND,
    args=None,
):
    """Auto-detect (or filter) MCP clients and register/unregister ci-proof."""
    detections = detect_clients(home=home, platform_name=platform_name, check_system=check_system)
    if action == "status":
        return {
            "action": "status",
            "dry_run": True,
            "clients": detections,
        }

    if clients:
        if "all" in [c.strip().lower() for c in clients]:
            target_names = list(CLIENT_ORDER)
        else:
            target_names = [normalize_client_name(c) for c in clients]
    else:
        target_names = [d["client"] for d in detections if d["detected"]]

    results = []
    for cname in target_names:
        change = compute_client_change(
            cname,
            action=action,
            home=home,
            platform_name=platform_name,
            server_name=server_name,
            command=command,
            args=args,
        )
        res = apply_client_change(change, dry_run=dry_run)
        results.append(res)

    return {
        "action": action,
        "dry_run": bool(dry_run),
        "detected_clients": [d["client"] for d in detections if d["detected"]],
        "target_clients": target_names,
        "results": results,
    }


def _format_human_report(report):
    lines = []
    if report["action"] == "status":
        lines.append("MCP Client Auto-Detection Status:")
        for d in report["clients"]:
            status = "DETECTED" if d["detected"] else "not-found"
            why = (" (%s)" % ", ".join(d["reasons"])) if d["reasons"] else ""
            lines.append(
                "  %-16s %-20s %-10s %s%s"
                % (d["client"], d["display_name"], status, d["config_path"], why)
            )
        return "\n".join(lines)

    mode = "DRY-RUN (no files modified; pass --apply to write)" if report["dry_run"] else "APPLIED"
    lines.append("ci-proof MCP %s [%s]" % (report["action"], mode))
    if not report["target_clients"]:
        lines.append("No MCP clients detected or selected.")
        return "\n".join(lines)

    for r in report["results"]:
        if not r["changed"]:
            state = "already up-to-date"
        elif r["applied"]:
            state = "updated" + (" (backup: %s)" % r["backup_path"] if r["backed_up"] else " (created)")
        else:
            state = "would update" + (
                " (would backup: %s)" % r["backup_path"] if r["existed"] else " (would create)"
            )
        lines.append("- %s (%s): %s -> %s" % (r["display_name"], r["client"], r["config_path"], state))
        if r["diff"]:
            lines.append(r["diff"].rstrip("\n"))
    return "\n".join(lines)


def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="ci-proof mcp-install",
        description=(
            "Auto-detect installed MCP clients and register or unregister the ci-proof MCP server. "
            "Runs in --dry-run mode by default and prints the unified diff."
        ),
    )
    ap.add_argument(
        "action_pos",
        nargs="?",
        choices=["install", "uninstall", "status"],
        default=None,
        help="Optional action: install (default), uninstall, or status",
    )
    ap.add_argument(
        "--uninstall",
        "--unregister",
        dest="uninstall",
        action="store_true",
        help="Unregister ci-proof from client config files",
    )
    ap.add_argument(
        "--status",
        "--detect",
        dest="status",
        action="store_true",
        help="List all supported MCP clients and their detection status",
    )
    ap.add_argument(
        "--client",
        "--clients",
        dest="clients",
        action="append",
        help="Target specific client(s) (comma-separated or repeatable, or 'all')",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Print unified diff without modifying any files (default)",
    )
    mode.add_argument(
        "--apply",
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Back up existing config files (<file>.bak) and write changes to disk",
    )
    ap.add_argument(
        "--home",
        default=None,
        help="Override HOME directory for config paths and detection",
    )
    ap.add_argument(
        "--platform",
        choices=["darwin", "linux", "win32"],
        default=None,
        help="Override target OS platform for config path resolution",
    )
    ap.add_argument(
        "--isolated-home",
        action="store_true",
        help="Detect clients strictly from files inside --home (ignore host PATH / /Applications)",
    )
    ap.add_argument(
        "--command",
        default=DEFAULT_COMMAND,
        help="Server command executable (default: ci-proof)",
    )
    ap.add_argument(
        "--arg",
        dest="server_args",
        action="append",
        default=None,
        help="Server command arguments (default: mcp)",
    )
    ap.add_argument(
        "--name",
        default=SERVER_NAME,
        help="Server registration name (default: ci-proof)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON report",
    )
    return ap


def main(argv=None):
    ap = build_arg_parser()
    ns = ap.parse_args(argv)

    if ns.status or ns.action_pos == "status":
        action = "status"
    elif ns.uninstall or ns.action_pos == "uninstall":
        action = "uninstall"
    else:
        action = "install"

    client_list = []
    if ns.clients:
        for item in ns.clients:
            for part in item.split(","):
                if part.strip():
                    client_list.append(part.strip())

    report = run_install(
        action=action,
        clients=client_list or None,
        dry_run=ns.dry_run,
        home=ns.home,
        platform_name=ns.platform,
        check_system=not ns.isolated_home,
        server_name=ns.name,
        command=ns.command,
        args=ns.server_args,
    )
    if ns.json:
        # Omit full before/after blobs in compact CLI JSON if desired, or keep full report
        out_report = dict(report)
        if "results" in out_report:
            out_report["results"] = [
                {k: v for k, v in r.items() if k not in ("before", "after")}
                for r in out_report["results"]
            ]
        print(json.dumps(out_report, indent=2))
    else:
        print(_format_human_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
