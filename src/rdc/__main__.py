"""支持 ``python -m rdc`` 运行方式。"""

from __future__ import annotations

from rdc.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
