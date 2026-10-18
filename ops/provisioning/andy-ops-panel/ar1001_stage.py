"""Add AR-1001 ficha to an EXISTING MGMT installation without replacing other tabs.

Use --target-dir on a disposable COPIED tree before staging to live. This
never edits app.js, transport_observability.py, investigator, or sources.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent
HTML_TAB = '    <button class="tab" data-tab="ar1001" type="button">FALHAS / AR-1001</button>'
HTML_SECTION = """    <section id="ar1001-view" class="view" aria-label="Ficha de ocorrência AR-1001">
      <div id="ar1001-content" aria-live="polite"><p class="empty">Selecione a aba para consultar as evidências do Zabbix.</p></div>
    </section>
"""
STYLE_LINK = '  <link rel="stylesheet" href="ar1001-card.css">'
SCRIPT_LINK = '  <script src="ar1001-card.js" defer></script>'
IMPORT = 'from ar1001_observability import cached_sample\n'
HANDLER = '''        if path == "/api/ar1001":
            self._json(cached_sample())
            return
'''


def prepare(index: str, server: str) -> tuple[str, str]:
    if 'data-tab="ar1001"' in index or '"/api/ar1001"' in server:
        raise ValueError("ALREADY_INSTALLED")
    if index.count('    <section id="compute-view"') != 1:
        raise ValueError("INDEX_ANCHOR_CHANGED")
    if index.count('    <button class="tab" data-tab="chat"') != 1:
        raise ValueError("TAB_ANCHOR_CHANGED")
    if index.count('</head>') != 1 or index.count('</body>') != 1:
        raise ValueError("HTML_ANCHOR_CHANGED")
    if server.count('from message_tracing import sample_message_traces\n') != 1:
        raise ValueError("IMPORT_ANCHOR_CHANGED")
    if server.count('        if path == "/api/config":\n') != 1:
        raise ValueError("HANDLER_ANCHOR_CHANGED")
    index = index.replace(
        '    <button class="tab" data-tab="chat"',
        HTML_TAB + '\n    <button class="tab" data-tab="chat"', 1
    )
    index = index.replace('    <section id="compute-view"', HTML_SECTION + '\n    <section id="compute-view"', 1)
    index = index.replace('</head>', STYLE_LINK + '\n</head>')
    index = index.replace('</body>', SCRIPT_LINK + '\n</body>')
    server = server.replace('from message_tracing import sample_message_traces\n',
                            'from message_tracing import sample_message_traces\n' + IMPORT)
    server = server.replace('        if path == "/api/config":\n',
                            HANDLER + '        if path == "/api/config":\n')
    return index, server


def stage(target_dir: Path) -> dict:
    if not target_dir.is_dir():
        raise ValueError("NO_EXISTING_MGMT")
    html_file, server_file = target_dir / "index.html", target_dir / "server.py"
    if not html_file.is_file() or not server_file.is_file():
        raise ValueError("EXISTING_MGMT_FILES_MISSING")
    old_html = html_file.read_text(encoding="utf-8")
    old_server = server_file.read_text(encoding="utf-8")
    new_html, new_server = prepare(old_html, old_server)
    original_tabs = set(re.findall(r'data-tab="([^"]+)"', old_html))
    new_tabs = set(re.findall(r'data-tab="([^"]+)"', new_html))
    if new_tabs != original_tabs | {"ar1001"}:
        raise ValueError("ORIGINAL_TABS_NOT_PRESERVED")
    for name in ("ar1001_observability.py", "ar1001-card.js", "ar1001-card.css"):
        if (target_dir / name).exists():
            raise ValueError("AR1001_TARGET_ALREADY_EXISTS")
    # Preflight complete: stage only within caller-selected, private candidate.
    for name in ("ar1001_observability.py", "ar1001-card.js", "ar1001-card.css"):
        shutil.copy2(ROOT / name, target_dir / name)
    html_file.write_text(new_html, encoding="utf-8")
    server_file.write_text(new_server, encoding="utf-8")
    return {"original_tabs": len(original_tabs), "candidate_tabs": len(new_tabs),
            "new_api": "/api/ar1001", "new_source": "ROC_ZABBIX_READONLY"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()
    report = stage(args.target_dir)
    print("AR1001_CANDIDATE_STAGED", report)


if __name__ == "__main__":
    main()
