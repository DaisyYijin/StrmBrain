# 标记为 Python 包
# === Python 3.10/3.11 兼容补丁 ===
# p115client 0.0.9.x 依赖 typing.Self / collections.abc.Buffer 等 3.12+ 符号
# 在导入 p115client 前，从 typing_extensions 补全到标准库 typing / collections.abc
import collections.abc as _collections_abc
import typing as _typing

try:
    import typing_extensions as _typing_ext
except ImportError:
    _typing_ext = None

# 补全 typing 模块缺失的符号（从 typing_extensions 借调）
if _typing_ext is not None:
    for _name in ("Self", "Buffer", "Never", "LiteralString", "Required", "NotRequired",
                  "Unpack", "TypeAlias", "ParamSpec", "TypeVarTuple", "Concatenate",
                  "TypeGuard", "override", "deprecated", "reveal_type"):
        if not hasattr(_typing, _name) and hasattr(_typing_ext, _name):
            setattr(_typing, _name, getattr(_typing_ext, _name))

# 补全 collections.abc.Buffer
if not hasattr(_collections_abc, "Buffer"):
    if hasattr(_typing, "Buffer"):
        _collections_abc.Buffer = _typing.Buffer
    else:
        _collections_abc.Buffer = type(
            "Buffer",
            (),
            {"__instancecheck__": lambda self, obj: isinstance(obj, (bytes, bytearray, memoryview))},
        )
