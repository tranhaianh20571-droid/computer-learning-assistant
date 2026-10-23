$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class BrowserCommand {
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CommandLineToArgvW(string command, out int count);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr pointer);
    public static string Executable(string command) {
        int count;
        IntPtr values=CommandLineToArgvW(command,out count);
        if(values==IntPtr.Zero) return null;
        try { return count>0 ? Marshal.PtrToStringUni(Marshal.ReadIntPtr(values)) : null; }
        finally { LocalFree(values); }
    }
}
'@
$browsers = [System.Collections.Generic.List[object]]::new()
function Add-BrowserPath([string]$executable, [string]$label) {
    if (-not $executable) { return }
    $executable = [Environment]::ExpandEnvironmentVariables($executable).Trim('"')
    if (-not $label -or $label.StartsWith('@')) {
        if (Test-Path -LiteralPath $executable -PathType Leaf) {
            $label = [Diagnostics.FileVersionInfo]::GetVersionInfo($executable).ProductName
        }
        if (-not $label) { $label = [IO.Path]::GetFileNameWithoutExtension($executable) }
    }
    $browsers.Add([pscustomobject]@{name=$label; executable=$executable})
}
foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryHive]::LocalMachine)) {
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
        try {
            $clients = $base.OpenSubKey('SOFTWARE\Clients\StartMenuInternet')
            if ($null -ne $clients) { try {
                foreach ($name in $clients.GetSubKeyNames()) {
                    $client = $clients.OpenSubKey($name)
                    $command = $client.OpenSubKey('shell\open\command')
                    try {
                        if ($null -eq $command) { continue }
                        $raw = [string]$command.GetValue('')
                        $executable = [BrowserCommand]::Executable([Environment]::ExpandEnvironmentVariables($raw))
                        if ($executable) {
                            $label = [string]$client.GetValue('')
                            if (-not $label) { $label = $name }
                            Add-BrowserPath $executable $label
                        }
                    } finally {
                        if ($command) { $command.Dispose() }
                        $client.Dispose()
                    }
                }
            } finally { $clients.Dispose() } }
            foreach ($filename in @('chrome.exe','msedge.exe','firefox.exe','brave.exe','vivaldi.exe','opera.exe',
                'tabbit.exe','quark.exe','360se.exe','360chrome.exe','QQBrowser.exe','SogouExplorer.exe','coccoc.exe')) {
                $entry = $base.OpenSubKey("SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$filename")
                if ($entry) {
                    try { Add-BrowserPath ([string]$entry.GetValue('')) '' }
                    finally { $entry.Dispose() }
                }
            }
        } finally { $base.Dispose() }
    }
}
# Include a browser registered only as the HTTPS handler, without making it the
# automatic choice in the application's picker.
$current = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Registry64)
$classes = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::ClassesRoot, [Microsoft.Win32.RegistryView]::Registry64)
try {
    $choice = $current.OpenSubKey('Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice')
    if ($choice) {
        try { $program = [string]$choice.GetValue('ProgId') } finally { $choice.Dispose() }
        $command = $classes.OpenSubKey("$program\shell\open\command")
        $application = $classes.OpenSubKey("$program\Application")
        try {
            if ($command) {
                $label = if ($application) { [string]$application.GetValue('ApplicationName') } else { '' }
                Add-BrowserPath ([BrowserCommand]::Executable([Environment]::ExpandEnvironmentVariables([string]$command.GetValue('')))) $label
            }
        } finally {
            if ($command) { $command.Dispose() }
            if ($application) { $application.Dispose() }
        }
    }
} finally { $current.Dispose(); $classes.Dispose() }
ConvertTo-Json -InputObject @($browsers.ToArray()) -Compress -Depth 3
