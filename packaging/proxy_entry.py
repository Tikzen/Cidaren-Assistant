"""Frozen mitmdump entry; the existing capture service finds mitmdump.exe beside the app."""
import multiprocessing
from mitmproxy.tools.main import mitmdump

if __name__ == '__main__':
    multiprocessing.freeze_support()
    mitmdump()
