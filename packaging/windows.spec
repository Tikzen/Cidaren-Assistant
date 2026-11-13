from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
spacy_data, spacy_binaries, spacy_hidden = collect_all('spacy')
resources = [(str(root / 'config'), 'config'), (str(root / 'assets'), 'assets'),
             (str(root / 'en_core_web_sm'), 'en_core_web_sm'), (str(root / 'LICENSE'), '.')]
# Whitelist capture source files. No token, capture logs or user-generated data.
for name in ('__init__.py', 'addon.py', 'proxy_manager.py'):
    resources.append((str(root / 'get token' / 'fetch_token' / name), 'get token/fetch_token'))
resources += collect_data_files('certifi')
a = Analysis([str(root / 'packaging/windows_entry.py')], pathex=[str(root)],
             binaries=spacy_binaries, datas=resources + spacy_data,
             hiddenimports=spacy_hidden + ['spacy.lang.en', 'PyQt6.QtSvg'],
             excludes=['mitmproxy', 'pytest', 'matplotlib', 'IPython', 'tkinter'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='CidarenAssistant',
          console=False, debug=False, upx=False, icon=str(root / 'assets/icon.ico'),
          contents_directory='.')

proxy_data = collect_data_files('mitmproxy') + copy_metadata('mitmproxy')
proxy_hidden = collect_submodules('mitmproxy')
b = Analysis([str(root / 'packaging/proxy_entry.py')], pathex=[str(root)],
             datas=proxy_data, hiddenimports=proxy_hidden,
             excludes=['PyQt6', 'spacy', 'pytest', 'matplotlib', 'IPython', 'tkinter'])
proxy_pyz = PYZ(b.pure)
proxy_exe = EXE(proxy_pyz, b.scripts, [], exclude_binaries=True, name='mitmdump',
                console=True, debug=False, upx=False, contents_directory='.')
COLLECT(exe, proxy_exe, a.binaries, a.datas, b.binaries, b.datas,
        strip=False, upx=False, name='CidarenAssistant-Windows-x64')
