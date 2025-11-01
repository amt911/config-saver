import os
import glob
import re

class PathExpander:
    def __init__(self, custom_vars=None):
        # Permite personalizar el diccionario si se desea
        if custom_vars is None:
            custom_vars = {
                "HOME": os.path.expanduser("~"),
                "CONFIG_DIR": os.path.expanduser("~/.config"),
                "SHARE_DIR": os.path.expanduser("~/.local/share"),
                "BIN_DIR": os.path.expanduser("~/.local/bin"),
                "LOCALSHARE_DIR": os.path.expanduser("~/.local/share"),
            }
        self.custom_vars = custom_vars

    def expand(self, path: str) -> str:
        # Expande variables personalizadas tipo $HOME, $CONFIG_DIR, etc.
        for key, value in self.custom_vars.items():
            path = path.replace(f"${key}", value)
        # Expande variables de entorno estándar
        path = os.path.expandvars(path)
        # Expande placeholders avanzados
        # ENDS_WITH
        ends_match = re.search(r"\${ENDS_WITH=['\"](.+?)['\"]}", path)
        if ends_match:
            suffix = ends_match.group(1)
            parent = os.path.dirname(path)
            candidates = [d for d in glob.glob(os.path.join(parent, "*")) if d.endswith(suffix)]
            if candidates:
                path = path.replace(ends_match.group(0), os.path.basename(candidates[0]))
        # BEGINS_WITH
        begins_match = re.search(r"\${BEGINS_WITH=['\"](.+?)['\"]}", path)
        if begins_match:
            prefix = begins_match.group(1)
            parent = os.path.dirname(path)
            candidates = [d for d in glob.glob(os.path.join(parent, "*")) if os.path.basename(d).startswith(prefix)]
            if candidates:
                path = path.replace(begins_match.group(0), os.path.basename(candidates[0]))
        return path