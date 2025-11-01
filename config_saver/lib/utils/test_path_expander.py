import os
from config_saver.lib.parser.parser import Parser

def main():
    parser = Parser("configs/default-config.yaml")
    data = parser.get_data()
    print("Rutas expandidas en 'directories':")
    dirs = data.get("directories", [])
    for entry in dirs:
        if isinstance(entry, str):
            print(f"- {entry} | exists: {os.path.exists(entry)}")
        elif isinstance(entry, dict) and "source" in entry:
            src = entry["source"]
            print(f"- {src} | exists: {os.path.exists(src)}")
            files = entry.get("files", [])
            for f in files:
                fpath = os.path.join(src, f)
                print(f"  - {fpath} | exists: {os.path.exists(fpath)}")

if __name__ == "__main__":
    main()