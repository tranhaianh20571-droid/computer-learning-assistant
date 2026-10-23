"""Build a narrow LLVM/AppContainer compatibility shim, then patch owned PE imports.

LLVM's startup GetLongPathNameW call requires enumerating every ancestor up to
the drive root, even when the executable path is already long. AppContainer
correctly denies that enumeration. Returning the supplied long path on this
specific denial grants no file access and leaves all filesystem ACL checks intact.
"""
import argparse
from pathlib import Path
import subprocess

import pefile

parser = argparse.ArgumentParser()
parser.add_argument("root", type=Path)
args = parser.parse_args()
directory = args.root.resolve() / "mingw" / "bin"
targets = [directory / name for name in
           ("clang-23.exe", "ld.lld.exe", "libLLVM-23.dll", "libclang-cpp.dll")]
exports = set()
patches = {}
for target in targets:
    pe = pefile.PE(str(target))
    for library in pe.DIRECTORY_ENTRY_IMPORT:
        if any(symbol.name == b"GetLongPathNameW" for symbol in library.imports):
            patches.setdefault(target, []).append(library.struct.Name)
            for symbol in library.imports:
                if symbol.name is None:
                    raise RuntimeError("Unexpected ordinal kernel import")
                exports.add(symbol.name.decode("ascii"))
    pe.close()
if "GetLongPathNameW" not in exports:
    raise RuntimeError("Pinned LLVM import layout changed")
source = directory / "study-compat.c"
definition = directory / "study-compat.def"
source.write_text(r"""
#define GetLongPathNameW original_declaration_GetLongPathNameW
#include <windows.h>
#undef GetLongPathNameW
#include <wchar.h>
#include <string.h>
__declspec(dllexport) DWORD WINAPI GetLongPathNameW(LPCWSTR input, LPWSTR output, DWORD count) {
  typedef DWORD (WINAPI *original_fn)(LPCWSTR, LPWSTR, DWORD);
  original_fn original = (original_fn)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "GetLongPathNameW");
  DWORD result = original(input, output, count);
  if (result || GetLastError() != ERROR_ACCESS_DENIED || wcschr(input, L'~')) return result;
  size_t length = wcslen(input);
  if (length >= count) return (DWORD)(length + 1);
  memmove(output, input, (length + 1) * sizeof(wchar_t));
  SetLastError(ERROR_SUCCESS);
  return (DWORD)length;
}
""", encoding="ascii")
definition.write_text("LIBRARY sc-k32\nEXPORTS\n" + "\n".join(
    name if name == "GetLongPathNameW" else name + "=KERNEL32." + name
    for name in sorted(exports)) + "\n", encoding="ascii")
subprocess.run([str(targets[0]), "-shared", str(source), str(definition),
    "-o", str(directory / "study-compat.new.dll"), "-Wl,--no-insert-timestamp"], check=True)
(directory / "study-compat.new.dll").replace(directory / "sc-k32.dll")
for target in targets:
    if target not in patches:
        continue
    pe = pefile.PE(str(target))
    for name_rva in patches[target]:
        pe.set_bytes_at_rva(name_rva, b"sc-k32.dll\0")
    pe.OPTIONAL_HEADER.CheckSum = pe.generate_checksum()
    content = pe.write()
    pe.close()
    target.write_bytes(content)
print("Pinned LLVM startup compatibility shim built; file and network permissions unchanged.")
