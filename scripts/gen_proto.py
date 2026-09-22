"""生成 Protobuf Python 代码（阶段 3，3.2）。

用法::

    uv run python scripts/gen_proto.py

输入 ``proto/rdc.proto``，输出 ``src/rdc/protocol/pb/rdc_pb2.py``。
生成物**不入库**（.gitignore 已含 ``src/rdc/protocol/pb/``），由构建/CI 现场生成。

实现选择 ``grpc_tools.protoc`` 而非系统 ``protoc``：本机与 CI 都无需额外安装
protoc 二进制，版本随 ``grpcio-tools`` 锁文件确定，避免"本机能生成 CI 不能"漂移。

注意：本脚本刻意**不** import ``rdc`` 包（生成物属于 rdc），因此可以在
``rdc`` 尚不可导入（首次 clone、pb/ 还不存在）时运行。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = REPO_ROOT / "proto"
PROTO_FILE = PROTO_DIR / "rdc.proto"
OUT_DIR = REPO_ROOT / "src" / "rdc" / "protocol" / "pb"


def main() -> int:
    """调用 grpc_tools.protoc 生成 pb 代码，返回进程退出码。"""
    from grpc_tools import protoc

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # PROTO_DIR 作为 include 路径，使 --proto_path 与输入文件同源；
    # sys.path 里的 protobuf well-known types 目录一并带上（本文件未使用，留作扩展）。
    args = [
        "gen_proto",
        f"--proto_path={PROTO_DIR}",
        f"--python_out={OUT_DIR}",
        # 一并生成类型存根，使 codec.py 在 mypy strict 下拿到真实字段类型，
        # 无需为 rdc_pb2 关闭类型检查（避免 Any 蔓延）。
        f"--pyi_out={OUT_DIR}",
        str(PROTO_FILE),
    ]
    status = protoc.main(args)
    if status != 0:
        print(f"protoc 失败，退出码 {status}", file=sys.stderr)
        return int(status)

    init_py = OUT_DIR / "__init__.py"
    if not init_py.exists():
        init_py.write_text(
            '"""protoc 生成物包（阶段 3）。整体 gitignore，不入库、不参与 ruff/mypy。"""\n',
            encoding="utf-8",
        )

    # 生成后立即自检导入，确保运行时 protobuf 版本与生成器匹配。
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from rdc.protocol.pb import rdc_pb2  # noqa: F401

    print(f"OK: 已生成 {OUT_DIR / 'rdc_pb2.py'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
