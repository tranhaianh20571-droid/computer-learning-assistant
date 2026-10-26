"""Opt the pinned PostgreSQL command-line processes into Windows UTF-8 paths.

Only PE manifests are changed. Requires the Windows SDK at build time.
"""
import argparse
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ASM = "urn:schemas-microsoft-com:asm.v1"
ASM3 = "urn:schemas-microsoft-com:asm.v3"
WINDOWS = "http://schemas.microsoft.com/SMI/2019/WindowsSettings"
TARGETS = ("initdb.exe", "postgres.exe", "pg_ctl.exe", "pg_dump.exe",
           "pg_restore.exe", "pg_isready.exe")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--mt", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("Windows build required")
    tools = list((Path(os.environ["ProgramFiles(x86)"]) / "Windows Kits/10/bin").glob("*/x64/mt.exe"))
    tool = args.mt or max(tools, key=lambda item: tuple(int(part) for part in item.parts[-3].split(".")))
    directory = args.root.resolve() / "bin"
    with tempfile.TemporaryDirectory(prefix="postgres-manifest-") as temporary:
        manifest = Path(temporary) / "application.manifest"
        for name in TARGETS:
            executable = directory / name
            if not executable.is_file():
                raise RuntimeError("Pinned PostgreSQL executable missing")
            result = subprocess.run([str(tool), "-nologo",
                f"-inputresource:{executable};#1", f"-out:{manifest}"],
                capture_output=True, check=False)
            if result.returncode == 0:
                document = ET.parse(manifest)
                assembly = document.getroot()
            elif b"c101008c" in result.stdout.lower() + result.stderr.lower():
                assembly = ET.Element(f"{{{ASM}}}assembly", {"manifestVersion": "1.0"})
                ET.SubElement(assembly, f"{{{ASM}}}assemblyIdentity",
                    {"name": "StudyCompanion.PostgreSQL." + executable.stem,
                     "version": "1.0.0.0", "processorArchitecture": "amd64", "type": "win32"})
                document = ET.ElementTree(assembly)
            else:
                raise RuntimeError("Cannot read PostgreSQL manifest")
            application = assembly.find(f"{{{ASM3}}}application")
            if application is None:
                application = ET.SubElement(assembly, f"{{{ASM3}}}application")
            settings = application.find(f"{{{ASM3}}}windowsSettings")
            if settings is None:
                settings = ET.SubElement(application, f"{{{ASM3}}}windowsSettings")
            codepage = settings.find(f"{{{WINDOWS}}}activeCodePage")
            if codepage is None:
                codepage = ET.SubElement(settings, f"{{{WINDOWS}}}activeCodePage")
            codepage.text = "UTF-8"
            document.write(manifest, encoding="utf-8", xml_declaration=True)
            subprocess.run([str(tool), "-nologo", "-manifest", str(manifest),
                f"-outputresource:{executable};#1"], capture_output=True, check=True)
            subprocess.run([str(tool), "-nologo", f"-inputresource:{executable};#1",
                f"-out:{manifest}"], capture_output=True, check=True)
            if ET.parse(manifest).find(f".//{{{WINDOWS}}}activeCodePage").text != "UTF-8":
                raise RuntimeError("PostgreSQL UTF-8 manifest not embedded")
    print("Pinned PostgreSQL UTF-8 manifests verified: 6 commands")


if __name__ == "__main__":
    main()
