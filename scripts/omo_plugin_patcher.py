#!/usr/bin/env python3
"""Re-aplica o patch dot-strip no bundle do oh-my-openagent quando updates @latest o desfazem.

Idempotente: marker presente → exit 0 silencioso; ausente → backup + patch +
validação `node --check`. Formato desconhecido → exit 3 (revisão manual).
"""

import os
import shutil
import subprocess
import sys
import time

PLUGIN_PATH = os.path.expanduser(
    "~/.cache/opencode/packages/oh-my-openagent@latest/"
    "node_modules/oh-my-openagent/dist/index.js"
)

MARKER = 'resolved.model = resolved.model.replace(/\\.+$/, "");'

UNPATCHED_FN = (
    "function resolveModelPipeline2(request) {\n"
    "  const resolved = resolveModelPipeline(request, exports_connected_providers_cache);\n"
    "  return resolved;\n"
    "}"
)

PATCHED_FN = (
    "function resolveModelPipeline2(request) {\n"
    "  const resolved = resolveModelPipeline(request, exports_connected_providers_cache);\n"
    "  if (resolved && typeof resolved.model === \"string\") {\n"
    "    resolved.model = resolved.model.replace(/\\.+$/, \"\");\n"
    "  }\n"
    "  return resolved;\n"
    "}"
)


def main() -> int:
    # Path override via argv[1]: permite testar contra cópias em /tmp.
    plugin_path = sys.argv[1] if len(sys.argv) > 1 else PLUGIN_PATH
    if not os.path.isfile(plugin_path):
        print(f"ERRO: plugin não encontrado em {plugin_path}", file=sys.stderr)
        return 1

    with open(plugin_path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    if MARKER in content:
        return 0

    if UNPATCHED_FN not in content:
        print(
            "ERRO: formato da função resolveModelPipeline2 desconhecido — "
            "patch automático impossível, revisão manual necessária",
            file=sys.stderr,
        )
        return 3

    backup = f"{plugin_path}.bak-patcher-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(plugin_path, backup)

    patched = content.replace(UNPATCHED_FN, PATCHED_FN, 1)
    with open(plugin_path, "w", encoding="utf-8") as fh:
        fh.write(patched)

    check = subprocess.run(["node", "--check", plugin_path], capture_output=True)
    if check.returncode != 0:
        shutil.copy2(backup, plugin_path)
        print(f"ERRO: node --check falhou após patch (restaurado do backup): "
              f"{check.stderr.decode(errors='replace')[:300]}", file=sys.stderr)
        return 1

    print(f"Patch dot-strip reaplicado (backup: {backup})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
