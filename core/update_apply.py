"""退出后运行的独立更新助手。仅使用标准库，支持失败回滚和自动重启。"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
import uuid

BLOCKED = {'.git', '.venv', 'venv', 'env', '__pycache__', '.idea', 'config', 'logs', 'build', 'dist', '.updates'}


def checked_path(root, name):
    parts = name.split('/')
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute() or any(p in ('', '.', '..') or p.endswith((' ', '.')) or any(ord(c) < 32 for c in p)
                or p.split('.')[0].casefold() in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))} for p in parts)
            or any(p.casefold() in BLOCKED for p in parts) or path.name.casefold() == 'token.txt'
            or path.name.casefold().startswith('capture.log') or path.name.casefold() in ('.update-state.json', '.update-result.json', 'wrong_memory.json')
            or path.name.casefold().endswith(('.log', '.lock', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm'))):
        raise ValueError('拒绝更新受保护或不安全的路径：' + name)
    target = Path(root)
    for part in parts:
        target /= part
        if target.is_symlink():
            raise ValueError('拒绝更新符号链接：' + name)
    if target.exists() and not target.is_file():
        raise ValueError('更新目标不是普通文件：' + name)
    return target


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def replace_file(source, target, mode=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.update-' + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def apply_transaction(plan):
    root, stage = Path(plan['root']).resolve(), Path(plan['stage']).resolve()
    if plan.get('schema') != 1 or not plan.get('files'):
        raise ValueError('更新计划为空或版本不受支持')
    prepared, seen = [], set()
    for row in plan['files']:
        name = row['path']
        key = name.casefold()
        if key in seen:
            raise ValueError('更新计划存在重复文件')
        seen.add(key)
        source, target = checked_path(stage / 'files', name), checked_path(root, name)
        if file_hash(source) != row['sha256']:
            raise ValueError('暂存文件校验失败：' + name)
        backup = checked_path(stage / 'backup', name)
        if target.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        prepared.append((source, target, backup, row.get('mode', 0o644)))
    changed = []
    try:
        for source, target, backup, mode in prepared:
            replace_file(source, target, mode)
            changed.append((target, backup))
    except BaseException:
        for target, backup in reversed(changed):
            if backup.exists():
                replace_file(backup, target)
            else:
                target.unlink(missing_ok=True)
        raise


def running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_for_exit(plan, timeout=120):
    end = time.monotonic() + timeout
    while True:
        pids = [plan['parent_pid']]
        for path in Path(plan['instances']).glob('*.lock'):
            try:
                pids.append(int(path.name.split('-', 1)[0]))
            except ValueError:
                continue
        if not any(running(pid) for pid in pids):
            return
        if time.monotonic() >= end:
            raise TimeoutError('其他窗口尚未退出，未替换程序文件')
        time.sleep(.25)


def write_result(plan, success, message):
    root = Path(plan['root'])
    (root / '.update-result.json').write_text(json.dumps({'success': success, 'message': message,
                                                        'version': plan['version']}, ensure_ascii=False), encoding='utf-8')
    if success:
        (root / '.update-state.json').write_text(json.dumps({'version': plan['version'], 'revision': plan['revision']}), encoding='utf-8')


def run_plan(path):
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    try:
        wait_for_exit(plan)
        apply_transaction(plan)
        write_result(plan, True, '更新完成')
    except Exception as error:
        write_result(plan, False, str(error))
    finally:
        (Path(plan['instances']) / 'update.pending').unlink(missing_ok=True)
        subprocess.Popen(plan['command'], cwd=plan['root'], start_new_session=True,
                         env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'})


# PowerShell 是 Windows 系统组件，更新 EXE 时不会占用被替换的 Python/Qt DLL。
WINDOWS_HELPER = r'''
$ErrorActionPreference = 'Stop'
$p = Get-Content -LiteralPath $env:CIDAREN_UPDATE_PLAN -Raw -Encoding UTF8 | ConvertFrom-Json
$root = [IO.Path]::GetFullPath($p.root).TrimEnd('\')
$stage = [IO.Path]::GetFullPath($p.stage)
$changed = [Collections.Generic.List[object]]::new()
function SafePath($base, $name) {
  if (!$name -or $name.Contains('\') -or $name.Contains(':') -or $name.StartsWith('/')) { throw 'Unsafe update path' }
  $parts = $name.Split('/')
  $blocked = @('.git','.venv','venv','env','__pycache__','.idea','config','logs','build','dist','.updates')
  foreach ($part in $parts) { if (!$part -or $part -eq '.' -or $part -eq '..' -or $blocked -contains $part) { throw 'Protected update path' } }
  $leaf = $parts[-1]
  if ($leaf -eq 'token.txt' -or $leaf -eq 'wrong_memory.json' -or $leaf.StartsWith('capture.log') -or $leaf -like '*.log' -or $leaf -like '*.lock' -or $leaf -like '*.sqlite3*' -or $leaf.StartsWith('.update-')) { throw 'Protected local data' }
  $target = [IO.Path]::GetFullPath((Join-Path $base $name))
  if (!$target.StartsWith($base.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Escaped update root' }
  $walk = $base
  foreach ($part in $parts) {
    $walk = Join-Path $walk $part
    if ((Test-Path -LiteralPath $walk) -and ((Get-Item -LiteralPath $walk -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Symbolic update path' }
  }
  return $target
}
function FileHash($path) {
  $sha = [Security.Cryptography.SHA256]::Create()
  $stream = $null
  try {
    $stream = [IO.File]::OpenRead($path)
    return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()
  } finally { if ($stream) { $stream.Dispose() }; $sha.Dispose() }
}
function InstallFile($source,$target) {
  [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
  $temp = $target+'.update-'+[guid]::NewGuid().ToString('N')
  try {
    [IO.File]::Copy($source,$temp,$true)
    if ([IO.File]::Exists($target)) { [IO.File]::Replace($temp,$target,$null) }
    else { [IO.File]::Move($temp,$target) }
  } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
}
$success = $false
$message = ''
try {
  if ($p.schema -ne 1 -or $p.files.Count -eq 0) { throw 'Invalid update plan' }
  $deadline = (Get-Date).AddSeconds(120)
  do {
    $busy = @(Get-CimInstance Win32_Process | Where-Object {
      $_.ProcessId -ne $PID -and ($_.ProcessId -eq $p.parent_pid -or
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)))
    })
    if ($busy.Count -eq 0) { break }
    if ((Get-Date) -gt $deadline) { throw 'Other application windows are still running; files were not replaced' }
    Start-Sleep -Milliseconds 250
  } while ($true)
  $seen = @{}
  foreach ($f in $p.files) {
    if ($seen.ContainsKey($f.path)) { throw 'Duplicate update path' }; $seen[$f.path] = $true
    $src = SafePath (Join-Path $stage 'files') $f.path
    $dst = SafePath $root $f.path
    if ((FileHash $src) -ne $f.sha256) { throw 'Staged file checksum mismatch' }
    $bak = SafePath (Join-Path $stage 'backup') $f.path
    if (Test-Path -LiteralPath $dst) {
      [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($bak)) | Out-Null
      [IO.File]::Copy($dst,$bak,$true)
    }
  }
  foreach ($f in $p.files) {
    $src = SafePath (Join-Path $stage 'files') $f.path
    $dst = SafePath $root $f.path
    $bak = SafePath (Join-Path $stage 'backup') $f.path
    try { InstallFile $src $dst } catch { throw ('Cannot replace '+$f.path+': '+$_.Exception.Message) }
    $changed.Add(@{target=$dst;backup=$bak})
  }
  $success = $true; $message = '更新完成'
  @{version=$p.version;revision=$p.revision} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root '.update-state.json') -Encoding UTF8
} catch {
  $success = $false
  $message = $_.Exception.Message
  Write-Output ($_ | Format-List * -Force | Out-String)
  for ($i=$changed.Count-1;$i -ge 0;$i--) {
    $item=$changed[$i]
    if (Test-Path -LiteralPath $item.backup) { InstallFile $item.backup $item.target }
    elseif (Test-Path -LiteralPath $item.target) { Remove-Item -LiteralPath $item.target -Force }
  }
}
@{success=$success;message=$message;version=$p.version} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root '.update-result.json') -Encoding UTF8
Remove-Item -LiteralPath (Join-Path $p.instances 'update.pending') -Force -ErrorAction SilentlyContinue
$env:PYINSTALLER_RESET_ENVIRONMENT = '1'
if ($p.command.Count -gt 1) {
  $argsLine = (($p.command | Select-Object -Skip 1 | ForEach-Object { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }) -join ' ')
  Start-Process -FilePath $p.command[0] -ArgumentList $argsLine -WorkingDirectory $root
} else { Start-Process -FilePath $p.command[0] -WorkingDirectory $root }
'''


def launch_windows_helper(plan_file, log):
    import base64
    env = os.environ.copy()
    env['CIDAREN_UPDATE_PLAN'] = str(plan_file)
    system_ps = Path(env.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0'
    # 从 pwsh/开发终端启动时，不能继承 PS7 的模块目录给系统 PowerShell 5。
    env['PSModulePath'] = str(system_ps / 'Modules')
    command = [str(system_ps / 'powershell.exe'), '-NoProfile', '-NonInteractive', '-EncodedCommand',
               base64.b64encode(WINDOWS_HELPER.encode('utf-16le')).decode()]
    return subprocess.Popen(command, env=env, stdout=log, stderr=log,
                            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)


_helpers = []  # 保持句柄到父进程退出，避免提前析构仍运行的更新进程。


def launch_helper(plan_file):
    from util.file_lock import atomic_json
    plan_file = Path(plan_file).resolve()
    stage = plan_file.parent
    plan = json.loads(plan_file.read_text(encoding='utf-8'))
    pending = Path(plan['instances']) / 'update.pending'
    pending.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(pending, {'pids': [os.getpid()]})
    log = (stage / 'apply.log').open('ab')
    try:
        if os.name == 'nt' and getattr(sys, 'frozen', False):
            child = launch_windows_helper(plan_file, log)
        else:
            helper = stage / 'apply_helper.py'
            shutil.copy2(Path(__file__), helper)
            child = subprocess.Popen([sys.executable, '-I', str(helper), str(plan_file)],
                                     stdout=log, stderr=log, start_new_session=True)
        atomic_json(pending, {'pids': [os.getpid(), child.pid]})
        _helpers[:] = [p for p in _helpers if p.poll() is None]
        _helpers.append(child)
        return child.pid
    except Exception:
        pending.unlink(missing_ok=True)
        raise
    finally:
        log.close()


if __name__ == '__main__':
    run_plan(sys.argv[1])
