"""PyInstaller entry point (absolute import so it works as a frozen script)."""
from isle_livemap.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
